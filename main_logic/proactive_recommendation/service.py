"""Recommendation application service and per-turn orchestration."""

from __future__ import annotations

from threading import RLock
import time
from typing import Any
from config import (
    PROACTIVE_RECOMMENDATION_BANDIT_MODE,
    PROACTIVE_RECOMMENDATION_MODE,
    PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE,
)
from collections import deque
import threading
import math
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import uuid4
from config import (
    MINI_GAME_INVITE_ENABLED,
    PROACTIVE_RECOMMENDATION_ACTIVE_MIN_SCORE_GAP,
    PROACTIVE_RECOMMENDATION_BANDIT_MODE,
    PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
    PROACTIVE_RECOMMENDATION_OBSERVATION_LOG,
    PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE,
    PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE,
    PROACTIVE_RECOMMENDATION_TUNING_MODE,
)
from main_logic.proactive_recommendation.feedback.service import (
    FEEDBACK_LOG_FILENAME,
    consecutive_unanswered_recommendation_deliveries,
    load_recommendation_feedback_jsonl,
    register_pending_feedback_from_observation,
)
from main_logic.proactive_recommendation.feedback.analytics import (
    summarize_feedback_calibration,
    summarize_recommendation_feedback,
    summarize_reward_score_v2_preview,
    summarize_reward_score_v3_preview,
)
from main_logic.proactive_recommendation.state.feedback_preview import (
    get_feedback_state_preview,
)
from main_logic.proactive_recommendation.engine.source_selection import (
    BANDIT_BASELINE_SCORE_CONTRACT,
    BANDIT_PERSONALIZED_SCORE_CONTRACT,
    bandit_preferred_candidate,
    build_source_bandit_decision,
)
from main_logic.proactive_recommendation.state.bandit_posteriors import (
    get_recommendation_bandit_state,
)
from main_logic.proactive_recommendation.state.source_preferences import (
    ensure_recommendation_preference_state,
    preference_adjustments,
)
from main_logic.proactive_recommendation.engine.source_selection import (
    build_personalization_plan,
    personalization_adjustments,
)
from main_logic.proactive_recommendation.observation.validation import (
    sanitize_recommendation_observation,
    summarize_recommendation_review_context,
)
from main_logic.proactive_recommendation.observation.storage import (
    DEFAULT_ROTATE_BYTES,
    OBSERVATION_LOG_FILENAME,
    append_recommendation_observation_jsonl,
    load_recommendation_observations_jsonl,
)
from main_logic.proactive_recommendation.observation.analytics import (
    CALIBRATION_SAMPLE_LIMIT,
    CALIBRATION_WINDOW_SECONDS,
    DEFAULT_EXAMPLE_LIMIT,
    DEFAULT_HIGH_SCORE_THRESHOLD,
    MAX_EXAMPLE_LIMIT,
    get_recommendation_calibration_samples,
    select_recommendation_observation_examples,
    summarize_recommendation_calibration,
    summarize_recommendation_policy,
    summarize_recommendation_validation,
)
from main_logic.proactive_recommendation.tuning.storage import (
    TUNING_FILENAME,
    load_recommendation_tuning,
)
from main_logic.proactive_recommendation.tuning.configuration import (
    tuning_public_status,
)
from collections.abc import Sequence
from config import PROACTIVE_RECOMMENDATION_TUNING_MODE
from .domain_models import (
    ProactiveActiveBias,
    ProactiveRecommendationContext,
    RecordFeedbackCommand,
    RecommendationFeedbackRecordResult,
    RecommendationSummaryQuery,
)
from .normalization import clamp_to_range, coerce_finite_float, to_stripped_text
from .persistence import resolve_persistence_path
from .engine.active_source_bias import (
    build_active_source_bias,
    reorder_phase1_topics_for_bias,
)
from .engine.source_selection import (
    build_phase1_material_shadow_decision,
    build_shadow_recommendation_decision,
    resolve_recommendation_activity_state,
)
from .observation.construction import (
    PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
    PROACTIVE_RECOMMENDATION_GIT_REVISION,
    build_recommendation_observation,
    build_recommendation_review_context,
)
from .feedback.service import (
    FeedbackService,
    ProactiveRecommendationFeedbackTurnSink,
    configure_feedback_logged_hook,
)
from .state.source_preferences import (
    get_recommendation_preference_state,
    reset_recommendation_preference_state,
)
from .tuning.service import (
    TuningService,
)


