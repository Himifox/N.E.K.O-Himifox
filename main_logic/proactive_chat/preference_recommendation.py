"""Process-local Web recommendation feedback demo for proactive chat.

The feature reuses the existing Phase 1 call and source-history identity.  It
does not build a durable profile and never changes music or meme resources.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


PRIMARY_TOPICS = (
    "technology",
    "programming",
    "digital_devices",
    "science",
    "games",
    "anime_comics",
    "music_culture",
    "film_tv",
    "internet_culture",
    "books_education",
    "art_creative",
    "finance_business",
    "society",
    "sports",
    "automotive",
    "health_fitness",
    "food_culture",
    "travel_culture",
    "fashion_lifestyle",
    "pets_animals",
)
CHANNEL_NAMES = ("news", "video", "music", "meme")
REACTIONS = (
    "positive",
    "not_interested",
    "quality_issue",
    "source_distrust",
    "temporary_skip",
    "unclear",
)

_RECEIPT_TTL_SECONDS = 2 * 3600
_TOPIC_EVIDENCE_TTL_SECONDS = 2 * 3600
_CORRECTION_TTL_SECONDS = 5 * 3600
_SOURCE_SUPPRESSION_TTL_SECONDS = 5 * 3600
_MAX_RECEIPTS_PER_ROLE = 10
_MAX_TOPIC_EVIDENCE_PER_ROLE = 64
_MAX_EVIDENCE_CHARS = 100


_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "technology": (
        "人工智能", "生成式ai", "机器学习", "深度学习", "大模型", "ai", "llm",
        "ai agent", "robotics", "机器人", "科技",
    ),
    "programming": (
        "编程", "程序员", "代码", "开发者", "开源", "github", "python",
        "javascript", "typescript", "rust", "golang", "java", "api", "coding",
    ),
    "digital_devices": (
        "数码", "手机", "电脑", "笔记本", "显卡", "芯片", "处理器", "相机",
        "iphone", "android", "ipad", "macbook", "gpu", "cpu", "smartphone",
    ),
    "science": (
        "科学", "物理", "化学", "生物学", "天文", "宇宙", "航天", "研究论文",
        "quantum", "physics", "chemistry", "biology", "astronomy", "space",
    ),
    "games": (
        "游戏", "电竞", "手游", "主机", "攻略", "steam", "playstation", "xbox",
        "nintendo", "switch", "esports", "gaming", "game",
    ),
    "anime_comics": (
        "动画", "动漫", "漫画", "二次元", "轻小说", "番剧", "acg", "anime",
        "manga", "comic", "cosplay", "vtuber", "vocaloid",
    ),
    "music_culture": (
        "音乐", "歌曲", "歌手", "乐队", "演唱会", "专辑", "新歌", "music",
        "song", "singer", "album", "concert", "band",
    ),
    "film_tv": (
        "电影", "电视剧", "影视", "综艺", "纪录片", "票房", "影评", "movie",
        "film", "television", "tv series", "documentary", "box office",
    ),
    "internet_culture": (
        "网络文化", "热搜", "热议", "梗", "表情包", "网红", "社区", "meme",
        "viral", "trending", "reddit", "weibo", "微博", "贴吧",
    ),
    "books_education": (
        "书籍", "读书", "阅读", "教育", "课程", "学习方法", "考试", "学校",
        "book", "reading", "education", "course", "university", "school",
    ),
    "art_creative": (
        "艺术", "绘画", "插画", "摄影", "设计", "创意", "美术", "art",
        "painting", "illustration", "photography", "design", "creative",
    ),
    "finance_business": (
        "财经", "金融", "股票", "基金", "投资", "商业", "创业", "公司财报",
        "finance", "stock", "fund", "investment", "business", "startup",
    ),
    "society": (
        "社会", "民生", "法律", "公共政策", "公益", "社区治理", "society",
        "social issue", "law", "public policy", "charity",
    ),
    "sports": (
        "体育", "足球", "篮球", "网球", "羽毛球", "乒乓球", "奥运", "世界杯",
        "sports", "football", "soccer", "basketball", "tennis", "olympic",
    ),
    "automotive": (
        "汽车", "新能源车", "电动车", "车展", "试驾", "自动驾驶", "automotive",
        "car", "vehicle", "electric vehicle", "ev", "driving",
    ),
    "health_fitness": (
        "健康", "健身", "运动训练", "跑步", "瑜伽", "营养", "医疗", "养生",
        "health", "fitness", "workout", "running", "yoga", "nutrition",
    ),
    "food_culture": (
        "美食", "餐厅", "料理", "菜谱", "烹饪", "咖啡", "茶文化", "food",
        "restaurant", "recipe", "cooking", "coffee", "cuisine",
    ),
    "travel_culture": (
        "旅行", "旅游", "景点", "酒店", "攻略", "城市漫游", "travel",
        "tourism", "destination", "hotel", "trip",
    ),
    "fashion_lifestyle": (
        "时尚", "穿搭", "美妆", "护肤", "家居", "生活方式", "fashion",
        "outfit", "beauty", "skincare", "lifestyle", "home decor",
    ),
    "pets_animals": (
        "宠物", "猫咪", "狗狗", "动物", "养猫", "养狗", "萌宠", "pet",
        "cat", "dog", "animal", "wildlife",
    ),
}

_WEB_FIELDS = ("title", "description_hint", "reason", "source", "url")


@dataclass(frozen=True, slots=True)
class CandidateTopic:
    primary_topic: str | None


@dataclass(frozen=True, slots=True)
class RecommendationReceipt:
    receipt_id: str
    turn_id: str
    resource_key: str
    source_key: str
    title: str
    primary_topic: str | None
    delivered_at: float
    evidence_snapshot: dict[str, int]


@dataclass(frozen=True, slots=True)
class RecommendationFeedback:
    receipt_id: str
    reaction: str
    confidence: float
    evidence_id: str
    resource_key: str
    source_key: str
    primary_topic: str | None


@dataclass(frozen=True, slots=True)
class TopicEvidence:
    topic: str
    direction: int
    confidence: float
    resource_key: str
    evidence_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class TopicCorrection:
    topic: str
    score: float
    resource_keys: tuple[str, str]
    expires_at: float


@dataclass(frozen=True, slots=True)
class FeedbackProcessResult:
    accepted: bool
    reaction: str | None = None
    state_changed: bool = False


@dataclass(frozen=True, slots=True)
class PreferenceCandidateSelection:
    items: list[dict[str, Any]]
    personalized_slots: int
    exploration_slots: int


_receipts: dict[str, deque[RecommendationReceipt]] = {}
_topic_evidence: dict[str, deque[TopicEvidence]] = {}
_topic_corrections: dict[str, dict[str, TopicCorrection]] = {}
_source_suppressions: dict[str, dict[str, float]] = {}
_processed_feedback: set[tuple[str, str, str, str]] = set()


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split())


def _keyword_hit(text: str, keyword: str) -> bool:
    folded = keyword.casefold()
    if folded.isascii():
        return bool(re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", text))
    return folded in text


def classify_candidate(item: Mapping[str, Any]) -> CandidateTopic:
    """Classify one Web candidate using only the five existing Web fields."""
    text = " ".join(_normalized(item.get(field)) for field in _WEB_FIELDS).casefold()
    hits = {
        topic: sum(_keyword_hit(text, keyword) for keyword in keywords)
        for topic, keywords in _TOPIC_KEYWORDS.items()
    }
    primary_topic = max(
        PRIMARY_TOPICS,
        key=lambda topic: (hits[topic], -PRIMARY_TOPICS.index(topic)),
    )
    if hits[primary_topic] == 0:
        primary_topic = None
    return CandidateTopic(primary_topic=primary_topic)


def source_key_for_candidate(item: Mapping[str, Any]) -> str:
    url = _normalized(item.get("url"))
    if url:
        hostname = (urlsplit(url).hostname or "").casefold().strip(".")
        if hostname.startswith("www."):
            hostname = hostname[4:]
        if hostname:
            return hostname
    return _normalized(item.get("source")).casefold()


def _user_history_lines(memory_context: str, master_name: str) -> list[str]:
    prefix = f"{master_name} |"
    return [
        _normalized(line[len(prefix):])
        for line in str(memory_context or "").splitlines()
        if line.startswith(prefix) and _normalized(line[len(prefix):])
    ]


def _line_fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def capture_evidence_snapshot(memory_context: str, master_name: str) -> dict[str, int]:
    return dict(Counter(_line_fingerprint(line) for line in _user_history_lines(
        memory_context, master_name
    )))


def register_recommendation_receipt(
    role: str,
    *,
    turn_id: Any,
    web_link: Mapping[str, Any],
    memory_context: str,
    master_name: str,
    now: float | None = None,
) -> RecommendationReceipt | None:
    """Register a receipt after delivery code has proved the Web link was sent."""
    from .state import _source_hash

    title = _normalized(web_link.get("title"))
    resource_key = _source_hash(_normalized(web_link.get("url")), title)
    if not role or not resource_key:
        return None
    timestamp = time.time() if now is None else now
    receipt_id = "rec-" + hashlib.sha256(
        f"{turn_id}|{resource_key}".encode("utf-8")
    ).hexdigest()[:16]
    receipt = RecommendationReceipt(
        receipt_id=receipt_id,
        turn_id=str(turn_id),
        resource_key=resource_key,
        source_key=source_key_for_candidate(web_link),
        title=title,
        primary_topic=classify_candidate(web_link).primary_topic,
        delivered_at=timestamp,
        evidence_snapshot=capture_evidence_snapshot(memory_context, master_name),
    )
    active = deque(
        (item for item in _receipts.get(role, ()) if item.receipt_id != receipt_id),
        maxlen=_MAX_RECEIPTS_PER_ROLE,
    )
    active.append(receipt)
    _receipts[role] = active
    return receipt


def get_pending_receipts(
    role: str, *, now: float | None = None
) -> tuple[RecommendationReceipt, ...]:
    timestamp = time.time() if now is None else now
    active = deque(
        (
            receipt
            for receipt in _receipts.get(role, ())
            if timestamp - receipt.delivered_at <= _RECEIPT_TTL_SECONDS
        ),
        maxlen=_MAX_RECEIPTS_PER_ROLE,
    )
    if active:
        _receipts[role] = active
    else:
        _receipts.pop(role, None)
    return tuple(active)


def format_feedback_receipts(role: str, *, now: float | None = None) -> str:
    payload = [
        {
            "receipt_id": receipt.receipt_id,
            "title": receipt.title,
            "delivered_at": int(receipt.delivered_at),
        }
        for receipt in get_pending_receipts(role, now=now)
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload else ""


def _find_new_evidence_id(
    receipt: RecommendationReceipt,
    evidence: str,
    *,
    memory_context: str,
    master_name: str,
) -> str | None:
    folded_evidence = evidence.casefold()
    seen: Counter[str] = Counter()
    match: str | None = None
    for line in _user_history_lines(memory_context, master_name):
        fingerprint = _line_fingerprint(line)
        seen[fingerprint] += 1
        if (
            folded_evidence in line.casefold()
            and seen[fingerprint] > receipt.evidence_snapshot.get(fingerprint, 0)
        ):
            match = f"{fingerprint}:{seen[fingerprint]}"
    return match


def _remove_receipt(role: str, receipt_id: str) -> None:
    remaining = deque(
        (item for item in _receipts.get(role, ()) if item.receipt_id != receipt_id),
        maxlen=_MAX_RECEIPTS_PER_ROLE,
    )
    if remaining:
        _receipts[role] = remaining
    else:
        _receipts.pop(role, None)


def _active_topic_evidence(role: str, now: float) -> deque[TopicEvidence]:
    active = deque(
        (item for item in _topic_evidence.get(role, ()) if item.expires_at >= now),
        maxlen=_MAX_TOPIC_EVIDENCE_PER_ROLE,
    )
    if active:
        _topic_evidence[role] = active
    else:
        _topic_evidence.pop(role, None)
    return active


def _apply_topic_evidence(
    role: str,
    feedback: RecommendationFeedback,
    *,
    direction: int,
    now: float,
) -> bool:
    if feedback.primary_topic is None:
        return False
    active = _active_topic_evidence(role, now)
    active.append(
        TopicEvidence(
            topic=feedback.primary_topic,
            direction=direction,
            confidence=feedback.confidence,
            resource_key=feedback.resource_key,
            evidence_id=feedback.evidence_id,
            expires_at=now + _TOPIC_EVIDENCE_TTL_SECONDS,
        )
    )
    _topic_evidence[role] = active
    latest_pair: list[TopicEvidence] = []
    seen_resources: set[str] = set()
    for item in reversed(active):
        if (
            item.topic == feedback.primary_topic
            and item.direction == direction
            and item.resource_key not in seen_resources
        ):
            latest_pair.append(item)
            seen_resources.add(item.resource_key)
            if len(latest_pair) == 2:
                break
    if len(latest_pair) < 2:
        return False
    pair = tuple(reversed(latest_pair))
    correction = TopicCorrection(
        topic=feedback.primary_topic,
        score=direction * sum(item.confidence for item in pair) / 2.0,
        resource_keys=(pair[0].resource_key, pair[1].resource_key),
        expires_at=now + _CORRECTION_TTL_SECONDS,
    )
    _topic_corrections.setdefault(role, {})[feedback.primary_topic] = correction
    return True


def process_recommendation_feedback(
    role: str,
    raw: Any,
    *,
    memory_context: str,
    master_name: str,
    now: float | None = None,
) -> FeedbackProcessResult:
    """Validate one Phase 1 feedback object and update only receipt-derived state."""
    if not isinstance(raw, Mapping):
        return FeedbackProcessResult(False)
    timestamp = time.time() if now is None else now
    receipt_id = str(raw.get("receipt_id", ""))
    reaction = str(raw.get("reaction", ""))
    receipt = next(
        (item for item in get_pending_receipts(role, now=timestamp) if item.receipt_id == receipt_id),
        None,
    )
    if receipt is None or reaction not in REACTIONS:
        return FeedbackProcessResult(False)
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError, OverflowError):
        return FeedbackProcessResult(False)
    evidence = _normalized(raw.get("evidence"))
    if confidence < 0.6 or confidence > 1.0 or not 1 <= len(evidence) <= _MAX_EVIDENCE_CHARS:
        return FeedbackProcessResult(False)
    evidence_id = _find_new_evidence_id(
        receipt,
        evidence,
        memory_context=memory_context,
        master_name=master_name,
    )
    if evidence_id is None:
        return FeedbackProcessResult(False)
    dedupe_key = (role, receipt_id, reaction, evidence_id)
    if dedupe_key in _processed_feedback:
        return FeedbackProcessResult(False)
    _processed_feedback.add(dedupe_key)
    _remove_receipt(role, receipt_id)
    feedback = RecommendationFeedback(
        receipt_id=receipt_id,
        reaction=reaction,
        confidence=confidence,
        evidence_id=evidence_id,
        resource_key=receipt.resource_key,
        source_key=receipt.source_key,
        primary_topic=receipt.primary_topic,
    )

    changed = False
    if reaction == "positive":
        changed = _apply_topic_evidence(role, feedback, direction=1, now=timestamp)
    elif reaction == "not_interested":
        changed = _apply_topic_evidence(role, feedback, direction=-1, now=timestamp)
    elif reaction == "source_distrust" and feedback.source_key:
        _source_suppressions.setdefault(role, {})[feedback.source_key] = (
            timestamp + _SOURCE_SUPPRESSION_TTL_SECONDS
        )
        changed = True
    # quality_issue, temporary_skip and unclear deliberately create no state.
    return FeedbackProcessResult(True, reaction, changed)


def get_topic_scores(role: str, *, now: float | None = None) -> dict[str, float]:
    timestamp = time.time() if now is None else now
    active = {
        topic: correction
        for topic, correction in _topic_corrections.get(role, {}).items()
        if correction.expires_at >= timestamp
    }
    if active:
        _topic_corrections[role] = active
    else:
        _topic_corrections.pop(role, None)
    _active_topic_evidence(role, timestamp)
    return {
        f"topic.{topic}": round(correction.score, 4)
        for topic, correction in active.items()
    }


def get_source_suppressions(role: str, *, now: float | None = None) -> frozenset[str]:
    timestamp = time.time() if now is None else now
    active = {
        source: expires_at
        for source, expires_at in _source_suppressions.get(role, {}).items()
        if expires_at >= timestamp
    }
    if active:
        _source_suppressions[role] = active
    else:
        _source_suppressions.pop(role, None)
    return frozenset(active)


def is_candidate_source_suppressed(
    role: str, item: Mapping[str, Any], *, now: float | None = None
) -> bool:
    source_key = source_key_for_candidate(item)
    return bool(source_key and source_key in get_source_suppressions(role, now=now))


def clear_recommendation_feedback_state() -> None:
    _receipts.clear()
    _topic_evidence.clear()
    _topic_corrections.clear()
    _source_suppressions.clear()
    _processed_feedback.clear()


def _candidate_pool(item: Mapping[str, Any], topic: CandidateTopic) -> str:
    task = _normalized(item.get("_phase1_task"))
    mode = _normalized(item.get("mode")) or "web"
    if task:
        return f"task/{task}"
    return f"{('topic.' + topic.primary_topic) if topic.primary_topic else 'untagged'}/{mode}"


def _candidate_affinity(topic: CandidateTopic, scores: Mapping[str, float]) -> float:
    return scores.get(f"topic.{topic.primary_topic}", 0.0) if topic.primary_topic else 0.0


def calculate_pool_probabilities(
    candidates: Sequence[Mapping[str, Any]],
    scores: Mapping[str, float],
    *,
    exploration: float = 0.15,
    source_weights: Mapping[str, float] | None = None,
) -> dict[str, float]:
    pools: dict[str, tuple[CandidateTopic, list[float]]] = {}
    fallback = 1.0 / max(1, len(source_weights or {}))
    for item in candidates:
        topic = classify_candidate(item)
        pool = _candidate_pool(item, topic)
        source_prior = max(
            0.0,
            float((source_weights or {}).get(str(item.get("mode", "")), fallback)),
        ) if source_weights else 1.0
        pools.setdefault(pool, (topic, []))[1].append(source_prior)
    if not pools:
        return {}
    raw = {
        pool: max(sum(priors) / len(priors), 1e-9)
        * math.exp(max(-4.0, min(4.0, _candidate_affinity(topic, scores))))
        for pool, (topic, priors) in pools.items()
    }
    total = sum(raw.values())
    uniform = exploration / len(pools)
    return {
        pool: (1.0 - exploration) * value / total + uniform
        for pool, value in raw.items()
    }


def select_preference_candidate_batch(
    candidates: Sequence[dict[str, Any]],
    scores: Mapping[str, float],
    *,
    total: int,
    rng: random.Random | None = None,
    source_weights: Mapping[str, float] | None = None,
) -> PreferenceCandidateSelection:
    """Select from already hard-filtered candidates with a fixed exploration slice."""
    ordered = list(candidates)
    if total <= 0 or not ordered:
        return PreferenceCandidateSelection([], 0, 0)
    effective_total = min(total, len(ordered))
    if not scores and not source_weights:
        return PreferenceCandidateSelection(ordered[:effective_total], 0, 0)
    randomizer = rng or random.Random()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in ordered:
        topic = classify_candidate(item)
        grouped.setdefault(_candidate_pool(item, topic), []).append(item)
    probabilities = calculate_pool_probabilities(
        ordered, scores, exploration=0.0, source_weights=source_weights
    )
    exploration_slots = min(effective_total, max(1, math.ceil(effective_total * 0.15)))
    personalized_target = effective_total - exploration_slots
    selected: list[dict[str, Any]] = []
    available_pools = list(grouped)
    while len(selected) < personalized_target and available_pools:
        weights = [probabilities.get(pool, 0.0) for pool in available_pools]
        if not any(weights):
            break
        pool = randomizer.choices(available_pools, weights=weights, k=1)[0]
        selected.append(grouped[pool].pop(0))
        if not grouped[pool]:
            available_pools.remove(pool)
    selected_ids = {id(item) for item in selected}
    remaining = [item for item in ordered if id(item) not in selected_ids]
    personalized_slots = len(selected)
    while len(selected) < effective_total and remaining:
        remaining_by_pool: dict[str, list[dict[str, Any]]] = {}
        for item in remaining:
            topic = classify_candidate(item)
            remaining_by_pool.setdefault(_candidate_pool(item, topic), []).append(item)
        exploration_pool = randomizer.choice(list(remaining_by_pool))
        item = randomizer.choice(remaining_by_pool[exploration_pool])
        remaining.remove(item)
        selected.append(item)
    return PreferenceCandidateSelection(
        selected,
        personalized_slots,
        len(selected) - personalized_slots,
    )


def select_preference_candidates(
    candidates: Sequence[dict[str, Any]],
    scores: Mapping[str, float],
    *,
    total: int,
    rng: random.Random | None = None,
    source_weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    return select_preference_candidate_batch(
        candidates,
        scores,
        total=total,
        rng=rng,
        source_weights=source_weights,
    ).items


def blend_source_weights(
    base_weights: Mapping[str, float],
    scores: Mapping[str, float],
    *,
    exploration: float = 0.15,
) -> dict[str, float]:
    """Topic feedback must not create channel-level user preferences."""
    return dict(base_weights)


def run_demo() -> dict[str, Any]:
    """Run the fixed Web-only feedback loop without an LLM call or thread."""
    clear_recommendation_feedback_state()
    role = "DemoNeko"
    master = "主人"
    game_a = {
        "title": "独立游戏试玩记录", "url": "https://a.example/game-1",
        "source": "A", "mode": "video",
    }
    game_b = {
        "title": "主机游戏攻略", "url": "https://b.example/game-2",
        "source": "B", "mode": "video",
    }
    candidates = [
        game_a,
        game_b,
        {"title": "Python 开源周报", "url": "https://c.example/python", "source": "C", "mode": "news"},
        {"title": "天文观测新发现", "url": "https://d.example/space", "source": "D", "mode": "news"},
        {"title": "电影票房观察", "url": "https://e.example/film", "source": "E", "mode": "news"},
        {"title": "猫咪宠物日常", "url": "https://f.example/cat", "source": "F", "mode": "video"},
    ]
    first = register_recommendation_receipt(
        role, turn_id="turn-1", web_link=game_a,
        memory_context="主人 | 今天天气不错", master_name=master, now=0.0,
    )
    assert first is not None
    first_result = process_recommendation_feedback(
        role,
        {"receipt_id": first.receipt_id, "reaction": "not_interested", "confidence": 0.9, "evidence": "这类游戏我没兴趣"},
        memory_context="主人 | 今天天气不错\n主人 | 这类游戏我没兴趣",
        master_name=master,
        now=60.0,
    )
    scores_after_first_phase1 = get_topic_scores(role, now=60.0)
    second = register_recommendation_receipt(
        role, turn_id="turn-2", web_link=game_b,
        memory_context="主人 | 今天天气不错\n主人 | 这类游戏我没兴趣",
        master_name=master, now=120.0,
    )
    assert second is not None
    second_result = process_recommendation_feedback(
        role,
        {"receipt_id": second.receipt_id, "reaction": "not_interested", "confidence": 0.8, "evidence": "还是不想看游戏"},
        memory_context="主人 | 这类游戏我没兴趣\n主人 | 还是不想看游戏",
        master_name=master,
        now=180.0,
    )
    scores_after_second_phase1 = get_topic_scores(role, now=180.0)
    before = calculate_pool_probabilities(candidates, {})
    after = calculate_pool_probabilities(candidates, scores_after_second_phase1)
    source_weights = {"video": 0.5, "news": 0.5}
    top_k_before = select_preference_candidates(
        candidates,
        {},
        total=3,
        rng=random.Random(1),
        source_weights=source_weights,
    )
    top_k_after = select_preference_candidates(
        candidates,
        scores_after_second_phase1,
        total=3,
        rng=random.Random(1),
        source_weights=source_weights,
    )
    return {
        "new_llm_calls": 0,
        "new_llm_threads": 0,
        "first_feedback_accepted": first_result.accepted,
        "second_feedback_accepted": second_result.accepted,
        "scores_after_first_phase1": scores_after_first_phase1,
        "scores_after_second_phase1": scores_after_second_phase1,
        "game_probability_before": sum(value for pool, value in before.items() if "topic.games" in pool),
        "game_probability_after": sum(value for pool, value in after.items() if "topic.games" in pool),
        "top_k_before_correction": [item["title"] for item in top_k_before],
        "top_k_after_correction": [item["title"] for item in top_k_after],
        "music_meme_behavior_changed": False,
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
