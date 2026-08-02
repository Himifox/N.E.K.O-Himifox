"""Pure reward contracts derived from sanitized feedback events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .events import (
    _clean_text,
    _number,
    sanitize_recommendation_feedback_event,
)

REWARD_SCORE_V2_PREVIEW_VERSION = "reward_score_v2_preview_v2"

BANDIT_ENCOUNTER_REWARD_VERSION = "bandit_encounter_reward_v1"

REPLY_SPEED_BONUS_MAX = 0.05

_SOURCE_REJECTION_SCORE = -0.35

_SOURCE_FATIGUE_SCORE = -0.20

_CANDIDATE_REJECTION_SCORE = -0.10

_REWARD_V2_PREVIEW_EVENT_COMPONENTS: dict[str, tuple[str, float]] = {
    "user_reply_fast": ("reply", 0.20),
    "user_reply": ("reply", 0.20),
    "user_continue": ("continue", 0.35),
    "ignored": ("interrupt", -0.05),
    "proactive_disabled_after": ("settings", -0.70),
    "source_disabled_after": ("settings", -0.35),
    "source_not_interested": ("settings", _SOURCE_REJECTION_SCORE),
    "source_fatigue": ("settings", _SOURCE_FATIGUE_SCORE),
    "candidate_not_interested": ("settings", _CANDIDATE_REJECTION_SCORE),
    "source_interested": ("settings", 0.35),
    "music_played_through": ("consumption", 0.90),
    "music_high_completion": ("consumption", 0.65),
    "music_mid_completion": ("consumption", 0.25),
    "music_normal_close": ("consumption", 0.05),
    "music_early_close": ("consumption", -0.35),
    "music_hard_skip": ("consumption", -0.70),
    "music_not_started": ("consumption", 0.00),
    "music_error": ("consumption", 0.00),
    "autoplay_blocked": ("consumption", 0.00),
    "mini_game_accept": ("interaction", 0.90),
    "mini_game_later": ("interaction", 0.20),
    "mini_game_decline": ("interaction", -0.35),
    "mini_game_ignored": ("interaction", -0.05),
}

_REWARD_V2_PREVIEW_COMPONENT_ORDER = (
    "reply",
    "continue",
    "consumption",
    "relative_speed",
    "interrupt",
    "settings",
    "interaction",
)

_REWARD_V2_PREVIEW_TECHNICAL_ZERO_EVENTS = {
    "music_error",
    "autoplay_blocked",
}

_REWARD_V2_PREVIEW_REPLY_EVENTS = {"user_reply_fast", "user_reply"}


def reward_event_score(event_type: str) -> float:
    """Return the state-learning score for one recognized feedback event."""
    component = _REWARD_V2_PREVIEW_EVENT_COMPONENTS.get(event_type)
    return float(component[1]) if component is not None else 0.0


def build_reward_score_v2_preview(
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    feedback_inferred: bool = False,
    relative_speed_preview: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-production reward preview for one turn.

    Events are deduplicated by event type.  When multiple events affect the
    same component, the strongest absolute signal wins, except that reply and
    continuation remain separate components.  An optional point-in-time speed
    preview may add a small non-negative bonus.  The result is never consumed
    by ranking or tuning.
    """
    event_types: list[str] = []
    seen_event_types: set[str] = set()
    components = {component: 0.0 for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER}
    recognized_event_types: list[str] = []
    technical_zero_events: list[str] = []
    unknown_events: list[str] = []

    for raw_event in feedback_events:
        if not isinstance(raw_event, Mapping):
            continue
        event = sanitize_recommendation_feedback_event(raw_event)
        event_type = _clean_text(event.get("event_type")) or "unknown"
        if event_type in seen_event_types:
            continue
        seen_event_types.add(event_type)
        event_types.append(event_type)

        component_score = _REWARD_V2_PREVIEW_EVENT_COMPONENTS.get(event_type)
        if component_score is None:
            unknown_events.append(event_type)
            continue
        recognized_event_types.append(event_type)
        component, score = component_score
        previous = float(components.get(component, 0.0))
        if abs(float(score)) > abs(previous):
            components[component] = float(score)
        if event_type in _REWARD_V2_PREVIEW_TECHNICAL_ZERO_EVENTS:
            technical_zero_events.append(event_type)

    has_reply = any(
        event_type in _REWARD_V2_PREVIEW_REPLY_EVENTS for event_type in event_types
    )
    relative_speed_status = "not_applicable"
    relative_speed_baseline_sample_count = 0
    if has_reply:
        relative_speed_status = "pending_personal_baseline"
        if isinstance(relative_speed_preview, Mapping):
            relative_speed_status = (
                _clean_text(relative_speed_preview.get("status"))
                or "pending_personal_baseline"
            )
            relative_speed_baseline_sample_count = max(
                0,
                int(_number(relative_speed_preview.get("baseline_sample_count"), 0.0)),
            )
            components["relative_speed"] = _clamp(
                _number(relative_speed_preview.get("bonus"), 0.0),
                0.0,
                REPLY_SPEED_BONUS_MAX,
            )
    reward = _clamp(sum(components.values()), -1.0, 1.0)
    return {
        "version": REWARD_SCORE_V2_PREVIEW_VERSION,
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "personalization_state_consumed": False,
        "reward_score_v2_preview": (
            round(reward, 3) if recognized_event_types else None
        ),
        "components": {
            component: round(float(components[component]), 3)
            for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER
        },
        "event_types": event_types,
        "recognized_event_types": recognized_event_types,
        "feedback_inferred": bool(feedback_inferred),
        "relative_speed_status": relative_speed_status,
        "relative_speed_baseline_sample_count": (relative_speed_baseline_sample_count),
        "technical_zero_event_types": technical_zero_events,
        "unknown_event_types": unknown_events,
    }


def build_bandit_encounter_reward(
    feedback_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Promote the existing v2 rule score through a versioned Bandit contract."""
    preview = build_reward_score_v2_preview(feedback_events)
    recognized = tuple(preview.get("recognized_event_types") or ())
    signal_events = tuple(
        event_type
        for event_type in recognized
        if event_type not in _REWARD_V2_PREVIEW_TECHNICAL_ZERO_EVENTS
        and abs(float(_REWARD_V2_PREVIEW_EVENT_COMPONENTS[event_type][1])) > 0.0
    )
    reward = preview.get("reward_score_v2_preview")
    eligible = bool(signal_events and isinstance(reward, (int, float)))
    return {
        "version": BANDIT_ENCOUNTER_REWARD_VERSION,
        "rule_score_version": REWARD_SCORE_V2_PREVIEW_VERSION,
        "eligible": eligible,
        "reward": float(reward) if eligible else None,
        "event_types": list(recognized),
        "signal_event_types": list(signal_events),
        "excluded_reason": None if eligible else "no_nontechnical_reward_signal",
    }


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