VALID_RECOMMENDATION_MODES = frozenset({"off", "shadow", "active_source"})


def _normalize_runtime_mode(value: Any) -> str:
    mode = to_stripped_text(value).lower()
    return mode if mode in VALID_RECOMMENDATION_MODES else "shadow"


class RecommendationRuntimeState:
    """Small synchronized state machine with no runtime activation path."""

    def __init__(self, startup_mode: str) -> None:
        self._lock = RLock()
        self._startup_mode = _normalize_runtime_mode(startup_mode)
        self._effective_mode = self._startup_mode
        self._rollback_count = 0
        self._last_rollback_at: float | None = None
        self._last_rollback_reason: str | None = None

    def mode(self) -> str:
        with self._lock:
            return self._effective_mode

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured_mode": self._startup_mode,
                "effective_mode": self._effective_mode,
                "active_source_enabled": self._effective_mode == "active_source",
                "bandit_configured_mode": PROACTIVE_RECOMMENDATION_BANDIT_MODE,
                "bandit_canary_effective": (
                    self._effective_mode == "active_source"
                    and PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE == "active"
                    and PROACTIVE_RECOMMENDATION_BANDIT_MODE == "canary"
                ),
                "activation_source": "startup_environment_only",
                "runtime_activation_allowed": False,
                "rollback_available": self._effective_mode == "active_source",
                "rollback_target": "shadow",
                "rollback_count": self._rollback_count,
                "last_rollback_at": self._last_rollback_at,
                "last_rollback_reason": self._last_rollback_reason,
                "restart_restores_configured_mode": (
                    self._effective_mode != self._startup_mode
                ),
            }

    def rollback(
        self, *, reason: Any = None, now: float | None = None
    ) -> dict[str, Any]:
        clean_reason = str(reason or "developer_runtime_rollback").strip()[:120]
        with self._lock:
            previous = self._effective_mode
            applied = previous == "active_source"
            if applied:
                self._effective_mode = "shadow"
                self._rollback_count += 1
                self._last_rollback_at = time.time() if now is None else float(now)
                self._last_rollback_reason = (
                    clean_reason or "developer_runtime_rollback"
                )
            return {
                "applied": applied,
                "previous_mode": previous,
                "status": self.status(),
            }


_RUNTIME = RecommendationRuntimeState(PROACTIVE_RECOMMENDATION_MODE)


def get_recommendation_runtime_mode() -> str:
    return _RUNTIME.mode()


def get_recommendation_runtime_status() -> dict[str, Any]:
    return _RUNTIME.status()


def rollback_recommendation_runtime(*, reason: Any = None) -> dict[str, Any]:
    return _RUNTIME.rollback(reason=reason)


_SHADOW_HISTORY_MAX = 20


_shadow_history: dict[str, deque[dict[str, Any]]] = {}


_shadow_history_lock = threading.RLock()


def record_shadow_selection(
    lanlan_name: str,
    decision: Any,
    *,
    now: float | None = None,
) -> None:
    selected = getattr(decision, "selected_candidate", None)
    source_type = getattr(selected, "source_type", None)
    candidate_id = getattr(selected, "id", None)
    if not source_type and not candidate_id:
        return
    with _shadow_history_lock:
        history = _shadow_history.setdefault(
            lanlan_name,
            deque(maxlen=_SHADOW_HISTORY_MAX),
        )
        history.append(
            {
                "ts": time.time() if now is None else now,
                "source_type": str(source_type or ""),
                "candidate_id": str(candidate_id or ""),
            }
        )


