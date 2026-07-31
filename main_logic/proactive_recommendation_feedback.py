"""Feedback event sink for proactive recommendation observations.

This module records what the user did after a proactive recommendation was
delivered. It keeps raw event types separate from report-only scores so future
calibration can recompute scores without losing the original signal.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
from statistics import median
import time
from typing import Any

from config import (
    PROACTIVE_RECOMMENDATION_BANDIT_MODE,
    PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE,
)
from main_logic.proactive_recommendation_bandit import BANDIT_ARMS
from main_logic.proactive_recommendation_bandit_state import (
    update_recommendation_bandit_reward,
)
from main_logic.proactive_recommendation_preference import (
    update_recommendation_source_preference,
)
from main_logic.proactive_recommendation_feedback_state import (
    update_conversation_acceptance_preview,
    update_source_affinity_preview,
)


logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_feedback")

FEEDBACK_LOG_FILENAME = "proactive_recommendation_feedback.jsonl"
FEEDBACK_SCORE_VERSION = "report_score_v1"
REWARD_SCORE_V2_PREVIEW_VERSION = "reward_score_v2_preview_v2"
BANDIT_ENCOUNTER_REWARD_VERSION = "bandit_encounter_reward_v1"
DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024
REPLY_FAST_SECONDS = 60
REPLY_WINDOW_SECONDS = 10 * 60
REPLY_SPEED_BASELINE_MIN_SAMPLES = 5
REPLY_SPEED_BONUS_MAX = 0.05
REPLY_SPEED_LOG_SCALE_FLOOR = 0.25

_FEEDBACK_EVENT_SCORES: dict[str, tuple[str, float, str]] = {
    "user_reply_fast": ("generic_engagement", 0.25, "medium"),
    "user_reply": ("generic_engagement", 0.15, "medium"),
    "user_continue": ("generic_engagement", 0.35, "medium"),
    "ignored": ("generic_engagement", -0.05, "low"),
    "proactive_disabled_after": ("settings", -0.70, "high"),
    "source_disabled_after": ("settings", -0.35, "medium"),
    "proactive_not_now": ("interrupt", -0.35, "high"),
    "source_not_interested": ("source_preference", -0.35, "high"),
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

# Shadow-only reward semantics.  These values are intentionally separate from
# ``report_score_v1`` so the original event stream remains replayable.  Reply
# speed is neutral until a later, independently reviewed personal baseline is
# available; ``user_reply_fast`` and ``user_reply`` therefore share the same
# base reward in this first G0 preview.
_REWARD_V2_PREVIEW_EVENT_COMPONENTS: dict[str, tuple[str, float]] = {
    "user_reply_fast": ("reply", 0.20),
    "user_reply": ("reply", 0.20),
    "user_continue": ("continue", 0.35),
    "ignored": ("interrupt", -0.05),
    "proactive_disabled_after": ("settings", -0.70),
    "source_disabled_after": ("settings", -0.35),
    "proactive_not_now": ("interrupt", -0.35),
    "source_not_interested": ("settings", -0.35),
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
_CONVERSATION_ACCEPTANCE_EVENT_TYPES = {
    "user_reply_fast",
    "user_reply",
    "user_continue",
    "proactive_disabled_after",
    "proactive_not_now",
}
_SOURCE_AFFINITY_EVENT_TYPES = {
    "source_disabled_after",
    "source_not_interested",
    "source_interested",
    "music_played_through",
    "music_high_completion",
    "music_mid_completion",
    "music_normal_close",
    "music_early_close",
    "music_hard_skip",
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
    "ui_generation",
}
_UI_GENERATIONS = {"dual_scope_v1"}
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
_SETTING_SOURCE_FIELDS = {
    "proactiveNewsChatEnabled": ("news", "web"),
    "proactiveVideoChatEnabled": ("video",),
    "proactivePersonalChatEnabled": ("personal",),
    "proactiveMusicEnabled": ("music",),
    "proactiveMemeEnabled": ("meme",),
    "proactiveMiniGameInviteEnabled": ("mini_game",),
    "proactiveVisionChatEnabled": ("vision", "window"),
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
_WEAK_NEGATIVE_EVENT_TYPES = {"ignored", "mini_game_ignored"}
_MUSIC_PLAYED_THROUGH_EVENT_TYPE = "music_played_through"
_MUSIC_ACTIONABLE_PLAYED_THROUGH_MIN = 3
_MUSIC_ACTIONABLE_AVERAGE_MIN = 0.50


@dataclass(slots=True)
class PendingRecommendationFeedback:
    lanlan_name: str
    turn_id: str
    source_type: str
    candidate_id: str | None = None
    delivered_at: float = field(default_factory=time.time)
    log_mode: str = "off"
    config_dir: str | os.PathLike[str] | None = None
    recommendation_mode: str = "off"
    seen_groups: set[str] = field(default_factory=set)
    seen_event_types: set[str] = field(default_factory=set)
    reply_seen: bool = False
    continue_seen: bool = False
    reward_events: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecommendationFeedbackRecordResult:
    event: dict[str, Any] | None
    logged: bool
    state_updated: bool = False
    feedback_scope: str = "diagnostic_only"
    state_reason: str = "not_logged"
    preference_state_updated: bool = False
    bandit_state_updated: bool = False


_pending_feedback: dict[tuple[str, str], PendingRecommendationFeedback] = {}


def clear_pending_recommendation_feedback() -> None:
    """Test helper: clear in-memory pending feedback state."""
    _pending_feedback.clear()


def consecutive_unanswered_recommendation_deliveries(
    lanlan_name: Any,
    *,
    now: float | None = None,
) -> int:
    """Count newest recommendation deliveries without an explicit user reply.

    This is an observation-only, process-local fatigue signal. It never changes
    delivery behavior and only considers pending turns retained by the existing
    feedback reply window.
    """
    name = _clean_text(lanlan_name)
    if not name:
        return 0
    current = time.time() if now is None else float(now)
    _prune_pending_feedback(now=current)
    pending_rows = sorted(
        (
            pending
            for pending in _pending_feedback.values()
            if pending.lanlan_name == name and pending.delivered_at <= current
        ),
        key=lambda pending: pending.delivered_at,
        reverse=True,
    )
    count = 0
    for pending in pending_rows:
        if pending.reply_seen:
            break
        count += 1
    return count


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
        if key == "ui_generation":
            value = _clean_text(metadata.get(key))
            if value not in _UI_GENERATIONS:
                continue
            safe[key] = value
            continue
        if key == "active_playback_ms":
            value = metadata.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                numeric_value = float(value)
            except (OverflowError, TypeError, ValueError):
                continue
            if not math.isfinite(numeric_value) or not 0 <= numeric_value <= _MAX_PLAYBACK_FEEDBACK_MS:
                continue
        safe[key] = _json_safe_scalar(metadata.get(key))
    return safe


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
    components = {
        component: 0.0
        for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER
    }
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
        event_type in _REWARD_V2_PREVIEW_REPLY_EVENTS
        for event_type in event_types
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
        "relative_speed_baseline_sample_count": (
            relative_speed_baseline_sample_count
        ),
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


def append_recommendation_feedback_jsonl(
    event: Mapping[str, Any],
    *,
    log_mode: str = "off",
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    rotate_bytes: int = DEFAULT_ROTATE_BYTES,
) -> bool:
    if log_mode != "jsonl":
        return False
    target = _resolve_feedback_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target, rotate_bytes=rotate_bytes)
        safe = sanitize_recommendation_feedback_event(event)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        logger.debug("proactive recommendation feedback append failed: %s", exc)
        return False


def load_recommendation_feedback_jsonl(
    path: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    rows: deque[dict[str, Any]] | list[dict[str, Any]]
    rows = deque(maxlen=limit) if limit and limit > 0 else []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, Mapping):
                    rows.append(sanitize_recommendation_feedback_event(item))
    except Exception as exc:
        logger.debug("proactive recommendation feedback read failed: %s", exc)
        return []
    return list(rows)


def register_pending_feedback_from_observation(
    observation: Mapping[str, Any],
    *,
    log_mode: str = "off",
    config_dir: str | os.PathLike[str] | None = None,
) -> PendingRecommendationFeedback | None:
    if not isinstance(observation, Mapping) or observation.get("delivered") is not True:
        return None
    lanlan_name = _clean_text(observation.get("lanlan_name"))
    turn_id = _clean_text(observation.get("turn_id"))
    if not lanlan_name or not turn_id:
        return None
    matched_material = observation.get("matched_actual_material") is True
    active_preference = observation.get("active_bias_applied") is True
    policy = observation.get("policy_decision")
    policy_v2 = (
        policy
        if isinstance(policy, Mapping)
        and policy.get("context_version")
        in {"source-context-v2", "source-context-v3", "source-context-v4"}
        else None
    )
    if policy_v2 is not None and policy_v2.get("actual_arm"):
        source_type = _normalize_source_type(policy_v2.get("actual_arm"))
        candidate_id = _clean_text(policy_v2.get("actual_candidate_id")) or None
    else:
        source_type = _normalize_source_type(
            observation.get("active_preferred_source_type")
            if matched_material and active_preference
            else observation.get("shadow_selected_source_type")
            if matched_material
            else observation.get("actual_primary_channel")
        )
        candidate_id = None
        if matched_material:
            candidate_id = _clean_text(
                observation.get("active_preferred_candidate_id")
                if active_preference
                else observation.get("shadow_selected_candidate_id")
            ) or None
    return register_pending_feedback(
        lanlan_name=lanlan_name,
        turn_id=turn_id,
        source_type=source_type,
        candidate_id=candidate_id,
        delivered_at=_number(observation.get("ts"), time.time()),
        log_mode=log_mode,
        config_dir=config_dir,
        recommendation_mode=_clean_text(observation.get("recommendation_mode")),
    )


def register_pending_feedback(
    *,
    lanlan_name: Any,
    turn_id: Any,
    source_type: Any,
    candidate_id: Any = None,
    delivered_at: float | None = None,
    log_mode: str = "off",
    config_dir: str | os.PathLike[str] | None = None,
    recommendation_mode: str = "off",
) -> PendingRecommendationFeedback | None:
    name = _clean_text(lanlan_name)
    tid = _clean_text(turn_id)
    if not name or not tid:
        return None
    pending = PendingRecommendationFeedback(
        lanlan_name=name,
        turn_id=tid,
        source_type=_normalize_source_type(source_type),
        candidate_id=_clean_text(candidate_id) or None,
        delivered_at=time.time() if delivered_at is None else float(delivered_at),
        log_mode=log_mode,
        config_dir=config_dir,
        recommendation_mode=_clean_text(recommendation_mode),
    )
    _pending_feedback[(name, tid)] = pending
    _prune_pending_feedback(now=pending.delivered_at)
    return pending


def record_feedback_event(
    *,
    lanlan_name: Any,
    turn_id: Any,
    event_type: str,
    source_type: Any = None,
    candidate_id: Any = None,
    metadata: Mapping[str, Any] | None = None,
    log_mode: str | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    ts: float | None = None,
) -> dict[str, Any] | None:
    return record_feedback_event_with_status(
        lanlan_name=lanlan_name,
        turn_id=turn_id,
        event_type=event_type,
        source_type=source_type,
        candidate_id=candidate_id,
        metadata=metadata,
        log_mode=log_mode,
        config_dir=config_dir,
        ts=ts,
    ).event


def record_feedback_event_with_status(
    *,
    lanlan_name: Any,
    turn_id: Any,
    event_type: str,
    source_type: Any = None,
    candidate_id: Any = None,
    metadata: Mapping[str, Any] | None = None,
    log_mode: str | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    ts: float | None = None,
) -> RecommendationFeedbackRecordResult:
    """Record one event through the shared log, state, and tuning chokepoint."""
    name = _clean_text(lanlan_name)
    tid = _clean_text(turn_id)
    if not name or not tid:
        return RecommendationFeedbackRecordResult(
            event=None, logged=False, state_reason="invalid_identity"
        )
    pending = _pending_feedback.get((name, tid))
    event = build_feedback_event(
        lanlan_name=name,
        turn_id=tid,
        event_type=event_type,
        source_type=source_type if source_type is not None else (pending.source_type if pending else None),
        candidate_id=candidate_id if candidate_id is not None else (pending.candidate_id if pending else None),
        metadata=metadata,
        ts=ts,
    )
    if not event.get("event_type"):
        return RecommendationFeedbackRecordResult(
            event=None, logged=False, state_reason="invalid_event"
        )
    duplicate_event = False
    duplicate_group = False
    if pending is not None:
        duplicate_event = str(event["event_type"]) in pending.seen_event_types
        state_group = _feedback_state_group(event)
        duplicate_group = state_group in pending.seen_groups
        pending.seen_event_types.add(str(event["event_type"]))
        pending.seen_groups.add(state_group)
    effective_log_mode = (
        log_mode if log_mode is not None else (pending.log_mode if pending else "off")
    )
    effective_config_dir = (
        config_dir if config_dir is not None else (pending.config_dir if pending else None)
    )
    wrote = append_recommendation_feedback_jsonl(
        event,
        log_mode=effective_log_mode,
        config_dir=effective_config_dir,
    )
    state_updated = False
    preference_state_updated = False
    bandit_state_updated = False
    feedback_scope = "diagnostic_only"
    state_reason = "not_logged"
    if wrote:
        state_reason = "not_shadow"
        _maybe_auto_apply_tuning_after_feedback(
            config_dir=effective_config_dir,
        )
        if pending is not None and _feedback_state_enabled(pending) and not duplicate_event:
            event_type = str(event.get("event_type") or "")
            if _bandit_event_matches_pending(event, pending):
                pending.reward_events[event_type] = event
                bandit_reward = build_bandit_encounter_reward(
                    pending.reward_events.values()
                )
                reward = bandit_reward.get("reward")
                if bandit_reward.get("eligible") is True and isinstance(
                    reward, (int, float)
                ):
                    try:
                        update_recommendation_bandit_reward(
                            config_dir=effective_config_dir,
                            turn_id=event.get("turn_id"),
                            arm=pending.source_type,
                            reward=reward,
                            event_types=bandit_reward.get("event_types") or (),
                            now=_number(event.get("ts"), time.time()),
                        )
                        bandit_state_updated = True
                    except Exception as exc:
                        logger.debug("recommendation bandit reward update failed: %s", exc)
            component = _REWARD_V2_PREVIEW_EVENT_COMPONENTS.get(event_type)
            score = float(component[1]) if component is not None else 0.0
            persistent_eligible = (
                not duplicate_group
                and event.get("confidence") in {"medium", "high"}
                and score != 0
            )
            try:
                if event_type in _CONVERSATION_ACCEPTANCE_EVENT_TYPES:
                    feedback_scope = "conversation_acceptance"
                    persistent_eligible = (
                        persistent_eligible and event_type != "proactive_not_now"
                    )
                    update_conversation_acceptance_preview(
                        config_dir=effective_config_dir,
                        score=score,
                        persistent_eligible=persistent_eligible,
                        now=_number(event.get("ts"), time.time()),
                    )
                    state_updated = True
                    state_reason = (
                        "temporary_only"
                        if event_type == "proactive_not_now"
                        else "applied"
                    )
                elif (
                    event_type in _SOURCE_AFFINITY_EVENT_TYPES
                    and _source_affinity_event_matches_pending(event, pending)
                ):
                    feedback_scope = "source_affinity"
                    update_source_affinity_preview(
                        config_dir=effective_config_dir,
                        source_type=event.get("source_type"),
                        score=score,
                        persistent_eligible=persistent_eligible,
                        now=_number(event.get("ts"), time.time()),
                    )
                    state_updated = True
                    state_reason = "exact_pending_match"
                elif event_type in _SOURCE_AFFINITY_EVENT_TYPES:
                    feedback_scope = "source_affinity"
                    state_reason = "pending_material_mismatch"
                else:
                    state_reason = "event_not_stateful"
            except Exception as exc:
                logger.debug("feedback state preview update failed: %s", exc)
                state_updated = False
                state_reason = "state_update_failed"
            outcome = source_preference_outcome(event_type)
            if (
                outcome is not None
                and _source_affinity_event_matches_pending(event, pending)
                and not duplicate_event
            ):
                try:
                    update_recommendation_source_preference(
                        config_dir=effective_config_dir,
                        turn_id=event.get("turn_id"),
                        source_type=event.get("source_type"),
                        success=outcome[0],
                        failure=outcome[1],
                        explicit=outcome[2],
                        outcome_strength=max(outcome[0], outcome[1]),
                        now=_number(event.get("ts"), time.time()),
                    )
                    preference_state_updated = True
                except Exception as exc:
                    logger.debug("recommendation preference update failed: %s", exc)
        elif pending is None:
            state_reason = "pending_missing"
        elif duplicate_event:
            state_reason = "duplicate_event"
    return RecommendationFeedbackRecordResult(
        event=event,
        logged=wrote,
        state_updated=state_updated,
        feedback_scope=feedback_scope,
        state_reason=state_reason,
        preference_state_updated=preference_state_updated,
        bandit_state_updated=bandit_state_updated,
    )


def _source_affinity_event_matches_pending(
    event: Mapping[str, Any],
    pending: PendingRecommendationFeedback,
) -> bool:
    """Require an exact delivered source and material before updating affinity."""
    pending_candidate = _clean_text(pending.candidate_id)
    return bool(
        pending_candidate
        and _normalize_source_type(event.get("source_type")) == pending.source_type
        and _clean_text(event.get("candidate_id")) == pending_candidate
    )


def _bandit_event_matches_pending(
    event: Mapping[str, Any],
    pending: PendingRecommendationFeedback,
) -> bool:
    """Bind encounter reward only to the material arm actually delivered."""
    return bool(
        pending.source_type in BANDIT_ARMS
        and pending.candidate_id
        and _normalize_source_type(event.get("source_type")) == pending.source_type
        and _clean_text(event.get("candidate_id")) == pending.candidate_id
    )


def _feedback_state_enabled(pending: PendingRecommendationFeedback) -> bool:
    """Keep learning enabled in Shadow and explicitly personalized active runs."""
    if pending.recommendation_mode == "shadow":
        return True
    return (
        pending.recommendation_mode == "active_source"
        and PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE
        in {"shadow_compare", "active"}
    ) or (
        pending.recommendation_mode == "active_source"
        and PROACTIVE_RECOMMENDATION_BANDIT_MODE in {"shadow", "canary"}
    )


def source_preference_outcome(event_type: str) -> tuple[float, float, bool] | None:
    """Map verified material feedback to one source-level learning outcome."""
    outcomes = {
        "source_interested": (1.0, 0.0, True),
        "source_not_interested": (0.0, 1.0, True),
        "source_disabled_after": (0.0, 1.0, True),
        "music_played_through": (1.0, 0.0, False),
        "music_high_completion": (1.0, 0.0, False),
        "music_mid_completion": (0.5, 0.0, False),
        "music_early_close": (0.0, 1.0, False),
        "music_hard_skip": (0.0, 1.0, False),
    }
    return outcomes.get(event_type)


def _feedback_state_group(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    event_group = str(event.get("event_group") or "unknown")
    if event_type in _CONVERSATION_ACCEPTANCE_EVENT_TYPES:
        return f"conversation:{event_group}"
    if event_type in _SOURCE_AFFINITY_EVENT_TYPES:
        source = _normalize_source_type(event.get("source_type")) or "unknown"
        return f"source:{source}:{event_group}"
    return f"other:{event_group}"


def note_user_turn_for_feedback(
    lanlan_name: str,
    *,
    timestamp: float,
    had_text: bool,
    text_allowed: bool = False,
    text: str | None = None,
) -> dict[str, Any] | None:
    if not had_text:
        return None
    pending = _latest_pending_for_lanlan(lanlan_name, now=timestamp)
    if pending is None:
        return None
    latency = max(0.0, float(timestamp) - float(pending.delivered_at))
    metadata: dict[str, Any] = {"reply_latency_seconds": round(latency, 3)}
    if text_allowed and text:
        metadata["reply_length"] = len(text)
        metadata["reply_length_bucket"] = _reply_length_bucket(len(text))
    if pending.reply_seen and not pending.continue_seen:
        pending.continue_seen = True
        return record_feedback_event(
            lanlan_name=pending.lanlan_name,
            turn_id=pending.turn_id,
            event_type="user_continue",
            metadata=metadata,
            ts=timestamp,
        )
    if not pending.reply_seen:
        pending.reply_seen = True
        event_type = "user_reply_fast" if latency <= REPLY_FAST_SECONDS else "user_reply"
        return record_feedback_event(
            lanlan_name=pending.lanlan_name,
            turn_id=pending.turn_id,
            event_type=event_type,
            metadata=metadata,
            ts=timestamp,
        )
    return None


def record_recent_setting_feedback(
    *,
    lanlan_name: str,
    disabled_fields: Iterable[str],
    log_mode: str = "off",
    config_dir: str | os.PathLike[str] | None = None,
    ts: float | None = None,
) -> list[dict[str, Any]]:
    current = time.time() if ts is None else float(ts)
    pending = _latest_pending_for_lanlan(lanlan_name, now=current)
    if pending is None:
        return []
    disabled = list(disabled_fields)
    if "proactiveChatEnabled" in disabled:
        event = record_feedback_event(
            lanlan_name=pending.lanlan_name,
            turn_id=pending.turn_id,
            event_type="proactive_disabled_after",
            metadata={"reason": "proactiveChatEnabled"},
            log_mode=log_mode or pending.log_mode,
            config_dir=config_dir if config_dir is not None else pending.config_dir,
            ts=current,
        )
        return [event] if event else []
    events: list[dict[str, Any]] = []
    pending_source = _normalize_source_type(pending.source_type)
    for field_name in disabled:
        source_types = _SETTING_SOURCE_FIELDS.get(field_name, ())
        if pending_source and pending_source not in source_types:
            continue
        event = record_feedback_event(
            lanlan_name=pending.lanlan_name,
            turn_id=pending.turn_id,
            event_type="source_disabled_after",
            source_type=pending_source,
            metadata={"reason": field_name},
            log_mode=log_mode or pending.log_mode,
            config_dir=config_dir if config_dir is not None else pending.config_dir,
            ts=current,
        )
        if event:
            events.append(event)
    return events


class ProactiveRecommendationFeedbackTurnSink:
    def note_turn(self, event: Any) -> None:
        try:
            if getattr(event, "actor", None) != "user":
                return
            note_user_turn_for_feedback(
                str(getattr(event, "lanlan_name", "") or ""),
                timestamp=float(getattr(event, "timestamp", time.time())),
                had_text=bool(getattr(event, "had_text", False)),
                text_allowed=bool(getattr(event, "text_allowed", False)),
                text=getattr(event, "text", None),
            )
        except Exception:
            logger.debug("proactive recommendation feedback turn sink failed", exc_info=True)


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


def summarize_recommendation_feedback(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    samples = _calibration_observation_samples(
        observations,
        now=current,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    events_by_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in feedback_events:
        if not isinstance(event, Mapping):
            continue
        safe = sanitize_recommendation_feedback_event(event)
        key = (_clean_text(safe.get("lanlan_name")), _clean_text(safe.get("turn_id")))
        if key[0] and key[1]:
            events_by_turn[key].append(safe)

    feedback_scores: list[float] = []
    source_scores: dict[str, list[float]] = defaultdict(list)
    event_counts: Counter[str] = Counter()
    high_positive = 0
    high_negative = 0
    missing = 0
    positive = 0
    negative = 0
    neutral = 0
    explicit_count = 0
    inferred_count = 0

    for row in samples:
        key = (_clean_text(row.get("lanlan_name")), _clean_text(row.get("turn_id")))
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = _number(row.get("ts"), -1.0)
            if ts >= 0 and current - ts >= REPLY_WINDOW_SECONDS:
                events = [
                    build_feedback_event(
                        lanlan_name=key[0],
                        turn_id=key[1],
                        event_type="ignored",
                        source_type=row.get("actual_primary_channel") or row.get("shadow_selected_source_type"),
                        ts=current,
                    )
                ]
                feedback_inferred = True
        if not events:
            missing += 1
            continue
        if feedback_inferred:
            inferred_count += 1
        else:
            explicit_count += 1
        selected = _select_feedback_events_for_turn(events)
        score = _clamp(sum(_number(event.get("report_score_v1"), 0.0) for event in selected), -1.0, 1.0)
        feedback_scores.append(score)
        source_type = _normalize_source_type(
            row.get("actual_primary_channel") or row.get("shadow_selected_source_type")
        )
        source_scores[source_type].append(score)
        if score > 0:
            positive += 1
        elif score < 0:
            negative += 1
        else:
            neutral += 1
        for event in selected:
            event_counts[str(event.get("event_type") or "unknown")] += 1
            confidence = str(event.get("confidence") or "")
            event_score = _number(event.get("report_score_v1"), 0.0)
            if confidence == "high" and event_score > 0:
                high_positive += 1
            if confidence == "high" and event_score < 0:
                high_negative += 1

    count = len(feedback_scores)
    return {
        "feedback_sample_count": count,
        "feedback_joined_count": explicit_count,
        "feedback_inferred_count": inferred_count,
        "feedback_scored_count": count,
        "average_turn_feedback_score": round(sum(feedback_scores) / count, 3) if count else None,
        "positive_rate": _rate(positive, count),
        "negative_rate": _rate(negative, count),
        "neutral_rate": _rate(neutral, count),
        "score_by_source_type": {
            source: round(sum(values) / len(values), 3)
            for source, values in sorted(source_scores.items())
            if values
        },
        "event_type_distribution": dict(sorted(event_counts.items())),
        "high_confidence_positive_count": high_positive,
        "high_confidence_negative_count": high_negative,
        "feedback_missing_count": missing,
        "sample_count": len(samples),
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "score_version": FEEDBACK_SCORE_VERSION,
    }


def join_observations_with_feedback(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> list[dict[str, Any]]:
    """Join recent recommendation observations with compact feedback scores."""
    current = time.time() if now is None else float(now)
    samples = _calibration_observation_samples(
        observations,
        now=current,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    events_by_turn = _feedback_events_by_turn(feedback_events)
    joined: list[dict[str, Any]] = []
    for row in samples:
        key = (_clean_text(row.get("lanlan_name")), _clean_text(row.get("turn_id")))
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = _number(row.get("ts"), -1.0)
            if ts >= 0 and current - ts >= REPLY_WINDOW_SECONDS:
                events = [
                    build_feedback_event(
                        lanlan_name=key[0],
                        turn_id=key[1],
                        event_type="ignored",
                        source_type=row.get("actual_primary_channel") or row.get("shadow_selected_source_type"),
                        ts=current,
                    )
                ]
                feedback_inferred = True
        selected = _select_feedback_events_for_turn(events) if events else []
        feedback_missing = not selected
        turn_feedback_score = None
        if selected:
            turn_feedback_score = round(
                _clamp(
                    sum(_number(event.get("report_score_v1"), 0.0) for event in selected),
                    -1.0,
                    1.0,
                ),
                3,
            )
        top1_source_type = _top1_source_type(row)
        shadow_score = _shadow_selected_score(row)
        joined.append(
            {
                "turn_id": key[1],
                "lanlan_name": key[0],
                "source_type": top1_source_type,
                "shadow_selected_score": shadow_score,
                "top1_source_type": top1_source_type,
                "actual_primary_channel": _normalize_source_type(row.get("actual_primary_channel")),
                "matched_actual_source": row.get("matched_actual_source") is True,
                "matched_actual_material": row.get("matched_actual_material") is True,
                "turn_feedback_score": turn_feedback_score,
                "feedback_event_types": [
                    str(event.get("event_type") or "unknown")
                    for event in selected
                ],
                "feedback_missing": feedback_missing,
                "feedback_inferred": feedback_inferred,
                "score_bucket": _score_bucket(shadow_score),
            }
        )
    return joined


def join_observations_with_reward_score_v2_preview(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> list[dict[str, Any]]:
    """Join feedback into a point-in-time, attribution-checked v2 preview."""
    current = time.time() if now is None else float(now)
    samples = _calibration_observation_samples(
        observations,
        now=current,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    events_by_turn = _feedback_events_by_turn(feedback_events)
    relative_speed_by_turn = _relative_reply_speed_previews(
        samples,
        events_by_turn,
    )
    joined: list[dict[str, Any]] = []
    for row in samples:
        key = (_clean_text(row.get("lanlan_name")), _clean_text(row.get("turn_id")))
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = _number(row.get("ts"), -1.0)
            if ts >= 0 and current - ts >= REPLY_WINDOW_SECONDS:
                events = [
                    build_feedback_event(
                        lanlan_name=key[0],
                        turn_id=key[1],
                        event_type="ignored",
                        source_type=(
                            row.get("actual_primary_channel")
                            or row.get("shadow_selected_source_type")
                        ),
                        ts=current,
                    )
                ]
                feedback_inferred = True

        preview = build_reward_score_v2_preview(
            events,
            feedback_inferred=feedback_inferred,
            relative_speed_preview=relative_speed_by_turn.get(key),
        )
        attribution_issue = (
            _reward_v2_preview_attribution_issue(row, events)
            if events
            else None
        )
        attribution_valid = None if not events else attribution_issue is None
        reward_score = preview.get("reward_score_v2_preview")
        if attribution_valid is not True:
            reward_score = None
        expected_candidate_id = (
            _clean_text(row.get("shadow_selected_candidate_id")) or None
            if row.get("matched_actual_material") is True
            else None
        )
        joined.append(
            {
                "turn_id": key[1],
                "lanlan_name": key[0],
                "source_type": _normalize_source_type(
                    row.get("actual_primary_channel")
                    or row.get("shadow_selected_source_type")
                ),
                "candidate_id": expected_candidate_id,
                "reward_score_v2_preview": reward_score,
                "reward_components_v2_preview": dict(preview["components"]),
                "feedback_event_types": list(preview["event_types"]),
                "feedback_missing": not events,
                "feedback_inferred": feedback_inferred,
                "attribution_valid": attribution_valid,
                "attribution_issue": attribution_issue,
                "relative_speed_status": preview["relative_speed_status"],
                "relative_speed_baseline_sample_count": preview[
                    "relative_speed_baseline_sample_count"
                ],
                "technical_zero_event_types": list(
                    preview["technical_zero_event_types"]
                ),
                "unknown_event_types": list(preview["unknown_event_types"]),
            }
        )
    return joined


def summarize_reward_score_v2_preview(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """Summarize v2 preview without mutating ranking, tuning, or profiles."""
    joined = join_observations_with_reward_score_v2_preview(
        observations,
        feedback_events,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    scored = [
        row
        for row in joined
        if row.get("attribution_valid") is True
        and isinstance(row.get("reward_score_v2_preview"), (int, float))
    ]
    explicit_scored = [
        row for row in scored if row.get("feedback_inferred") is not True
    ]
    inferred_scored = [
        row for row in scored if row.get("feedback_inferred") is True
    ]
    rewards = [
        float(row["reward_score_v2_preview"])
        for row in explicit_scored
    ]
    inferred_rewards = [
        float(row["reward_score_v2_preview"])
        for row in inferred_scored
    ]
    all_rewards = [float(row["reward_score_v2_preview"]) for row in scored]
    source_rewards: dict[str, list[float]] = defaultdict(list)
    component_values: dict[str, list[float]] = defaultdict(list)
    for row in explicit_scored:
        source_rewards[_normalize_source_type(row.get("source_type"))].append(
            float(row["reward_score_v2_preview"])
        )
        components = row.get("reward_components_v2_preview")
        if isinstance(components, Mapping):
            for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER:
                component_values[component].append(
                    _number(components.get(component), 0.0)
                )

    attribution_issues = Counter(
        str(row.get("attribution_issue"))
        for row in joined
        if row.get("attribution_issue")
    )
    positive_count = sum(1 for reward in rewards if reward > 0)
    negative_count = sum(1 for reward in rewards if reward < 0)
    neutral_count = sum(1 for reward in rewards if reward == 0)
    return {
        "version": REWARD_SCORE_V2_PREVIEW_VERSION,
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "personalization_state_consumed": False,
        "sample_count": len(joined),
        "reward_scored_count": len(scored),
        "explicit_reward_scored_count": len(explicit_scored),
        "inferred_reward_scored_count": len(inferred_scored),
        "feedback_joined_count": len(explicit_scored),
        "feedback_inferred_count": len(inferred_scored),
        "feedback_missing_count": sum(
            1 for row in joined if row.get("feedback_missing") is True
        ),
        "attribution_issue_count": sum(attribution_issues.values()),
        "attribution_issue_distribution": dict(sorted(attribution_issues.items())),
        "average_reward_score_v2_preview": _average(rewards),
        "average_all_reward_score_v2_preview": _average(all_rewards),
        "average_inferred_reward_score_v2_preview": _average(inferred_rewards),
        "positive_rate": _rate(positive_count, len(rewards)),
        "negative_rate": _rate(negative_count, len(rewards)),
        "neutral_rate": _rate(neutral_count, len(rewards)),
        "score_by_source_type": {
            source: _average(values)
            for source, values in sorted(source_rewards.items())
            if values
        },
        "average_components": {
            component: _average(component_values.get(component, []))
            for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER
        },
        "relative_speed_neutral_count": sum(
            1
            for row in explicit_scored
            if row.get("relative_speed_status")
            in {
                "pending_personal_baseline",
                "insufficient_personal_baseline",
                "baseline_ready_no_bonus",
                "missing_reply_latency",
            }
        ),
        "relative_speed_bonus_count": sum(
            1
            for row in explicit_scored
            if _number(
                (row.get("reward_components_v2_preview") or {}).get(
                    "relative_speed"
                )
                if isinstance(row.get("reward_components_v2_preview"), Mapping)
                else None,
                0.0,
            )
            > 0
        ),
        "personal_reply_speed_baseline_ready_count": sum(
            1
            for row in explicit_scored
            if row.get("relative_speed_status")
            in {"baseline_ready_bonus", "baseline_ready_no_bonus"}
        ),
        "technical_zero_event_count": sum(
            len(row.get("technical_zero_event_types") or [])
            for row in explicit_scored
        ),
        "unknown_event_count": sum(
            len(row.get("unknown_event_types") or [])
            for row in explicit_scored
        ),
        "score_population": "valid_turn_id_joined_explicit_only",
        "inferred_ignored_reported_separately": True,
        "window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
    }


def summarize_feedback_calibration(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """Report whether recommendation scores align with later user feedback."""
    joined = join_observations_with_feedback(
        observations,
        feedback_events,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    scored = [
        row
        for row in joined
        if row.get("feedback_missing") is not True
        and isinstance(row.get("turn_feedback_score"), (int, float))
    ]
    feedback_joined_count = sum(
        1 for row in scored if row.get("feedback_inferred") is not True
    )
    feedback_inferred_count = sum(
        1 for row in scored if row.get("feedback_inferred") is True
    )
    feedback_scored_count = len(scored)
    feedback_scores = [float(row["turn_feedback_score"]) for row in scored]
    positive_count = sum(1 for score in feedback_scores if score > 0)
    negative_count = sum(1 for score in feedback_scores if score < 0)

    source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    high_score_source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mid_low_source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    top1_counts: Counter[str] = Counter()
    for row in joined:
        source = _normalize_source_type(row.get("source_type"))
        if source:
            top1_counts[source] += 1
        if row.get("feedback_missing") is True or not isinstance(row.get("turn_feedback_score"), (int, float)):
            continue
        source_rows[source].append(row)
        bucket = row.get("score_bucket")
        if bucket == "high":
            high_score_source_rows[source].append(row)
        elif bucket in {"mid", "low"}:
            mid_low_source_rows[source].append(row)

    score_by_source_type = {
        source: _average_joined_feedback(rows)
        for source, rows in sorted(source_rows.items())
        if rows
    }
    bucket_feedback = _score_bucket_feedback(scored)

    over_scored_sources = sorted(
        source
        for source, rows in high_score_source_rows.items()
        if rows and _average_joined_feedback(rows) < 0
    )
    under_scored_sources = sorted(
        source
        for source, rows in mid_low_source_rows.items()
        if rows and _average_joined_feedback(rows) >= 0.25
    )

    dominant_low_feedback_sources = _dominant_low_feedback_sources(
        joined,
        score_by_source_type,
        top1_counts,
    )
    feedback_signal_summary = _feedback_signal_summary(scored)
    source_feedback_pressure = _source_feedback_pressure(feedback_signal_summary)
    suggested_weight_adjustments = _suggest_feedback_weight_adjustments(
        over_scored_sources=over_scored_sources,
        under_scored_sources=under_scored_sources,
        dominant_low_feedback_sources=dominant_low_feedback_sources,
        source_feedback_pressure=source_feedback_pressure,
    )
    feedback_actionable_suggestions = _feedback_actionable_suggestions(
        score_by_source_type=score_by_source_type,
        signal_summary=feedback_signal_summary,
        source_feedback_pressure=source_feedback_pressure,
    )
    active_ready_reasons = _feedback_active_ready_reasons(
        feedback_joined_count=feedback_joined_count,
        average_feedback_score=_average(feedback_scores),
        top1_positive_rate=_rate(positive_count, feedback_scored_count),
        top1_negative_rate=_rate(negative_count, feedback_scored_count),
        bucket_feedback=bucket_feedback,
        dominant_low_feedback_sources=dominant_low_feedback_sources,
    )

    return {
        "sample_count": len(joined),
        "feedback_joined_count": feedback_joined_count,
        "feedback_inferred_count": feedback_inferred_count,
        "feedback_scored_count": feedback_scored_count,
        "feedback_missing_count": len(joined) - feedback_scored_count,
        "average_feedback_score": _average(feedback_scores),
        "top1_positive_rate": _rate(positive_count, feedback_scored_count),
        "top1_negative_rate": _rate(negative_count, feedback_scored_count),
        "feedback_score_population": "explicit_and_inferred",
        "feedback_rate_denominator": "feedback_scored_count",
        "score_by_source_type": score_by_source_type,
        "score_bucket_feedback": bucket_feedback,
        "over_scored_sources": over_scored_sources,
        "under_scored_sources": under_scored_sources,
        "suggested_weight_adjustments": suggested_weight_adjustments,
        "feedback_signal_summary": feedback_signal_summary,
        "source_feedback_pressure": source_feedback_pressure,
        "feedback_actionable_suggestions": feedback_actionable_suggestions,
        "manual_tuning_preview": _manual_tuning_preview(feedback_actionable_suggestions),
        "active_ready_by_feedback": not active_ready_reasons,
        "active_ready_reasons": active_ready_reasons,
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "score_version": FEEDBACK_SCORE_VERSION,
    }


def _feedback_events_by_turn(
    feedback_events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    events_by_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in feedback_events:
        if not isinstance(event, Mapping):
            continue
        safe = sanitize_recommendation_feedback_event(event)
        key = (_clean_text(safe.get("lanlan_name")), _clean_text(safe.get("turn_id")))
        if key[0] and key[1]:
            events_by_turn[key].append(safe)
    return events_by_turn


def _relative_reply_speed_previews(
    observations: Sequence[Mapping[str, Any]],
    events_by_turn: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build per-turn speed previews from earlier, valid replies only."""
    records: list[tuple[tuple[str, str], float, float | None]] = []
    for observation in observations:
        key = (
            _clean_text(observation.get("lanlan_name")),
            _clean_text(observation.get("turn_id")),
        )
        events = list(events_by_turn.get(key, ()))
        if not key[0] or not key[1] or not events:
            continue
        if _reward_v2_preview_attribution_issue(observation, events) is not None:
            continue
        replies = [
            event
            for event in events
            if _clean_text(event.get("event_type")) in _REWARD_V2_PREVIEW_REPLY_EVENTS
        ]
        if not replies:
            continue
        reply = min(
            replies,
            key=lambda event: _number(event.get("ts"), float("inf")),
        )
        event_ts = _number(
            reply.get("ts"),
            _number(observation.get("ts"), float("inf")),
        )
        latency = _reply_latency_seconds(reply)
        records.append((key, event_ts, latency))

    valid_history = [record for record in records if record[2] is not None]
    previews: dict[tuple[str, str], dict[str, Any]] = {}
    for key, event_ts, latency in records:
        prior_latencies = [
            float(previous_latency)
            for _, previous_ts, previous_latency in valid_history
            if previous_ts < event_ts
            and previous_latency is not None
        ]
        previews[key] = _relative_reply_speed_preview(
            latency,
            prior_latencies,
        )
    return previews


