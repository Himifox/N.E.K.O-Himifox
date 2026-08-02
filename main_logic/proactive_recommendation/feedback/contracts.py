"""Feedback-domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import time
from typing import Any


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