def recent_shadow_values(lanlan_name: str, field_name: str) -> list[str]:
    with _shadow_history_lock:
        return [
            str(item.get(field_name) or "")
            for item in _shadow_history.get(lanlan_name, ())
            if item.get(field_name)
        ]


def recent_shadow_sources(lanlan_name: str) -> list[str]:
    return recent_shadow_values(lanlan_name, "source_type")


def recent_shadow_candidate_ids(lanlan_name: str) -> list[str]:
    return recent_shadow_values(lanlan_name, "candidate_id")


DELIVERY_TIMING_HISTORY_MAX = 512


DELIVERY_TIMING_MAX_AGE_SECONDS = 2 * 60 * 60


_delivery_timing_history: dict[str, deque[float]] = {}


def record_proactive_delivery_for_timing(
    lanlan_name: str,
    *,
    delivered_at: float | None = None,
) -> None:
    """Record a real proactive delivery without affecting recommendation logic."""
    name = str(lanlan_name or "").strip()
    if not name:
        return
    timestamp = time.time() if delivered_at is None else float(delivered_at)
    history = _delivery_timing_history.get(name)
    if history is None:
        history = deque(maxlen=DELIVERY_TIMING_HISTORY_MAX)
        _delivery_timing_history[name] = history
    history.append(timestamp)
    _prune_delivery_timing_history(history, now=timestamp)


def proactive_delivery_timing_snapshot(
    lanlan_name: str,
    *,
    configured_interval_seconds: object = None,
    now: float | None = None,
) -> dict[str, int | float | None]:
    """Freeze timing features before the current proactive turn can deliver."""
    current = time.time() if now is None else float(now)
    history = _delivery_timing_history.get(str(lanlan_name or "").strip())
    if history is not None:
        _prune_delivery_timing_history(history, now=current)
    timestamps = list(history or ())
    last_delivery = timestamps[-1] if timestamps else None
    elapsed = (
        max(0.0, current - last_delivery)
        if last_delivery is not None and last_delivery <= current
        else None
    )
    return {
        "configured_interval_seconds": _optional_nonnegative_seconds(
            configured_interval_seconds
        ),
        "elapsed_since_last_delivery_seconds": (
            round(elapsed, 3) if elapsed is not None else None
        ),
        "recent_delivery_count_30m": sum(
            0 <= current - timestamp <= 30 * 60 for timestamp in timestamps
        ),
        "recent_delivery_count_2h": sum(
            0 <= current - timestamp <= DELIVERY_TIMING_MAX_AGE_SECONDS
            for timestamp in timestamps
        ),
    }


def clear_proactive_delivery_timing_history() -> None:
    """Clear process-local timing state; intended for tests and clean shutdown."""
    _delivery_timing_history.clear()


def _prune_delivery_timing_history(
    history: deque[float],
    *,
    now: float,
) -> None:
    cutoff = now - DELIVERY_TIMING_MAX_AGE_SECONDS
    while history and history[0] < cutoff:
        history.popleft()


def _optional_nonnegative_seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)


def _record_proactive_recommendation_shadow_selection(
    lanlan_name: str, decision: Any, *, now: float | None = None
) -> None:
    record_shadow_selection(lanlan_name, decision, now=now)


def _recent_proactive_recommendation_shadow_sources(lanlan_name: str) -> list[str]:
    return recent_shadow_sources(lanlan_name)


def _recent_proactive_recommendation_shadow_candidate_ids(
    lanlan_name: str,
) -> list[str]:
    return recent_shadow_candidate_ids(lanlan_name)


def _load_tuning_adjustments(config_dir: Any | None) -> dict[str, float]:
    if PROACTIVE_RECOMMENDATION_TUNING_MODE not in ("manual", "auto_safe"):
        return {}
    try:
        status = tuning_public_status(load_recommendation_tuning(config_dir=config_dir))
        if not status.get("enabled"):
            return {}
        return {
            str(source): float(adjustment)
            for source, adjustment in (
                status.get("source_type_adjustment") or {}
            ).items()
        }
    except Exception:
        return {}


