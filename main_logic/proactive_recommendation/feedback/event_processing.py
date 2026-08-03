"""Feedback event contracts, sanitization, and music event mapping."""

from __future__ import annotations


from collections.abc import Mapping, Sequence

import math

import time

from typing import Any

from difflib import SequenceMatcher

import re

from ..normalization import (
    coerce_float_or_default,
    normalize_source_identifier,
    sanitize_json_value,
    to_stripped_text,
)


FEEDBACK_SCORE_VERSION = "report_score_v1"

_SOURCE_REJECTION_SCORE = -0.35

_SOURCE_FATIGUE_SCORE = -0.20

_CANDIDATE_REJECTION_SCORE = -0.10

_FEEDBACK_EVENT_SCORES: dict[str, tuple[str, float, str]] = {
    "user_reply_fast": ("generic_engagement", 0.25, "medium"),
    "user_reply": ("generic_engagement", 0.15, "medium"),
    "user_continue": ("generic_engagement", 0.35, "medium"),
    "ignored": ("generic_engagement", -0.05, "low"),
    "proactive_disabled_after": ("settings", -0.70, "high"),
    "source_disabled_after": ("settings", -0.35, "medium"),
    "source_not_interested": ("source_preference", _SOURCE_REJECTION_SCORE, "high"),
    "source_fatigue": ("source_preference", _SOURCE_FATIGUE_SCORE, "medium"),
    "candidate_not_interested": (
        "source_preference",
        _CANDIDATE_REJECTION_SCORE,
        "low",
    ),
    "source_interested": ("source_preference", 0.35, "high"),
    "music_played_through": ("music", 0.90, "high"),
    "music_high_completion": ("music", 0.65, "high"),
    "music_mid_completion": ("music", 0.25, "medium"),
    "music_normal_close": ("music", 0.05, "low"),
    "music_early_close": ("music", -0.35, "medium"),
    "music_hard_skip": ("music", -0.70, "high"),
    "music_not_started": ("music", 0.00, "low"),
    "music_error": ("music", 0.00, "low"),
    "autoplay_blocked": ("music", 0.00, "low"),
    "mini_game_accept": ("mini_game", 0.90, "high"),
    "mini_game_later": ("mini_game", 0.20, "medium"),
    "mini_game_decline": ("mini_game", -0.35, "high"),
    "mini_game_ignored": ("mini_game", -0.05, "low"),
}

_TOP_LEVEL_KEYS = {
    "ts",
    "lanlan_name",
    "turn_id",
    "source_type",
    "candidate_id",
    "event_type",
    "event_group",
    "report_score_v1",
    "confidence",
    "metadata",
    "score_version",
}

_METADATA_KEYS = {
    "reply_latency_seconds",
    "reply_length",
    "reply_length_bucket",
    "played_wall_ms",
    "active_playback_ms",
    "audio_current_time_sec",
    "audio_duration_sec",
    "completion_ratio",
    "mini_game_choice",
    "game_type",
    "reason",
    "attribution_basis",
}

_MAX_PLAYBACK_FEEDBACK_MS = 24 * 60 * 60 * 1000

_FORBIDDEN_EVENT_KEYS = {
    "payload",
    "source_links",
    "raw_data",
    "screenshot",
    "screenshot_b64",
    "prompt",
    "messages",
    "chat_text",
    "raw_text",
    "text",
}


def has_forbidden_feedback_fields(payload: Mapping[str, Any]) -> bool:
    return _contains_forbidden_keys(payload)