def _reply_latency_seconds(event: Mapping[str, Any]) -> float | None:
    metadata = event.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    latency = _number(metadata.get("reply_latency_seconds"), float("nan"))
    if (
        not math.isfinite(latency)
        or latency < 0
        or latency > REPLY_WINDOW_SECONDS
    ):
        return None
    return latency


def _relative_reply_speed_preview(
    latency: float | None,
    prior_latencies: Sequence[float],
) -> dict[str, Any]:
    sample_count = len(prior_latencies)
    if latency is None:
        return {
            "status": "missing_reply_latency",
            "baseline_sample_count": sample_count,
            "bonus": 0.0,
        }
    if sample_count < REPLY_SPEED_BASELINE_MIN_SAMPLES:
        return {
            "status": "insufficient_personal_baseline",
            "baseline_sample_count": sample_count,
            "bonus": 0.0,
        }

    logged = [math.log1p(value) for value in prior_latencies]
    center = float(median(logged))
    mad = float(median(abs(value - center) for value in logged))
    scale = max(1.4826 * mad, REPLY_SPEED_LOG_SCALE_FLOOR)
    faster_z = max(0.0, (center - math.log1p(latency)) / scale)
    bonus = min(REPLY_SPEED_BONUS_MAX, faster_z * 0.02)
    return {
        "status": (
            "baseline_ready_bonus" if bonus > 0 else "baseline_ready_no_bonus"
        ),
        "baseline_sample_count": sample_count,
        "bonus": round(bonus, 3),
    }


