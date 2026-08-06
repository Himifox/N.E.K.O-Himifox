"""Feature-gated preference recommendation primitives for proactive chat.

The implementation is intentionally process-local.  It proves the causal loop
without adding a model request or a persistence dependency:

Phase 1 output -> validated preference events -> decayed profile -> next-round
source/candidate weighting.

Run the deterministic offline loop with::

    python -m main_logic.proactive_chat.preference_recommendation

Set ``NEKO_PROACTIVE_PREFERENCE_DEMO_ENABLED=1`` before starting N.E.K.O. to
enable the same path in the existing proactive Phase 1 flow.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


TAG_VALUES: dict[str, tuple[str, ...]] = {
    "domain": (
        "tech",
        "acg",
        "gaming",
        "companion",
        "entertainment",
        "internet_culture",
        "daily_life",
    ),
    "media": ("news", "video", "music", "meme"),
    "context": ("focus", "relax", "energy", "sleep"),
}

SIGNAL_WEIGHTS = {
    "explicit_like": 5.0,
    "explicit_dislike": 5.0,
    "current_intent": 2.0,
    "inferred_interest": 0.5,
    "topic_mention": 0.2,
}

SESSION_TTL_SECONDS = {
    "explicit_like": 24 * 3600,
    "explicit_dislike": 24 * 3600,
    "current_intent": 6 * 3600,
    "inferred_interest": 2 * 3600,
    "topic_mention": 3600,
}

_SCOPE_HALF_LIFE_SECONDS = {
    "session": 6 * 3600,
    "long_term": 30 * 86400,
}
_PROFILE_MAX_EVENTS = 128
_MAX_EVENTS_PER_TURN = 3
_MAX_EVIDENCE_CHARS = 60


@dataclass(frozen=True, slots=True)
class PreferenceEvent:
    dimension: str
    value: str
    signal: str
    polarity: int
    confidence: float
    scope: str
    occurred_at: float
    evidence_id: str
    expires_at: float | None

    @property
    def tag(self) -> str:
        return f"{self.dimension}.{self.value}"

    @property
    def dedupe_key(self) -> str:
        return f"{self.evidence_id}|{self.tag}|{self.polarity}"


@dataclass(frozen=True, slots=True)
class CandidateTags:
    domain: str | None
    media: str | None
    contexts: tuple[str, ...] = ()

    @property
    def pool(self) -> str:
        if self.domain and self.media:
            return f"{self.domain}/{self.media}"
        return "unknown"


_profile_events: dict[str, deque[PreferenceEvent]] = {}


_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "tech": (
        "ai", "llm", "agent", "python", "github", "open source",
        "人工智能", "模型", "智能体", "编程", "代码", "开源", "数码",
    ),
    "acg": (
        "anime", "manga", "vtuber", "vocaloid", "cosplay",
        "动画", "动漫", "漫画", "二次元", "虚拟主播", "虚拟歌手", "初音", "插画",
    ),
    "gaming": (
        "game", "gaming", "esports", "steam", "playstation", "xbox", "nintendo",
        "游戏", "电竞", "手游", "主机", "攻略",
    ),
    "companion": (
        "desktop pet", "virtual companion", "live2d", "vrm", "desktop mascot",
        "桌宠", "虚拟陪伴", "ai伴侣", "虚拟角色",
    ),
    "entertainment": (
        "movie", "film", "celebrity", "television", "pop music",
        "电影", "影视", "综艺", "明星", "娱乐", "新歌",
    ),
    "internet_culture": (
        "meme", "viral", "trending", "reddit", "weibo", "tieba",
        "梗", "热搜", "热议", "网络文化", "社区",
    ),
    "daily_life": (
        "cat", "pet", "food", "travel", "health", "lifestyle",
        "猫", "宠物", "美食", "旅行", "生活", "健康",
    ),
}

_CONTEXT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "focus": ("focus", "study", "work", "coding", "专注", "学习", "工作", "写代码"),
    "relax": ("relax", "chill", "lofi", "lo-fi", "放松", "治愈", "轻松"),
    "energy": ("energetic", "workout", "high energy", "元气", "高能", "燃"),
    "sleep": ("sleep", "night", "bedtime", "睡前", "助眠", "夜晚"),
}

_VIDEO_SOURCES = (
    "bilibili", "b站", "youtube", "twitch", "douyin", "抖音", "kuaishou", "快手",
)
_CHANNEL_MEDIA = {
    "news": "news",
    "video": "video",
    "music": "music",
    "meme": "meme",
}


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split())


def _keyword_hit(text: str, keyword: str) -> bool:
    keyword = keyword.casefold()
    if keyword.isascii():
        return bool(re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text))
    return keyword in text


def _user_history_lines(memory_context: str, master_name: str) -> list[str]:
    prefix = f"{master_name} |"
    return [
        _normalized(line[len(prefix):])
        for line in str(memory_context or "").splitlines()
        if line.startswith(prefix) and _normalized(line[len(prefix):])
    ]


def _resolve_evidence_id(
    evidence: str, *, memory_context: str, master_name: str
) -> str | None:
    evidence = _normalized(evidence)
    if not 4 <= len(evidence) <= _MAX_EVIDENCE_CHARS:
        return None
    user_lines = _user_history_lines(memory_context, master_name)
    folded_evidence = evidence.casefold()
    matches = [
        index
        for index, line in enumerate(user_lines)
        if folded_evidence in line.casefold()
    ]
    if not matches:
        return None
    index = matches[-1]
    full_line = user_lines[index]
    occurrence = sum(1 for line in user_lines[: index + 1] if line == full_line)
    digest = hashlib.sha256(full_line.encode("utf-8")).hexdigest()[:12]
    return f"{digest}:{occurrence}"


def validate_preference_events(
    raw_events: Any,
    *,
    memory_context: str,
    master_name: str,
    now: float | None = None,
) -> tuple[PreferenceEvent, ...]:
    """Validate model output and ground every event in a real user line."""

    if not isinstance(raw_events, list) or len(raw_events) > _MAX_EVENTS_PER_TURN:
        return ()
    timestamp = time.time() if now is None else now
    accepted: list[PreferenceEvent] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            continue
        dimension = str(raw.get("dimension", ""))
        value = str(raw.get("value", ""))
        signal = str(raw.get("signal", ""))
        scope = str(raw.get("scope", ""))
        if dimension not in TAG_VALUES or value not in TAG_VALUES[dimension]:
            continue
        if signal not in SIGNAL_WEIGHTS or scope not in _SCOPE_HALF_LIFE_SECONDS:
            continue
        try:
            polarity = int(raw.get("polarity", 0))
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if polarity not in {-1, 1} or not 0.0 <= confidence <= 1.0:
            continue
        if signal == "explicit_like" and polarity != 1:
            continue
        if signal == "explicit_dislike" and polarity != -1:
            continue
        if scope == "long_term" and signal not in {
            "explicit_like", "explicit_dislike"
        }:
            continue
        if dimension == "context" or signal in {
            "inferred_interest", "topic_mention"
        }:
            scope = "session"
        evidence_id = _resolve_evidence_id(
            str(raw.get("evidence", "")),
            memory_context=memory_context,
            master_name=master_name,
        )
        if evidence_id is None:
            continue
        expires_at = (
            timestamp + SESSION_TTL_SECONDS[signal]
            if scope == "session"
            else None
        )
        accepted.append(
            PreferenceEvent(
                dimension=dimension,
                value=value,
                signal=signal,
                polarity=polarity,
                confidence=confidence,
                scope=scope,
                occurred_at=timestamp,
                evidence_id=evidence_id,
                expires_at=expires_at,
            )
        )
    return tuple(accepted)


def _active_events(lanlan_name: str, now: float) -> deque[PreferenceEvent]:
    active = deque(
        (
            event
            for event in _profile_events.get(lanlan_name, ())
            if event.expires_at is None or event.expires_at >= now
        ),
        maxlen=_PROFILE_MAX_EVENTS,
    )
    if active:
        _profile_events[lanlan_name] = active
    else:
        _profile_events.pop(lanlan_name, None)
    return active


def update_preference_profile(
    lanlan_name: str,
    events: Sequence[PreferenceEvent],
    *,
    now: float | None = None,
) -> int:
    """Add new evidence-backed events and return the number actually added."""

    timestamp = time.time() if now is None else now
    active = _active_events(lanlan_name, timestamp)
    seen = {event.dedupe_key for event in active}
    added = 0
    for event in events:
        if event.dedupe_key in seen:
            continue
        active.append(event)
        seen.add(event.dedupe_key)
        added += 1
    if active:
        _profile_events[lanlan_name] = active
    return added


def get_preference_scores(
    lanlan_name: str, *, now: float | None = None
) -> dict[str, float]:
    timestamp = time.time() if now is None else now
    scores: dict[str, float] = {}
    for event in _active_events(lanlan_name, timestamp):
        age = max(0.0, timestamp - event.occurred_at)
        decay = 0.5 ** (age / _SCOPE_HALF_LIFE_SECONDS[event.scope])
        delta = (
            SIGNAL_WEIGHTS[event.signal]
            * event.confidence
            * event.polarity
            * decay
        )
        scores[event.tag] = scores.get(event.tag, 0.0) + delta
    return {tag: round(value, 4) for tag, value in scores.items()}


def format_preference_summary(scores: Mapping[str, float], *, limit: int = 3) -> str:
    strongest = sorted(scores.items(), key=lambda item: (-abs(item[1]), item[0]))
    return "\n".join(f"{tag}={score:+.2f}" for tag, score in strongest[:limit])


def clear_preference_profiles() -> None:
    """Reset process-local state; used by the executable demo and tests."""

    _profile_events.clear()


def classify_candidate(item: Mapping[str, Any]) -> CandidateTags:
    text = " ".join(
        _normalized(item.get(field))
        for field in ("title", "description_hint", "reason", "source", "url")
    ).casefold()
    hits = {
        domain: sum(_keyword_hit(text, keyword) for keyword in keywords)
        for domain, keywords in _DOMAIN_KEYWORDS.items()
    }
    domain = max(hits, key=lambda key: (hits[key], -list(hits).index(key)))
    if hits[domain] == 0:
        domain = None

    mode = str(item.get("mode", "")).lower()
    media = _CHANNEL_MEDIA.get(mode)
    if media is None and mode in {"home", "personal"}:
        media = (
            "video"
            if any(source in text for source in _VIDEO_SOURCES)
            else "news"
        )
    contexts = tuple(
        context
        for context, keywords in _CONTEXT_KEYWORDS.items()
        if any(_keyword_hit(text, keyword) for keyword in keywords)
    )
    return CandidateTags(domain=domain, media=media, contexts=contexts)


def _candidate_affinity(tags: CandidateTags, scores: Mapping[str, float]) -> float:
    domain_score = scores.get(f"domain.{tags.domain}", 0.0) if tags.domain else 0.0
    media_score = scores.get(f"media.{tags.media}", 0.0) if tags.media else 0.0
    context_score = max(
        (scores.get(f"context.{context}", 0.0) for context in tags.contexts),
        default=0.0,
    )
    return 0.6 * domain_score + 0.3 * media_score + 0.1 * context_score


def calculate_pool_probabilities(
    candidates: Sequence[Mapping[str, Any]],
    scores: Mapping[str, float],
    *,
    exploration: float = 0.15,
) -> dict[str, float]:
    pools: dict[str, CandidateTags] = {}
    for item in candidates:
        tags = classify_candidate(item)
        pools.setdefault(tags.pool, tags)
    if not pools:
        return {}
    if not any(abs(value) > 1e-9 for value in scores.values()):
        uniform = 1.0 / len(pools)
        return {pool: uniform for pool in pools}

    known = {pool: tags for pool, tags in pools.items() if pool != "unknown"}
    if not known:
        return {"unknown": 1.0}
    raw = {
        pool: math.exp(max(-4.0, min(4.0, 0.3 * _candidate_affinity(tags, scores))))
        for pool, tags in known.items()
    }
    total = sum(raw.values())
    uniform = exploration / len(pools)
    return {
        pool: (1.0 - exploration) * (raw.get(pool, 0.0) / total) + uniform
        for pool in pools
    }


def select_preference_candidates(
    candidates: Sequence[dict[str, Any]],
    scores: Mapping[str, float],
    *,
    total: int,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Select personalized candidates while reserving a 15% exploration slice."""

    ordered = list(candidates)
    if total <= 0 or not ordered:
        return []
    if not any(abs(value) > 1e-9 for value in scores.values()):
        return ordered[:total]

    randomizer = rng or random.Random()
    grouped: dict[str, list[dict[str, Any]]] = {}
    tags_by_pool: dict[str, CandidateTags] = {}
    for item in ordered:
        tags = classify_candidate(item)
        grouped.setdefault(tags.pool, []).append(item)
        tags_by_pool.setdefault(tags.pool, tags)

    known_pools = [pool for pool in grouped if pool != "unknown"]
    if not known_pools:
        return ordered[:total]
    exploration_slots = min(len(ordered), max(1, round(total * 0.15)))
    personalized_slots = min(total - exploration_slots, len(ordered))
    selected: list[dict[str, Any]] = []
    while len(selected) < personalized_slots and known_pools:
        weights = [
            math.exp(
                max(
                    -4.0,
                    min(4.0, 0.3 * _candidate_affinity(tags_by_pool[pool], scores)),
                )
            )
            for pool in known_pools
        ]
        pool = randomizer.choices(known_pools, weights=weights, k=1)[0]
        selected.append(grouped[pool].pop(0))
        if not grouped[pool]:
            known_pools.remove(pool)

    selected_ids = {id(item) for item in selected}
    remaining = [item for item in ordered if id(item) not in selected_ids]
    while len(selected) < total and remaining:
        item = randomizer.choice(remaining)
        remaining.remove(item)
        selected.append(item)
    return selected


