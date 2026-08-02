"""Feedback event contracts, sanitization, and music event mapping."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import time
from typing import Any

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

_SOURCE_ALIASES = {
    "web": "web",
    "news": "news",
    "home": "web",
    "personal": "personal",
    "video": "video",
    "music": "music",
    "meme": "meme",
    "topic_hook": "topic_hook",
    "vision": "vision",
    "window": "window",
    "mini_game": "mini_game",
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
            "lanlan_name": _clean_text(lanlan_name),
            "turn_id": _clean_text(turn_id),
            "source_type": _normalize_source_type(source_type),
            "candidate_id": _clean_text(candidate_id) or None,
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
            safe[key] = _json_safe_scalar(event.get(key))
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
        safe[key] = _json_safe_scalar(metadata.get(key))
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
    active = _number(active_playback_ms, -1.0)
    wall = _number(played_wall_ms, -1.0)
    played_ms = active if math.isfinite(active) and active >= 0 else wall
    if math.isfinite(played_ms) and 0 <= played_ms <= 10_000:
        return "music_hard_skip"
    if math.isfinite(played_ms) and 0 <= played_ms < 30_000:
        return "music_early_close"
    ratio = _number(completion_ratio, -1.0)
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


def _normalize_source_type(value: Any) -> str:
    raw = _clean_text(value).lower()
    return _SOURCE_ALIASES.get(raw, raw or "unknown")


def _json_safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe_scalar(item) for item in value]
    return str(value)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
