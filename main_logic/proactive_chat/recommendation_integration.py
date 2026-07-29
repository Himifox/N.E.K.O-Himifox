# -*- coding: utf-8 -*-
"""Recommendation observation integration for proactive-chat turns."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from config import (
    MINI_GAME_INVITE_ENABLED,
    PROACTIVE_RECOMMENDATION_ACTIVE_MIN_SCORE_GAP,
    PROACTIVE_RECOMMENDATION_BANDIT_MODE,
    PROACTIVE_RECOMMENDATION_EXPLICIT_FEEDBACK_UI,
    PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
    PROACTIVE_RECOMMENDATION_OBSERVATION_LOG,
    PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE,
    PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE,
    PROACTIVE_RECOMMENDATION_TUNING_MODE,
)
from main_logic.proactive_recommendation import (
    PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
    PROACTIVE_RECOMMENDATION_GIT_REVISION,
    ProactiveRecommendationContext,
    ProactiveActiveBias,
    build_active_source_bias,
    build_phase1_material_shadow_decision,
    build_recommendation_observation,
    build_recommendation_review_context,
    build_shadow_recommendation_decision,
    reorder_phase1_topics_for_bias,
    resolve_recommendation_activity_state,
)
from main_logic.proactive_recommendation_feedback import (
    consecutive_unanswered_recommendation_deliveries,
    register_pending_feedback_from_observation,
)
from main_logic.proactive_recommendation_feedback_state import get_feedback_state_preview
from main_logic.proactive_recommendation_bandit import (
    BANDIT_BASELINE_SCORE_CONTRACT,
    BANDIT_PERSONALIZED_SCORE_CONTRACT,
    bandit_preferred_candidate,
    build_source_bandit_decision,
)
from main_logic.proactive_recommendation_preference import (
    ensure_recommendation_preference_state,
    preference_adjustments,
)
from main_logic.proactive_recommendation_personalization import (
    build_personalization_plan,
    personalization_adjustments,
)
from main_logic.proactive_recommendation_observer import (
    append_recommendation_observation_jsonl,
    sanitize_recommendation_observation,
)
from main_logic.proactive_recommendation_runtime import get_recommendation_runtime_mode
from main_logic.proactive_recommendation_timing import proactive_delivery_timing_snapshot
from main_logic.proactive_recommendation_tuning import (
    load_recommendation_tuning,
    tuning_public_status,
)

from .state import _recent_proactive_chat_entries


_SHADOW_HISTORY_MAX = 20
_shadow_history: dict[str, deque[dict[str, Any]]] = {}
_EXPLICIT_FEEDBACK_SOURCES = {
    "news",
    "web",
    "meme",
    "vision",
    "window",
    "video",
    "music",
}


def _explicit_feedback_context(
    observation: Mapping[str, Any] | None,
    *,
    recommendation_mode: str,
) -> dict[str, Any] | None:
    """Expose only the facts needed to render scoped feedback actions."""
    ui_mode = PROACTIVE_RECOMMENDATION_EXPLICIT_FEEDBACK_UI
    mode_matches = (
        ui_mode == "shadow" and recommendation_mode == "shadow"
    ) or (
        ui_mode == "active" and recommendation_mode == "active_source"
    )
    if (
        not mode_matches
        or not isinstance(observation, Mapping)
        or observation.get("delivered") is not True
    ):
        return None
    turn_id = str(observation.get("turn_id") or "").strip()
    if not turn_id:
        return None
    source = str(
        observation.get("active_preferred_source_type")
        if observation.get("active_bias_applied") is True
        else observation.get("shadow_selected_source_type")
        or ""
    ).strip().lower()
    candidate_id = (
        observation.get("active_preferred_candidate_id")
        if observation.get("active_bias_applied") is True
        else observation.get("shadow_selected_candidate_id")
    )
    source_available = bool(
        observation.get("matched_actual_material") is True
        and candidate_id
        and source in _EXPLICIT_FEEDBACK_SOURCES
    )
    display_source = {"web": "news", "window": "vision"}.get(source, source)
    return {
        "turn_id": turn_id,
        "source_type": display_source if source_available else None,
        "source_feedback_available": source_available,
        "ui_generation": "dual_scope_v1",
    }


def _record_shadow_selection(
    lanlan_name: str, decision: Any, *, now: float | None = None
) -> None:
    selected = getattr(decision, "selected_candidate", None)
    source_type = getattr(selected, "source_type", None)
    candidate_id = getattr(selected, "id", None)
    if not source_type and not candidate_id:
        return
    history = _shadow_history.setdefault(
        lanlan_name, deque(maxlen=_SHADOW_HISTORY_MAX)
    )
    history.append(
        {
            "ts": time.time() if now is None else now,
            "source_type": str(source_type or ""),
            "candidate_id": str(candidate_id or ""),
        }
    )


def _recent_shadow_values(lanlan_name: str, field_name: str) -> list[str]:
    return [
        str(item.get(field_name) or "")
        for item in _shadow_history.get(lanlan_name, ())
        if item.get(field_name)
    ]


def _record_proactive_recommendation_shadow_selection(
    lanlan_name: str, decision: Any, *, now: float | None = None
) -> None:
    _record_shadow_selection(lanlan_name, decision, now=now)


def _recent_proactive_recommendation_shadow_sources(lanlan_name: str) -> list[str]:
    return _recent_shadow_values(lanlan_name, "source_type")


def _recent_proactive_recommendation_shadow_candidate_ids(
    lanlan_name: str,
) -> list[str]:
    return _recent_shadow_values(lanlan_name, "candidate_id")


def _load_tuning_adjustments(config_dir: Any | None) -> dict[str, float]:
    if PROACTIVE_RECOMMENDATION_TUNING_MODE not in ("manual", "auto_safe"):
        return {}
    try:
        status = tuning_public_status(
            load_recommendation_tuning(config_dir=config_dir)
        )
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
    _record_shadow_selection(lanlan_name, decision, now=observation.get("ts"))
    return observation


@dataclass(slots=True)
class RecommendationTurn:
    """Point-in-time recommendation state owned by one proactive-chat turn."""

    lanlan_name: str
    configured_interval_seconds: Any = None
    config_dir: Any | None = None
    log: logging.Logger | None = None
    mode: str = field(init=False)
    decision_context: dict[str, Any] = field(init=False)
    personalization_mode: str = field(init=False)
    feedback_state_snapshot: dict[str, Any] = field(init=False)
    personalization_plan: dict[str, Any] = field(init=False)
    bandit_mode: str = field(init=False)
    preference_state_snapshot: dict[str, Any] = field(init=False)
    policy_decision: dict[str, Any] | None = None
    activity_snapshot: Any = None
    source_decision: Any = None
    material_decision: Any = None
    active_bias: Any = None
    delivered_text: str | None = None

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
        self.personalization_plan = build_personalization_plan(
            self.feedback_state_snapshot,
            mode=self.personalization_mode,
        )
        timing = proactive_delivery_timing_snapshot(
            self.lanlan_name,
            configured_interval_seconds=self.configured_interval_seconds,
            now=now,
        )
        timing["consecutive_unanswered_deliveries"] = (
            consecutive_unanswered_recommendation_deliveries(
                self.lanlan_name, now=now
            )
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
        recent_sources = [
            str(entry[2])
            for entry in _recent_proactive_chat_entries(self.lanlan_name)
            if len(entry) > 2 and entry[2]
        ]
        return ProactiveRecommendationContext(
            lanlan_name=self.lanlan_name,
            enabled_modes=tuple(enabled_modes),
            source_weights=dict(source_weights),
            source_type_adjustments=_load_tuning_adjustments(self.config_dir),
            recent_sources=recent_sources,
            recent_shadow_sources=_recent_shadow_values(
                self.lanlan_name, "source_type"
            ),
            recent_candidate_ids=_recent_shadow_values(
                self.lanlan_name, "candidate_id"
            ),
            privacy_state=(
                "open" if self.activity_snapshot is not None else "unknown"
            ),
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

    def finalize(self, response_body: dict[str, Any]) -> None:
        decision = self.material_decision or self.source_decision
        if not self.enabled or decision is None:
            return
        snapshot = self.activity_snapshot
        observation = _record_proactive_recommendation_observation(
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
        feedback_context = _explicit_feedback_context(
            observation,
            recommendation_mode=self.mode,
        )
        if feedback_context is not None:
            response_body["feedback_context"] = feedback_context