def blend_source_weights(
    base_weights: Mapping[str, float],
    scores: Mapping[str, float],
    *,
    exploration: float = 0.15,
) -> dict[str, float]:
    if not base_weights or not any(abs(value) > 1e-9 for value in scores.values()):
        return dict(base_weights)
    raw: dict[str, float] = {}
    for channel, base in base_weights.items():
        media = _CHANNEL_MEDIA.get(channel)
        media_score = scores.get(f"media.{media}", 0.0) if media else 0.0
        raw[channel] = base * math.exp(max(-4.0, min(4.0, 0.3 * media_score)))
    total = sum(raw.values())
    uniform = 1.0 / len(raw)
    return {
        channel: (1.0 - exploration) * value / total + exploration * uniform
        for channel, value in raw.items()
    }


def _demo_candidates() -> list[dict[str, Any]]:
    return [
        {"title": "AI Agent 开源工具速览", "mode": "news", "source": "GitHub"},
        {"title": "Python 桌宠开发记录", "mode": "video", "source": "B站"},
        {"title": "本季动画新作整理", "mode": "video", "source": "B站"},
        {"title": "虚拟歌手新曲", "mode": "music", "source": "网易云"},
        {"title": "独立游戏试玩合集", "mode": "video", "source": "B站"},
        {"title": "今日社区热议", "mode": "news", "source": "微博"},
        {"title": "适合工作的 Lo-fi", "mode": "music", "source": "网易云"},
        {"title": "周末宠物日常", "mode": "video", "source": "B站"},
    ]