def _top1_source_type(row: Mapping[str, Any]) -> str:
    candidates = row.get("top_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                source = _normalize_source_type(candidate.get("source_type"))
                if source:
                    return source
    return _normalize_source_type(row.get("shadow_selected_source_type"))


def _shadow_selected_score(row: Mapping[str, Any]) -> float | None:
    score = _number(row.get("shadow_selected_score"), float("nan"))
    if score == score:
        return round(score, 3)
    candidates = row.get("top_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                score = _number(candidate.get("score"), float("nan"))
                if score == score:
                    return round(score, 3)
    return None


def _score_bucket(score: Any) -> str | None:
    value = _number(score, float("nan"))
    if value != value:
        return None
    if value >= 0.75:
        return "high"
    if value >= 0.50:
        return "mid"
    return "low"


def _average(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _average_joined_feedback(rows: Sequence[Mapping[str, Any]]) -> float:
    scores = [
        float(row["turn_feedback_score"])
        for row in rows
        if isinstance(row.get("turn_feedback_score"), (int, float))
    ]
    average = _average(scores)
    return 0.0 if average is None else average


def _score_bucket_feedback(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for bucket in ("high", "mid", "low"):
        bucket_rows = [row for row in rows if row.get("score_bucket") == bucket]
        scores = [
            float(row["turn_feedback_score"])
            for row in bucket_rows
            if isinstance(row.get("turn_feedback_score"), (int, float))
        ]
        positive = sum(1 for score in scores if score > 0)
        negative = sum(1 for score in scores if score < 0)
        result[bucket] = {
            "count": len(scores),
            "average_feedback_score": _average(scores),
            "positive_rate": _rate(positive, len(scores)),
            "negative_rate": _rate(negative, len(scores)),
        }
    return result


def _dominant_low_feedback_sources(
    joined: Sequence[Mapping[str, Any]],
    score_by_source_type: Mapping[str, float],
    top1_counts: Counter[str],
) -> list[str]:
    total = len(joined)
    if total <= 0:
        return []
    return sorted(
        source
        for source, count in top1_counts.items()
        if _rate(count, total) is not None
        and float(_rate(count, total) or 0.0) >= 0.60
        and float(score_by_source_type.get(source, 0.0)) < 0.10
    )


def _suggest_feedback_weight_adjustments(
    *,
    over_scored_sources: Sequence[str],
    under_scored_sources: Sequence[str],
    dominant_low_feedback_sources: Sequence[str],
    source_feedback_pressure: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    suggestions: dict[str, dict[str, Any]] = {}
    pressure = source_feedback_pressure or {}

    def add(source: str, adjustment: float, reason: str) -> None:
        entry = suggestions.setdefault(
            source,
            {"adjustment": adjustment, "reasons": []},
        )
        if abs(adjustment) > abs(float(entry.get("adjustment", 0.0))):
            entry["adjustment"] = adjustment
        reasons = entry.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)

    for source in over_scored_sources:
        if _is_weak_ignored_only_pressure(source, pressure):
            continue
        add(source, -0.05, "over_scored_high_score_low_feedback")
    for source in under_scored_sources:
        add(source, 0.03, "under_scored_positive_feedback")
    for source in dominant_low_feedback_sources:
        if _is_weak_ignored_only_pressure(source, pressure):
            continue
        add(source, -0.05, "dominant_low_feedback_source")
    return {
        source: {
            "adjustment": round(float(entry["adjustment"]), 3),
            "reasons": list(entry["reasons"]),
        }
        for source, entry in sorted(suggestions.items())
    }


def _is_weak_ignored_only_pressure(
    source: str,
    source_feedback_pressure: Mapping[str, Mapping[str, Any]],
) -> bool:
    pressure = source_feedback_pressure.get(source)
    if not isinstance(pressure, Mapping):
        return False
    return (
        pressure.get("level") == "weak_ignored_pressure"
        and int(pressure.get("weak_negative_count") or 0) > 0
        and int(pressure.get("high_confidence_negative_count") or 0) == 0
        and int(pressure.get("strong_positive_count") or 0) == 0
    )


def _feedback_signal_summary(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "feedback_count": 0,
            "strong_positive_count": 0,
            "weak_negative_count": 0,
            "ignored_count": 0,
            "played_through_count": 0,
            "high_confidence_negative_count": 0,
        }
    )
    for row in rows:
        source = _normalize_source_type(row.get("source_type"))
        if not source:
            continue
        bucket = stats[source]
        bucket["feedback_count"] += 1
        event_types = row.get("feedback_event_types")
        if not isinstance(event_types, Sequence) or isinstance(event_types, (str, bytes)):
            continue
        for raw_event_type in event_types:
            event_type = str(raw_event_type or "unknown")
            _, score, confidence = _FEEDBACK_EVENT_SCORES.get(
                event_type,
                ("unknown", 0.0, "low"),
            )
            if confidence == "high" and score > 0:
                bucket["strong_positive_count"] += 1
            if confidence == "high" and score < 0:
                bucket["high_confidence_negative_count"] += 1
            if event_type in _WEAK_NEGATIVE_EVENT_TYPES:
                bucket["weak_negative_count"] += 1
            if event_type == "ignored":
                bucket["ignored_count"] += 1
            if event_type == _MUSIC_PLAYED_THROUGH_EVENT_TYPE:
                bucket["played_through_count"] += 1

    result: dict[str, dict[str, Any]] = {}
    for source, bucket in sorted(stats.items()):
        denominator = (
            int(bucket["strong_positive_count"])
            + int(bucket["high_confidence_negative_count"])
            + int(bucket["weak_negative_count"])
        )
        result[source] = {
            "feedback_count": int(bucket["feedback_count"]),
            "strong_positive_count": int(bucket["strong_positive_count"]),
            "weak_negative_count": int(bucket["weak_negative_count"]),
            "ignored_count": int(bucket["ignored_count"]),
            "played_through_count": int(bucket["played_through_count"]),
            "high_confidence_negative_count": int(bucket["high_confidence_negative_count"]),
            "confidence_positive_rate": _rate(
                int(bucket["strong_positive_count"]),
                denominator,
            ),
        }
    return result


def _source_feedback_pressure(
    signal_summary: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    pressure: dict[str, dict[str, Any]] = {}
    for source, stats in sorted(signal_summary.items()):
        ignored_count = int(stats.get("ignored_count") or 0)
        weak_negative_count = int(stats.get("weak_negative_count") or 0)
        high_negative_count = int(stats.get("high_confidence_negative_count") or 0)
        strong_positive_count = int(stats.get("strong_positive_count") or 0)
        if high_negative_count > 0:
            level = "high_confidence_negative_pressure"
        elif weak_negative_count > 0:
            level = "weak_ignored_pressure"
        else:
            level = "none"
        pressure[source] = {
            "level": level,
            "ignored_count": ignored_count,
            "weak_negative_count": weak_negative_count,
            "high_confidence_negative_count": high_negative_count,
            "strong_positive_count": strong_positive_count,
        }
    return pressure


def _feedback_actionable_suggestions(
    *,
    score_by_source_type: Mapping[str, float],
    signal_summary: Mapping[str, Mapping[str, Any]],
    source_feedback_pressure: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    suggestions: dict[str, dict[str, Any]] = {}

    def add(source: str, adjustment: float, reason: str, confidence: str) -> None:
        entry = suggestions.setdefault(
            source,
            {"adjustment": adjustment, "reasons": [], "confidence": confidence},
        )
        if abs(adjustment) > abs(float(entry.get("adjustment", 0.0))):
            entry["adjustment"] = adjustment
            entry["confidence"] = confidence
        reasons = entry.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)

    music_stats = signal_summary.get("music") or {}
    music_average = score_by_source_type.get("music")
    if (
        int(music_stats.get("played_through_count") or 0) >= _MUSIC_ACTIONABLE_PLAYED_THROUGH_MIN
        and isinstance(music_average, (int, float))
        and float(music_average) >= _MUSIC_ACTIONABLE_AVERAGE_MIN
    ):
        add("music", 0.03, "strong_music_positive_feedback", "high")

    for source, pressure in sorted(source_feedback_pressure.items()):
        level = str(pressure.get("level") or "")
        average = score_by_source_type.get(source)
        if level == "weak_ignored_pressure":
            add(source, 0.0, "weak_ignored_pressure", "low")
        elif (
            level == "high_confidence_negative_pressure"
            and isinstance(average, (int, float))
            and float(average) < 0
        ):
            add(source, -0.05, "high_confidence_negative_feedback", "high")

    return {
        source: {
            "adjustment": round(float(entry["adjustment"]), 3),
            "reasons": list(entry["reasons"]),
            "confidence": str(entry.get("confidence") or "low"),
        }
        for source, entry in sorted(suggestions.items())
    }


def _manual_tuning_preview(
    feedback_actionable_suggestions: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    preview: dict[str, dict[str, Any]] = {}
    for source, suggestion in sorted(feedback_actionable_suggestions.items()):
        adjustment = round(_number(suggestion.get("adjustment"), 0.0), 3)
        preview[source] = {
            "current_adjustment": 0.0,
            "suggested_delta": adjustment,
            "preview_adjustment": adjustment,
            "reasons": list(suggestion.get("reasons") or []),
            "confidence": str(suggestion.get("confidence") or "low"),
            "write_mode": "manual_review_only",
        }
    return preview


def _feedback_active_ready_reasons(
    *,
    feedback_joined_count: int,
    average_feedback_score: float | None,
    top1_positive_rate: float | None,
    top1_negative_rate: float | None,
    bucket_feedback: Mapping[str, Mapping[str, Any]],
    dominant_low_feedback_sources: Sequence[str],
) -> list[str]:
    reasons: list[str] = []
    if feedback_joined_count < 30:
        reasons.append("feedback_sample_count_below_threshold")
    if average_feedback_score is None or average_feedback_score <= 0:
        reasons.append("average_feedback_score_not_positive")
    high = bucket_feedback.get("high", {}).get("average_feedback_score")
    mid = bucket_feedback.get("mid", {}).get("average_feedback_score")
    low = bucket_feedback.get("low", {}).get("average_feedback_score")
    if not all(isinstance(value, (int, float)) for value in (high, mid, low)):
        reasons.append("score_bucket_feedback_insufficient")
    elif not (float(high) > float(mid) >= float(low)):
        reasons.append("score_bucket_feedback_not_monotonic")
    if top1_positive_rate is None or top1_positive_rate < 0.35:
        reasons.append("top1_positive_rate_below_threshold")
    if top1_negative_rate is None or top1_negative_rate > 0.20:
        reasons.append("top1_negative_rate_above_threshold")
    if dominant_low_feedback_sources:
        reasons.append("dominant_low_feedback_source")
    return reasons


def _select_feedback_events_for_turn(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = sanitize_recommendation_feedback_event(raw)
        group = str(event.get("event_group") or "unknown")
        previous = by_group.get(group)
        if previous is None or abs(_number(event.get("report_score_v1"), 0.0)) > abs(_number(previous.get("report_score_v1"), 0.0)):
            by_group[group] = event
    return list(by_group.values())


def _reward_v2_preview_attribution_issue(
    observation: Mapping[str, Any],
    feedback_events: Sequence[Mapping[str, Any]],
) -> str | None:
    """Validate that feedback belongs to the material actually delivered."""
    if observation.get("delivered") is not True:
        return "observation_not_delivered"
    expected_source = _normalize_source_type(
        observation.get("actual_primary_channel")
        or observation.get("shadow_selected_source_type")
    )
    expected_candidate_id = (
        _clean_text(observation.get("shadow_selected_candidate_id")) or None
        if observation.get("matched_actual_material") is True
        else None
    )
    for raw_event in feedback_events:
        event = sanitize_recommendation_feedback_event(raw_event)
        event_source = _normalize_source_type(event.get("source_type"))
        if (
            event_source != "unknown"
            and expected_source != "unknown"
            and event_source != expected_source
        ):
            return "source_mismatch"
        event_candidate_id = _clean_text(event.get("candidate_id")) or None
        if event_candidate_id is None:
            continue
        if expected_candidate_id is None:
            return "candidate_unverifiable"
        if event_candidate_id != expected_candidate_id:
            return "candidate_mismatch"
    return None


def _maybe_auto_apply_tuning_after_feedback(
    *,
    config_dir: str | os.PathLike[str] | None,
) -> None:
    if config_dir is None:
        return
    try:
        from config import PROACTIVE_RECOMMENDATION_TUNING_MODE
        from main_logic.proactive_recommendation_tuning import (
            maybe_auto_apply_recommendation_tuning_from_logs,
            maybe_update_recommendation_tuning_health_from_logs,
        )

        result = maybe_auto_apply_recommendation_tuning_from_logs(
            mode=PROACTIVE_RECOMMENDATION_TUNING_MODE,
            config_dir=config_dir,
        )
        if not result.get("applied") and not result.get("rollback_applied"):
            maybe_update_recommendation_tuning_health_from_logs(
                mode=PROACTIVE_RECOMMENDATION_TUNING_MODE,
                config_dir=config_dir,
            )
    except Exception as exc:
        logger.debug("proactive recommendation tuning auto-apply failed: %s", exc)


def _latest_pending_for_lanlan(lanlan_name: str, *, now: float) -> PendingRecommendationFeedback | None:
    _prune_pending_feedback(now=now)
    candidates = [
        pending
        for pending in _pending_feedback.values()
        if pending.lanlan_name == lanlan_name
        and 0 <= now - pending.delivered_at <= REPLY_WINDOW_SECONDS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.delivered_at)


def _prune_pending_feedback(*, now: float) -> None:
    expired = [
        key
        for key, pending in _pending_feedback.items()
        if now - pending.delivered_at > REPLY_WINDOW_SECONDS * 2
    ]
    for key in expired:
        _pending_feedback.pop(key, None)


def _calibration_observation_samples(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float,
    window_seconds: int,
    sample_limit: int,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    recent = [
        row
        for row in rows
        if 0 <= now - _number(row.get("ts"), -1.0) <= max(0, int(window_seconds))
    ]
    limit = max(0, int(sample_limit))
    return recent[-limit:] if limit else []


def _resolve_feedback_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    if config_dir is None:
        return None
    return Path(config_dir) / FEEDBACK_LOG_FILENAME


def _rotate_if_needed(path: Path, *, rotate_bytes: int) -> None:
    if rotate_bytes <= 0:
        return
    try:
        if path.exists() and path.stat().st_size > rotate_bytes:
            os.replace(path, path.parent / (path.name + ".1"))
    except OSError as exc:
        logger.debug("proactive recommendation feedback rotate failed: %s", exc)


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


def _reply_length_bucket(length: int) -> str:
    if length < 20:
        return "short"
    if length < 120:
        return "medium"
    return "long"


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


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