def build_feedback_event(
    *,
    lanlan_name: Any,
    turn_id: Any,
    event_type: str,
    source_type: Any = None,
    candidate_id: Any = None,
    metadata: Mapping[str, Any] | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """Build one sanitized feedback event with report-only v1 score fields."""
    event_name = str(event_type or "").strip()
    event_group, score, confidence = _FEEDBACK_EVENT_SCORES.get(
        event_name,
        ("unknown", 0.0, "low"),
    )
    return sanitize_recommendation_feedback_event(
        {
            "ts": time.time() if ts is None else float(ts),
            "lanlan_name": to_stripped_text(lanlan_name),
            "turn_id": to_stripped_text(turn_id),
            "source_type": normalize_feedback_source_identifier(source_type),
            "candidate_id": to_stripped_text(candidate_id) or None,
            "event_type": event_name,
            "event_group": event_group,
            "report_score_v1": round(float(score), 3),
            "confidence": confidence,
            "metadata": sanitize_feedback_metadata(metadata or {}),
            "score_version": FEEDBACK_SCORE_VERSION,
        }
    )


def sanitize_recommendation_feedback_event(event: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in _TOP_LEVEL_KEYS:
        if key not in event:
            continue
        if key == "metadata":
            safe[key] = sanitize_feedback_metadata(event.get(key))
        else:
            safe[key] = sanitize_json_value(event.get(key))
    return safe


def sanitize_feedback_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in _METADATA_KEYS:
        if key not in metadata:
            continue
        if key == "active_playback_ms":
            value = metadata.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                numeric_value = float(value)
            except (OverflowError, TypeError, ValueError):
                continue
            if (
                not math.isfinite(numeric_value)
                or not 0 <= numeric_value <= _MAX_PLAYBACK_FEEDBACK_MS
            ):
                continue
        safe[key] = sanitize_json_value(metadata.get(key))
    return safe


def music_feedback_event_type(
    *,
    active_playback_ms: Any = None,
    played_wall_ms: Any = None,
    completion_ratio: Any = None,
    started: bool = True,
    error: bool = False,
    autoplay_blocked: bool = False,
    played_through: bool = False,
) -> str:
    if played_through:
        return "music_played_through"
    if error:
        return "music_error"
    if autoplay_blocked:
        return "autoplay_blocked"
    if not started:
        return "music_not_started"
    active = coerce_float_or_default(active_playback_ms, default=-1.0)
    wall = coerce_float_or_default(played_wall_ms, default=-1.0)
    played_ms = active if math.isfinite(active) and active >= 0 else wall
    if math.isfinite(played_ms) and 0 <= played_ms <= 10_000:
        return "music_hard_skip"
    if math.isfinite(played_ms) and 0 <= played_ms < 30_000:
        return "music_early_close"
    ratio = coerce_float_or_default(completion_ratio, default=-1.0)
    if ratio >= 0.70:
        return "music_high_completion"
    if ratio >= 0.30:
        return "music_mid_completion"
    return "music_normal_close"


def _contains_forbidden_keys(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _FORBIDDEN_EVENT_KEYS:
                return True
            if _contains_forbidden_keys(item):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_forbidden_keys(item) for item in value)
    return False


def normalize_feedback_source_identifier(value: Any) -> str:
    """Normalize a feedback source while preserving the historical fallback."""
    return normalize_source_identifier(value) or "unknown"


_EXPLICIT_TEXT_PREFERENCE_SOURCE_ALIASES = {
    "news": ("新闻", "新聞", "资讯", "資訊", "news"),
    "meme": ("表情包", "梗图", "梗圖", "meme", "sticker"),
    "vision": ("屏幕内容", "屏幕信息", "窗口内容", "螢幕內容", "視窗內容", "vision"),
    "video": ("视频", "影片", "video"),
}

_EXPLICIT_TEXT_SOURCE_REJECTION_MARKERS = (
    "少推荐",
    "少推薦",
    "别推荐",
    "別推薦",
    "不要推荐",
    "不要推薦",
    "别发",
    "別發",
    "不要发",
    "不要發",
    "不喜欢",
    "不喜歡",
    "不感兴趣",
    "不感興趣",
    "不太感兴趣",
    "不太感興趣",
    "没兴趣",
    "沒興趣",
    "不想看",
    "stop recommending",
    "stop sending",
    "show me less",
    "not interested",
    "don't recommend",
    "do not recommend",
)

_EXPLICIT_TEXT_QUALITY_NEGATIVE_MARKERS = (
    "不好看",
    "不好笑",
    "没意思",
    "沒意思",
    "无聊",
    "無聊",
    "不行",
    "没用",
    "沒用",
    "不相关",
    "不相關",
    "boring",
    "not useful",
    "irrelevant",
)

_EXPLICIT_TEXT_POSITIVE_MARKERS = (
    "多推荐",
    "多推薦",
    "多发",
    "多發",
    "再来点",
    "再來點",
    "喜欢",
    "喜歡",
    "感兴趣",
    "感興趣",
    "想看",
    "爱看",
    "愛看",
    "show me more",
    "more like this",
    "i like",
    "interested in",
)

_EXPLICIT_TEXT_DEICTIC_NEGATIVE = (
    "少推荐这",
    "少推薦這",
    "别推荐这",
    "別推薦這",
    "不要推荐这",
    "不要推薦這",
    "不想看这类",
    "不想看這類",
    "不喜欢这类内容",
    "不喜歡這類內容",
    "不喜欢这种内容",
    "不喜歡這種內容",
)

_EXPLICIT_TEXT_DEICTIC_POSITIVE = (
    "多推荐这",
    "多推薦這",
    "想看这类",
    "想看這類",
    "喜欢这类内容",
    "喜歡這類內容",
    "喜欢这种内容",
    "喜歡這種內容",
)

_EXPLICIT_TEXT_NEGATION_EXCEPTIONS = (
    "不无聊",
    "不無聊",
    "不是没意思",
    "不是沒意思",
    "不是不喜欢",
    "不是不喜歡",
    "没觉得无聊",
    "沒覺得無聊",
    "别不推荐",
    "別不推薦",
)

_EXPLICIT_TEXT_DEICTIC_RE = re.compile(
    r"(?:这个|這個|这种|這種|这类|這類|这条|這條|这张|這張|刚才那个|剛才那個)"
)

_EXPLICIT_TEXT_SWITCH_RE = re.compile(
    r"(?:换一个|換一個|换个|換個|下一个|下一個|来点别的|來點別的|"
    r"聊点别的|聊點別的|跳过|跳過|somethingelse|nextone)"
)

_EXPLICIT_TEXT_FATIGUE_RE = re.compile(
    r"(?:又是|老是|怎么还是|怎麼還是|太多了?|重复了?|重複了?|看腻了?|看膩了?)"
)


def _compact_feedback_text(text: str | None) -> str:
    return "".join(
        character
        for character in to_stripped_text(text).casefold()[:256]
        if character.isalnum()
    )


def _contains_feedback_phrase(
    normalized: str,
    phrases: Iterable[str],
    *,
    fuzzy: bool = False,
) -> bool:
    for raw_phrase in phrases:
        phrase = _compact_feedback_text(raw_phrase)
        if not phrase:
            continue
        if phrase in normalized:
            return True
        if not fuzzy or len(phrase) < 3:
            continue
        threshold = 0.66 if len(phrase) == 3 else 0.75 if len(phrase) == 4 else 0.82
        for window_size in range(max(2, len(phrase) - 1), len(phrase) + 2):
            for start in range(0, len(normalized) - window_size + 1):
                window = normalized[start : start + window_size]
                if SequenceMatcher(None, window, phrase).ratio() >= threshold:
                    return True
    return False


def _explicit_text_named_source_types(text: str | None) -> tuple[str, ...]:
    normalized = _compact_feedback_text(text)
    if not normalized:
        return ()
    return tuple(
        source_type
        for source_type, aliases in _EXPLICIT_TEXT_PREFERENCE_SOURCE_ALIASES.items()
        if _contains_feedback_phrase(normalized, aliases, fuzzy=True)
    )


def _explicit_text_source_preference_event_type(
    text: str | None,
    source_type: str,
) -> tuple[str, str] | None:
    aliases = _EXPLICIT_TEXT_PREFERENCE_SOURCE_ALIASES.get(source_type)
    normalized = _compact_feedback_text(text)
    if not aliases or not normalized:
        return None
    if _contains_feedback_phrase(normalized, _EXPLICIT_TEXT_NEGATION_EXCEPTIONS):
        return None
    source_named = _contains_feedback_phrase(normalized, aliases, fuzzy=True)
    deictic = bool(_EXPLICIT_TEXT_DEICTIC_RE.search(normalized))
    if source_named and _contains_feedback_phrase(
        normalized,
        _EXPLICIT_TEXT_SOURCE_REJECTION_MARKERS,
    ):
        return "source_not_interested", "explicit_source_rejection"
    if _contains_feedback_phrase(normalized, _EXPLICIT_TEXT_DEICTIC_NEGATIVE):
        return "source_not_interested", "explicit_source_rejection"
    if (source_named or deictic) and _EXPLICIT_TEXT_FATIGUE_RE.search(normalized):
        return "source_fatigue", "explicit_source_fatigue"
    quality_negative = _contains_feedback_phrase(
        normalized,
        _EXPLICIT_TEXT_QUALITY_NEGATIVE_MARKERS,
    )
    if _EXPLICIT_TEXT_SWITCH_RE.search(normalized) or (
        quality_negative and (source_named or deictic)
    ):
        return "candidate_not_interested", "explicit_candidate_rejection"
    if source_named and _contains_feedback_phrase(
        normalized,
        _EXPLICIT_TEXT_POSITIVE_MARKERS,
    ):
        return "source_interested", "explicit_source_interest"
    if _contains_feedback_phrase(normalized, _EXPLICIT_TEXT_DEICTIC_POSITIVE):
        return "source_interested", "explicit_source_interest"
    return None


def detect_source_feedback_signal(
    text: str | None,
    source_type: str,
) -> tuple[str, str] | None:
    return _explicit_text_source_preference_event_type(text, source_type)