def run_demo() -> dict[str, Any]:
    """Execute a deterministic two-round loop without calling an external model."""

    from .generation import _parse_unified_phase1_result

    clear_preference_profiles()
    character = "DemoNeko"
    master = "主人"
    memory_context = "主人 | 我最近在研究 AI Agent，也喜欢看编程新闻\n"
    candidates = _demo_candidates()
    before = select_preference_candidates(
        candidates, {}, total=5, rng=random.Random(7)
    )
    phase1_output = """[WEB]\nTopic: AI Agent 开源工具速览\nSource: GitHub\n[PREFERENCE]\n[{"dimension":"domain","value":"tech","signal":"explicit_like","polarity":1,"confidence":0.95,"scope":"long_term","evidence":"最近在研究 AI Agent"},{"dimension":"media","value":"news","signal":"explicit_like","polarity":1,"confidence":0.9,"scope":"long_term","evidence":"喜欢看编程新闻"}]"""
    parsed = _parse_unified_phase1_result(phase1_output)
    events = validate_preference_events(
        parsed.get("preference_events"),
        memory_context=memory_context,
        master_name=master,
        now=0.0,
    )
    update_preference_profile(character, events, now=0.0)
    scores = get_preference_scores(character, now=0.0)
    after = select_preference_candidates(
        candidates, scores, total=5, rng=random.Random(7)
    )
    return {
        "new_llm_threads": 0,
        "new_llm_calls": 0,
        "accepted_events": [asdict(event) for event in events],
        "scores": scores,
        "pool_probabilities": calculate_pool_probabilities(candidates, scores),
        "round_1_candidates": [item["title"] for item in before],
        "round_2_candidates": [item["title"] for item in after],
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
