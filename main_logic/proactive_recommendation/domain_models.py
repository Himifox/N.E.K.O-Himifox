"""Stable, side-effect-free domain models for proactive recommendation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import os
import time
from typing import Any

from .normalization import clamp_to_unit_interval


PERSISTENT_INTEREST_MIN_EVIDENCE = 3
PERSISTENT_AFFINITY_MAX = 0.20


@dataclass(slots=True)
class ProactiveCandidate:
    id: str
    source_type: str
    family: str
    topic: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    freshness: float = 0.5
    risk_flags: tuple[str, ...] = ()
    quality: float = 0.5
    score: float = 0.0

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "family": self.family,
            "topic": self.topic,
            "summary": self.summary,
            "freshness": round(clamp_to_unit_interval(self.freshness), 3),
            "risk_flags": list(self.risk_flags),
            "quality": round(clamp_to_unit_interval(self.quality), 3),
            "score": round(float(self.score), 3),
        }


@dataclass(slots=True)
class ProactiveRecommendationContext:
    lanlan_name: str
    enabled_modes: Sequence[str] = ()
    source_weights: Mapping[str, float] = field(default_factory=dict)
    source_type_adjustments: Mapping[str, float] = field(default_factory=dict)
    recent_sources: Sequence[str] = ()
    recent_shadow_sources: Sequence[str] = ()
    recent_candidate_ids: Sequence[str] = ()
    privacy_state: str = "open"
    activity_state: str = "unknown"
    topic_materials: Sequence[Mapping[str, Any]] = ()
    mini_game_available: bool = False
    personalization_mode: str = "off"
    personalization_adjustments: Mapping[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ProactiveRecommendationDecision:
    candidate_count: int
    selected_candidate: ProactiveCandidate | None
    decision_stage: str = "pre_phase1_source"
    ranked_candidates: tuple[ProactiveCandidate, ...] = ()
    filtered_reasons: dict[str, str] = field(default_factory=dict)
    score_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    shadow_selected_source_type: str | None = None
    personalization: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        selected = (
            self.selected_candidate.to_log_dict() if self.selected_candidate else None
        )
        return {
            "decision_stage": self.decision_stage,
            "candidate_count": self.candidate_count,
            "filtered_reasons": dict(self.filtered_reasons),
            "shadow_selected_source_type": self.shadow_selected_source_type,
            "shadow_selected_candidate_id": self.selected_candidate.id
            if self.selected_candidate
            else None,
            "shadow_selected_score": round(self.selected_candidate.score, 3)
            if self.selected_candidate
            else None,
            "selected_candidate": selected,
            "top_candidates": [
                {
                    "rank": index,
                    "id": candidate.id,
                    "source_type": candidate.source_type,
                    "family": candidate.family,
                    "topic": candidate.topic,
                    "score": round(float(candidate.score), 3),
                }
                for index, candidate in enumerate(self.ranked_candidates[:3], start=1)
            ],
            "score_breakdown": {
                key: {name: round(value, 3) for name, value in values.items()}
                for key, values in self.score_breakdown.items()
            },
            **(
                {"personalization": dict(self.personalization)}
                if self.personalization
                else {}
            ),
        }


@dataclass(slots=True)
class ProactiveActiveBias:
    applied: bool
    preferred_source_type: str | None = None
    preferred_source_tag: str | None = None
    preferred_candidate_id: str | None = None
    score_gap: float | None = None
    fallback_reason: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "preferred_source_type": self.preferred_source_type,
            "preferred_source_tag": self.preferred_source_tag,
            "preferred_candidate_id": self.preferred_candidate_id,
            "score_gap": round(float(self.score_gap), 3)
            if self.score_gap is not None
            else None,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True, slots=True)
class RecordFeedbackCommand:
    lanlan_name: str
    turn_id: str
    event_type: str
    source_type: Any = None
    candidate_id: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    log_mode: str | None = None
    config_dir: Any = None
    ts: float | None = None


@dataclass(frozen=True, slots=True)
class RecommendationSummaryQuery:
    limit: int | None = None
    high_score_threshold: float = 0.75
    include_examples: bool = False


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
    availability_activity_state: str = "unknown"
    availability_input_mode: str = "unknown"
    availability_finalized: bool = False


@dataclass(frozen=True, slots=True)
class PendingFeedbackClaim:
    pending: PendingRecommendationFeedback | None
    duplicate_event: bool = False
    duplicate_group: bool = False


@dataclass(frozen=True, slots=True)
class RecommendationFeedbackRecordResult:
    event: dict[str, Any] | None
    logged: bool
    state_updated: bool = False
    feedback_scope: str = "diagnostic_only"
    state_reason: str = "not_logged"
    preference_state_updated: bool = False
    bandit_state_updated: bool = False
