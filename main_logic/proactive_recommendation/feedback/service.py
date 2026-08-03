"""Feedback event sink for proactive recommendation observations.

This module records what the user did after a proactive recommendation was
delivered. It keeps raw event types separate from report-only scores so future
calibration can recompute scores without losing the original signal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import os
import time
from typing import Any, Callable

from main_logic.proactive_recommendation.domain_models import RecordFeedbackCommand

from main_logic.proactive_recommendation.state.bandit import (
    update_recommendation_bandit_reward,
)
from main_logic.proactive_recommendation.state.preference import (
    update_recommendation_source_preference,
)
from main_logic.proactive_recommendation.state.feedback import (
    update_conversation_acceptance_preview,
    update_source_affinity_preview,
)
from main_logic.proactive_recommendation.feedback.contracts import (
    PendingRecommendationFeedback,
    RecommendationFeedbackRecordResult,
)
from main_logic.proactive_recommendation.feedback.pending import (
    PendingFeedbackRegistry,
)
from main_logic.proactive_recommendation.feedback.learning import (
    bandit_event_matches_pending,
    feedback_learning_enabled,
    source_affinity_event_matches_pending,
    source_preference_outcome,
)
from main_logic.proactive_recommendation.feedback.events import (
    _clean_text,
    _normalize_source_type,
    _number,
    build_feedback_event,
)
from main_logic.proactive_recommendation.feedback.rewards import (
    build_bandit_encounter_reward,
    reward_event_score,
)
from main_logic.proactive_recommendation.feedback.store import (
    append_recommendation_feedback_jsonl,
)
from main_logic.proactive_recommendation.feedback.text_signals import (
    _explicit_text_named_source_types,
    _explicit_text_source_preference_event_type,
)

logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_feedback")

REPLY_FAST_SECONDS = 60
REPLY_WINDOW_SECONDS = 10 * 60
_CONVERSATION_ACCEPTANCE_EVENT_TYPES = {
    "user_reply_fast",
    "user_reply",
    "user_continue",
    "proactive_disabled_after",
}
_SOURCE_AFFINITY_EVENT_TYPES = {
    "source_disabled_after",
    "source_not_interested",
    "source_fatigue",
    "candidate_not_interested",
    "source_interested",
    "music_played_through",
    "music_high_completion",
    "music_mid_completion",
    "music_normal_close",
    "music_early_close",
    "music_hard_skip",
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
_pending_registry = PendingFeedbackRegistry(reply_window_seconds=REPLY_WINDOW_SECONDS)
_after_feedback_logged: Callable[[Any], None] | None = None


def configure_feedback_logged_hook(
    hook: Callable[[Any], None] | None,
) -> None:
    """Inject application follow-up without a feedback-to-tuning import."""
    global _after_feedback_logged
    _after_feedback_logged = hook


def clear_pending_recommendation_feedback() -> None:
    """Test helper: clear in-memory pending feedback state."""
    _pending_registry.clear()


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
    return _pending_registry.consecutive_unanswered(name, now=current)


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
            candidate_id = (
                _clean_text(
                    observation.get("active_preferred_candidate_id")
                    if active_preference
                    else observation.get("shadow_selected_candidate_id")
                )
                or None
            )
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
    return _pending_registry.register(pending)


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
    pending = _pending_registry.get(name, tid)
    event = build_feedback_event(
        lanlan_name=name,
        turn_id=tid,
        event_type=event_type,
        source_type=source_type
        if source_type is not None
        else (pending.source_type if pending else None),
        candidate_id=candidate_id
        if candidate_id is not None
        else (pending.candidate_id if pending else None),
        metadata=metadata,
        ts=ts,
    )
    if not event.get("event_type"):
        return RecommendationFeedbackRecordResult(
            event=None, logged=False, state_reason="invalid_event"
        )
    claim = _pending_registry.claim_event(
        name,
        tid,
        event_type=str(event["event_type"]),
        state_group=_feedback_state_group(event),
    )
    pending = claim.pending
    duplicate_event = claim.duplicate_event
    duplicate_group = claim.duplicate_group
    effective_log_mode = (
        log_mode if log_mode is not None else (pending.log_mode if pending else "off")
    )
    effective_config_dir = (
        config_dir
        if config_dir is not None
        else (pending.config_dir if pending else None)
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
        if _after_feedback_logged is not None:
            try:
                _after_feedback_logged(effective_config_dir)
            except Exception as exc:
                logger.debug("recommendation feedback follow-up failed: %s", exc)
        if (
            pending is not None
            and feedback_learning_enabled(pending)
            and not duplicate_event
        ):
            event_type = str(event.get("event_type") or "")
            if bandit_event_matches_pending(event, pending):
                reward_events = _pending_registry.add_reward_event(
                    pending,
                    event_type,
                    event,
                )
                bandit_reward = build_bandit_encounter_reward(reward_events)
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
                        logger.debug(
                            "recommendation bandit reward update failed: %s", exc
                        )
            score = reward_event_score(event_type)
            persistent_eligible = (
                not duplicate_group
                and event.get("confidence") in {"medium", "high"}
                and score != 0
            )
            try:
                if event_type in _CONVERSATION_ACCEPTANCE_EVENT_TYPES:
                    feedback_scope = "conversation_acceptance"
                    update_conversation_acceptance_preview(
                        config_dir=effective_config_dir,
                        score=score,
                        persistent_eligible=persistent_eligible,
                        now=_number(event.get("ts"), time.time()),
                    )
                    state_updated = True
                    state_reason = "applied"
                elif (
                    event_type in _SOURCE_AFFINITY_EVENT_TYPES
                    and source_affinity_event_matches_pending(event, pending)
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
                and source_affinity_event_matches_pending(event, pending)
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
    latest_pending = _latest_pending_for_lanlan(lanlan_name, now=timestamp)
    if latest_pending is None:
        return None
    pending = latest_pending
    source_preference_match: tuple[str, str] | None = None
    if text_allowed and text:
        named_sources = _explicit_text_named_source_types(text)
        if len(named_sources) == 1:
            named_source = named_sources[0]
            named_match = _explicit_text_source_preference_event_type(
                text,
                named_source,
            )
            if named_match is not None:
                named_pending = _latest_verified_pending_for_source(
                    lanlan_name,
                    source_type=named_source,
                    now=timestamp,
                )
                if named_pending is not None:
                    pending = named_pending
                    source_preference_match = named_match
        elif not named_sources:
            source_preference_match = _explicit_text_source_preference_event_type(
                text,
                pending.source_type,
            )
            if not pending.candidate_id:
                source_preference_match = None
    latency = max(0.0, float(timestamp) - float(pending.delivered_at))
    metadata: dict[str, Any] = {"reply_latency_seconds": round(latency, 3)}
    if text_allowed and text:
        metadata["reply_length"] = len(text)
        metadata["reply_length_bucket"] = _reply_length_bucket(len(text))
        if source_preference_match is not None:
            source_preference_event, reason = source_preference_match
            _pending_registry.mark_replied((latest_pending, pending))
            metadata["reason"] = reason
            return record_feedback_event(
                lanlan_name=pending.lanlan_name,
                turn_id=pending.turn_id,
                event_type=source_preference_event,
                source_type=pending.source_type,
                candidate_id=pending.candidate_id,
                metadata=metadata,
                ts=timestamp,
            )
    reply_action = _pending_registry.claim_reply_action(pending)
    if reply_action == "continue":
        return record_feedback_event(
            lanlan_name=pending.lanlan_name,
            turn_id=pending.turn_id,
            event_type="user_continue",
            metadata=metadata,
            ts=timestamp,
        )
    if reply_action == "reply":
        event_type = (
            "user_reply_fast" if latency <= REPLY_FAST_SECONDS else "user_reply"
        )
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
            logger.debug(
                "proactive recommendation feedback turn sink failed", exc_info=True
            )


class FeedbackService:
    """Transaction boundary for feedback delivery, events, and user turns."""

    def register_delivery(
        self,
        observation: Mapping[str, Any],
        *,
        log_mode: str = "off",
        config_dir: str | os.PathLike[str] | None = None,
    ) -> PendingRecommendationFeedback | None:
        return register_pending_feedback_from_observation(
            observation,
            log_mode=log_mode,
            config_dir=config_dir,
        )

    def record_event(
        self,
        command: RecordFeedbackCommand,
    ) -> RecommendationFeedbackRecordResult:
        return record_feedback_event_with_status(
            lanlan_name=command.lanlan_name,
            turn_id=command.turn_id,
            event_type=command.event_type,
            source_type=command.source_type,
            candidate_id=command.candidate_id,
            metadata=command.metadata,
            log_mode=command.log_mode,
            config_dir=command.config_dir,
            ts=command.ts,
        )

    def note_user_turn(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return note_user_turn_for_feedback(*args, **kwargs)

    def record_setting_change(self, **kwargs: Any) -> list[dict[str, Any]]:
        return record_recent_setting_feedback(**kwargs)


def _latest_pending_for_lanlan(
    lanlan_name: str, *, now: float
) -> PendingRecommendationFeedback | None:
    return _pending_registry.latest(lanlan_name, now=now)


def _latest_verified_pending_for_source(
    lanlan_name: str,
    *,
    source_type: str,
    now: float,
) -> PendingRecommendationFeedback | None:
    source = _normalize_source_type(source_type)
    return _pending_registry.latest(
        lanlan_name,
        now=now,
        source_type=source,
        require_candidate=True,
    )


def _reply_length_bucket(length: int) -> str:
    if length < 20:
        return "short"
    if length < 120:
        return "medium"
    return "long"