def _record_proactive_recommendation_observation(
    decision: Any,
    *,
    lanlan_name: str,
    response_body: dict[str, Any],
    recommendation_mode: str,
    active_bias: Any = None,
    observation_log_mode: str = "off",
    config_dir: Any | None = None,
    ts: float | None = None,
    activity_state: Any = None,
    activity_propensity: Any = None,
    review_context_mode: str = "off",
    delivered_text: Any = None,
    decision_context: Mapping[str, Any] | None = None,
    feedback_state_snapshot: Mapping[str, Any] | None = None,
    preference_state_snapshot: Mapping[str, Any] | None = None,
    policy_decision: Mapping[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """Build and optionally persist one finalized recommendation observation."""
    if decision is None:
        return None
    turn_id = str(response_body.get("turn_id") or "").strip()
    if not turn_id:
        turn_id = str(uuid4())
        response_body["turn_id"] = turn_id
    effective_review_mode = "off"
    if review_context_mode == "testbench":
        effective_review_mode = "testbench"
    elif recommendation_mode == "shadow" and review_context_mode == "shadow_review":
        effective_review_mode = "shadow_review"
    review_context = build_recommendation_review_context(
        decision,
        mode=effective_review_mode,
        activity_state=activity_state,
        delivered_text=delivered_text,
    )
    observation_ts = time.time() if ts is None else ts
    raw_observation = build_recommendation_observation(
        decision,
        recommendation_mode=recommendation_mode,
        active_bias=active_bias,
        action=response_body.get("action"),
        reason_code=response_body.get("reason_code"),
        stage=response_body.get("stage"),
        source_mode=response_body.get("source_mode"),
        channel=response_body.get("channel"),
        source_tag=response_body.get("source_tag"),
        active_channels=response_body.get("active_channels"),
        source_links=response_body.get("source_links"),
        ts=observation_ts,
        lanlan_name=lanlan_name,
        turn_id=turn_id,
        activity_state=activity_state,
        activity_propensity=activity_propensity,
        algorithm_version=PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
        git_revision=PROACTIVE_RECOMMENDATION_GIT_REVISION,
        review_context=review_context,
        decision_context=decision_context,
        policy_decision=policy_decision,
    )
    if feedback_state_snapshot:
        state_snapshot = dict(feedback_state_snapshot)
        ranking_consumed = bool(
            decision.personalization
            and decision.personalization.get("ranking_consumed") is True
        )
        state_snapshot["preview_only"] = not ranking_consumed
        state_snapshot["ranking_consumed"] = ranking_consumed
        raw_observation["feedback_state_preview"] = state_snapshot
    if preference_state_snapshot:
        raw_observation["preference_state"] = dict(preference_state_snapshot)
    elif recommendation_mode == "shadow" and config_dir is not None:
        raw_observation["feedback_state_preview"] = get_feedback_state_preview(
            config_dir=config_dir, now=observation_ts
        )
    observation = sanitize_recommendation_observation(raw_observation)
    if log is not None:
        log.info(
            "[%s] proactive recommendation observation: %s",
            lanlan_name,
            json.dumps(observation, ensure_ascii=False),
        )
    if observation_log_mode == "jsonl":
        append_recommendation_observation_jsonl(
            observation,
            log_mode=observation_log_mode,
            config_dir=config_dir,
        )
    register_pending_feedback_from_observation(
        observation,
        log_mode=PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
        config_dir=config_dir,
    )
    record_shadow_selection(lanlan_name, decision, now=observation.get("ts"))
    return observation


@dataclass
class RecommendationTurn:
    """Point-in-time recommendation state owned by one proactive-chat turn."""

    lanlan_name: str
    configured_interval_seconds: Any = None
    config_dir: Any | None = None
    log: logging.Logger | None = None
    recent_sources: Sequence[str] = ()
    mode: str = field(init=False)
    decision_context: dict[str, Any] = field(init=False)
    personalization_mode: str = field(init=False)
    feedback_state_snapshot: dict[str, Any] = field(init=False)
    personalization_plan: dict[str, Any] = field(init=False)
    bandit_mode: str = field(init=False)
    preference_state_snapshot: dict[str, Any] = field(init=False)
    bandit_state_snapshot: dict[str, Any] = field(init=False)
    tuning_adjustments_snapshot: dict[str, float] = field(init=False)
    policy_decision: dict[str, Any] | None = None
    activity_snapshot: Any = None
    source_decision: Any = None
    material_decision: Any = None
    active_bias: Any = None
    delivered_text: str | None = None

    @classmethod
    async def create(
        cls,
        *,
        lanlan_name: str,
        configured_interval_seconds: Any = None,
        config_dir: Any | None = None,
        log: logging.Logger | None = None,
        recent_sources: Sequence[str] = (),
    ) -> "RecommendationTurn":
        """Load file-backed snapshots outside the asyncio event-loop thread."""
        return await asyncio.to_thread(
            cls,
            lanlan_name=lanlan_name,
            configured_interval_seconds=configured_interval_seconds,
            config_dir=config_dir,
            log=log,
            recent_sources=tuple(recent_sources),
        )

    def __post_init__(self) -> None:
        self.mode = get_recommendation_runtime_mode()
        now = time.time()
        self.personalization_mode = PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE
        self.bandit_mode = PROACTIVE_RECOMMENDATION_BANDIT_MODE
        self.feedback_state_snapshot = get_feedback_state_preview(
            config_dir=self.config_dir,
            now=now,
        )
        self.preference_state_snapshot = (
            ensure_recommendation_preference_state(
                config_dir=self.config_dir,
                legacy_preview=self.feedback_state_snapshot,
                now=now,
            )
            if self.personalization_mode != "off" or self.bandit_mode != "off"
            else {}
        )
        self.bandit_state_snapshot = (
            get_recommendation_bandit_state(config_dir=self.config_dir, now=now)
            if self.bandit_mode != "off"
            else {}
        )
        self.personalization_plan = build_personalization_plan(
            self.feedback_state_snapshot,
            mode=self.personalization_mode,
        )
        self.tuning_adjustments_snapshot = _load_tuning_adjustments(self.config_dir)
        timing = proactive_delivery_timing_snapshot(
            self.lanlan_name,
            configured_interval_seconds=self.configured_interval_seconds,
            now=now,
        )
        timing["consecutive_unanswered_deliveries"] = (
            consecutive_unanswered_recommendation_deliveries(self.lanlan_name, now=now)
        )
        self.decision_context = {"timing": timing}

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "active_source")

    def _context(
        self,
        *,
        enabled_modes: Sequence[str],
        source_weights: Mapping[str, float],
    ) -> ProactiveRecommendationContext:
        return ProactiveRecommendationContext(
            lanlan_name=self.lanlan_name,
            enabled_modes=tuple(enabled_modes),
            source_weights=dict(source_weights),
            source_type_adjustments=self.tuning_adjustments_snapshot,
            recent_sources=tuple(self.recent_sources),
            recent_shadow_sources=recent_shadow_values(self.lanlan_name, "source_type"),
            recent_candidate_ids=recent_shadow_values(self.lanlan_name, "candidate_id"),
            privacy_state=("open" if self.activity_snapshot is not None else "unknown"),
            activity_state=resolve_recommendation_activity_state(
                self.activity_snapshot
            ),
            mini_game_available=MINI_GAME_INVITE_ENABLED,
            personalization_mode=self.personalization_mode,
            personalization_adjustments=(
                preference_adjustments(self.preference_state_snapshot)
                or personalization_adjustments(self.personalization_plan)
            ),
        )

    def decide_source(
        self,
        *,
        enabled_modes: Sequence[str],
        source_weights: Mapping[str, float],
        sources: Mapping[str, Any],
    ) -> None:
        if self.enabled:
            self.source_decision = build_shadow_recommendation_decision(
                self._context(
                    enabled_modes=enabled_modes,
                    source_weights=source_weights,
                ),
                sources,
            )

    def decide_material(
        self,
        *,
        enabled_modes: Sequence[str],
        source_weights: Mapping[str, float],
        phase1_topics: list[tuple[str, str]],
        selected_web_link: Any,
        selected_music_link: Any,
        selected_meme_link: Any,
        vision_content: Any,
        active_channels: Sequence[str],
    ) -> list[tuple[str, str]]:
        if not self.enabled:
            return phase1_topics
        self.material_decision = build_phase1_material_shadow_decision(
            self._context(
                enabled_modes=enabled_modes,
                source_weights=source_weights,
            ),
            phase1_topics=phase1_topics,
            selected_web_link=selected_web_link,
            selected_music_link=selected_music_link,
            selected_meme_link=selected_meme_link,
            vision_content=vision_content,
            active_channels=active_channels,
        )
        effective_bandit_mode = self.bandit_mode
        if effective_bandit_mode == "canary" and not (
            self.mode == "active_source" and self.personalization_mode == "active"
        ):
            effective_bandit_mode = "shadow"
        if effective_bandit_mode != "off":
            self.policy_decision = build_source_bandit_decision(
                self.material_decision,
                mode=effective_bandit_mode,
                preference_state=self.preference_state_snapshot,
                bandit_state=self.bandit_state_snapshot,
                score_contract=(
                    BANDIT_PERSONALIZED_SCORE_CONTRACT
                    if self.personalization_mode in {"shadow_compare", "active"}
                    else BANDIT_BASELINE_SCORE_CONTRACT
                ),
            )
        if self.mode != "active_source":
            return phase1_topics
        if effective_bandit_mode == "canary" and self.policy_decision:
            preferred = bandit_preferred_candidate(
                self.material_decision, self.policy_decision
            )
            if preferred is not None:
                tag = {
                    "news": "WEB",
                    "web": "WEB",
                    "music": "MUSIC",
                    "meme": "MEME",
                }.get(str(preferred.source_type).lower())
                if tag:
                    self.active_bias = ProactiveActiveBias(
                        applied=True,
                        preferred_source_type=preferred.source_type,
                        preferred_source_tag=tag,
                        preferred_candidate_id=preferred.id,
                        score_gap=0.0,
                    )
                    return reorder_phase1_topics_for_bias(
                        phase1_topics, self.active_bias
                    )
        self.active_bias = build_active_source_bias(
            self.material_decision,
            min_score_gap=PROACTIVE_RECOMMENDATION_ACTIVE_MIN_SCORE_GAP,
        )
        return reorder_phase1_topics_for_bias(phase1_topics, self.active_bias)

    async def finalize(self, response_body: dict[str, Any]) -> None:
        decision = self.material_decision or self.source_decision
        if not self.enabled or decision is None:
            return
        snapshot = self.activity_snapshot
        await asyncio.to_thread(
            _record_proactive_recommendation_observation,
            decision,
            lanlan_name=self.lanlan_name,
            response_body=response_body,
            recommendation_mode=self.mode,
            active_bias=self.active_bias,
            observation_log_mode=PROACTIVE_RECOMMENDATION_OBSERVATION_LOG,
            config_dir=self.config_dir,
            activity_state=(
                getattr(snapshot, "state", "unknown")
                if snapshot is not None
                else "unknown"
            ),
            activity_propensity=(
                getattr(snapshot, "propensity", "unknown")
                if snapshot is not None
                else "unknown"
            ),
            review_context_mode=PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE,
            delivered_text=self.delivered_text,
            decision_context=self.decision_context,
            feedback_state_snapshot=self.feedback_state_snapshot,
            preference_state_snapshot=(
                self.preference_state_snapshot
                if self.personalization_mode != "off" or self.bandit_mode != "off"
                else None
            ),
            policy_decision=self.policy_decision,
            log=self.log,
        )


class RecommendationService:
    def __init__(self) -> None:
        self.feedback = FeedbackService()
        self.tuning = TuningService()
        configure_feedback_logged_hook(self._after_feedback_logged)

    async def create_turn(
        self,
        *,
        lanlan_name: str,
        configured_interval_seconds: Any = None,
        config_dir: Any = None,
        log: logging.Logger | None = None,
        recent_sources: Sequence[str] = (),
    ) -> RecommendationTurn:
        return await RecommendationTurn.create(
            lanlan_name=lanlan_name,
            configured_interval_seconds=configured_interval_seconds,
            config_dir=config_dir,
            log=log,
            recent_sources=recent_sources,
        )

    async def record_feedback(
        self,
        command: RecordFeedbackCommand,
    ) -> RecommendationFeedbackRecordResult:
        return await asyncio.to_thread(self.record_feedback_sync, command)

    def record_feedback_sync(
        self,
        command: RecordFeedbackCommand,
    ) -> RecommendationFeedbackRecordResult:
        return self.feedback.record_event(command)

    async def get_preference_state(self, *, config_dir: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            get_recommendation_preference_state,
            config_dir=config_dir,
        )

    async def reset_preference_state(self, *, config_dir: Any) -> bool:
        return await asyncio.to_thread(
            reset_recommendation_preference_state,
            config_dir=config_dir,
        )

    async def get_tuning_status(self, *, config_dir: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.tuning.status, config_dir=config_dir)

    async def reset_tuning(self, *, config_dir: Any) -> dict[str, Any]:
        await asyncio.to_thread(self.tuning.reset, config_dir=config_dir)
        return await self.get_tuning_status(config_dir=config_dir)

    async def pause_tuning(
        self,
        *,
        config_dir: Any,
        duration_seconds: int,
        reason: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.tuning.pause,
            config_dir=config_dir,
            duration_seconds=duration_seconds,
            reason=reason,
        )

    async def resume_tuning(self, *, config_dir: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.tuning.resume,
            config_dir=config_dir,
        )

    async def build_summary(
        self,
        query: RecommendationSummaryQuery,
        *,
        config_dir: Any,
    ) -> dict[str, Any]:
        """Build the read-only recommendation diagnostics response."""
        threshold = clamp_to_range(
            coerce_finite_float(
                query.high_score_threshold,
                default=DEFAULT_HIGH_SCORE_THRESHOLD,
            ),
            0.0,
            1.0,
        )
        observation_path = resolve_persistence_path(
            explicit_path=None,
            config_directory=config_dir,
            filename=OBSERVATION_LOG_FILENAME,
        )
        feedback_path = resolve_persistence_path(
            explicit_path=None,
            config_directory=config_dir,
            filename=FEEDBACK_LOG_FILENAME,
        )
        observation_missing = observation_path is None or not observation_path.exists()
        feedback_missing = feedback_path is None or not feedback_path.exists()
        observations = (
            []
            if observation_missing
            else await asyncio.to_thread(
                load_recommendation_observations_jsonl,
                observation_path,
                limit=CALIBRATION_SAMPLE_LIMIT,
            )
        )
        feedback_events = (
            []
            if feedback_missing
            else await asyncio.to_thread(
                load_recommendation_feedback_jsonl,
                feedback_path,
                limit=CALIBRATION_SAMPLE_LIMIT * 4,
            )
        )
        current_time = time.time()
        calibration_samples = get_recommendation_calibration_samples(
            observations,
            now=current_time,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        calibration = summarize_recommendation_calibration(
            observations,
            now=current_time,
            high_score_threshold=threshold,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        validation = summarize_recommendation_validation(
            observations,
            now=current_time,
            high_score_threshold=threshold,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        feedback = summarize_recommendation_feedback(
            calibration_samples,
            feedback_events,
            now=current_time,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        feedback_calibration = summarize_feedback_calibration(
            calibration_samples,
            feedback_events,
            now=current_time,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        reward_preview = summarize_reward_score_v2_preview(
            calibration_samples,
            feedback_events,
            now=current_time,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        reward_v3_preview = summarize_reward_score_v3_preview(
            calibration_samples,
            feedback_events,
            now=current_time,
            window_seconds=CALIBRATION_WINDOW_SECONDS,
            sample_limit=CALIBRATION_SAMPLE_LIMIT,
        )
        tuning = load_recommendation_tuning(config_dir=config_dir)
        payload: dict[str, Any] = {
            "ok": True,
            "missing": observation_missing,
            "log_enabled": PROACTIVE_RECOMMENDATION_OBSERVATION_LOG == "jsonl",
            "summary": calibration["summary"],
            "calibration": calibration,
            "validation": validation,
            "feedback": feedback,
            "feedback_calibration": feedback_calibration,
            "reward_score_v2_preview": reward_preview,
            "reward_score_v3_preview": reward_v3_preview,
            "review_context_validation": summarize_recommendation_review_context(
                calibration_samples
            ),
            "policy_monitor": summarize_recommendation_policy(calibration_samples),
            "bandit_learning": get_recommendation_bandit_state(
                config_dir=config_dir,
                now=current_time,
            ),
            "manual_tuning_preview": feedback_calibration.get(
                "manual_tuning_preview", {}
            ),
            "runtime": self.get_runtime_status(),
            "tuning": tuning_public_status(tuning),
            "sample_count": calibration["sample_count"],
            "retention": {
                "filename": OBSERVATION_LOG_FILENAME,
                "feedback_filename": FEEDBACK_LOG_FILENAME,
                "tuning_filename": TUNING_FILENAME,
                "sample_window_seconds": CALIBRATION_WINDOW_SECONDS,
                "sample_limit": CALIBRATION_SAMPLE_LIMIT,
                "requested_limit_ignored": query.limit is not None,
                "high_score_threshold": threshold,
                "examples_default_limit": DEFAULT_EXAMPLE_LIMIT,
                "examples_max_limit": MAX_EXAMPLE_LIMIT,
                "rotate_bytes": DEFAULT_ROTATE_BYTES,
                "feedback_missing": feedback_missing,
                "feedback_log_enabled": PROACTIVE_RECOMMENDATION_FEEDBACK_LOG
                == "jsonl",
                "tuning_mode": PROACTIVE_RECOMMENDATION_TUNING_MODE,
                "review_context_mode": PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE,
            },
        }
        if query.include_examples:
            payload["examples"] = select_recommendation_observation_examples(
                calibration_samples,
                high_score_threshold=threshold,
                limit=DEFAULT_EXAMPLE_LIMIT,
            )
        return payload

    def get_runtime_status(self) -> dict[str, Any]:
        return get_recommendation_runtime_status()

    def rollback_runtime(self, *, reason: Any = None) -> dict[str, Any]:
        return rollback_recommendation_runtime(reason=reason)

    def feedback_turn_sink(self) -> ProactiveRecommendationFeedbackTurnSink:
        return ProactiveRecommendationFeedbackTurnSink()

    def _after_feedback_logged(self, config_dir: Any) -> None:
        if config_dir is None or PROACTIVE_RECOMMENDATION_TUNING_MODE != "auto_safe":
            return
        result = self.tuning.maybe_auto_apply_from_logs(
            mode=PROACTIVE_RECOMMENDATION_TUNING_MODE,
            config_dir=config_dir,
        )
        if not result.get("applied") and not result.get("rollback_applied"):
            self.tuning.update_health_from_logs(
                mode=PROACTIVE_RECOMMENDATION_TUNING_MODE,
                config_dir=config_dir,
            )


_service = RecommendationService()


def get_recommendation_service() -> RecommendationService:
    return _service
