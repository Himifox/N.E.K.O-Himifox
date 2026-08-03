"""Learning eligibility and outcome rules for recommendation feedback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config import (
    PROACTIVE_RECOMMENDATION_BANDIT_MODE,
    PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE,
)

from ..engine.source_selection import BANDIT_ARMS
from .contracts import PendingRecommendationFeedback
from .events import _clean_text, _normalize_source_type


def source_affinity_event_matches_pending(
    event: Mapping[str, Any],
    pending: PendingRecommendationFeedback,
) -> bool:
    """Require an exact delivered source and material before learning affinity."""
    pending_candidate = _clean_text(pending.candidate_id)
    return bool(
        pending_candidate
        and _normalize_source_type(event.get("source_type")) == pending.source_type
        and _clean_text(event.get("candidate_id")) == pending_candidate
    )


def bandit_event_matches_pending(
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


def feedback_learning_enabled(pending: PendingRecommendationFeedback) -> bool:
    """Keep learning enabled in Shadow and explicitly personalized active runs."""
    if pending.recommendation_mode == "shadow":
        return True
    if pending.recommendation_mode != "active_source":
        return False
    return PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE in {
        "shadow_compare",
        "active",
    } or PROACTIVE_RECOMMENDATION_BANDIT_MODE in {"shadow", "canary"}


def source_preference_outcome(
    event_type: str,
) -> tuple[float, float, bool] | None:
    """Map verified material feedback to one source-level learning outcome."""
    outcomes = {
        "source_interested": (1.0, 0.0, True),
        "source_not_interested": (0.0, 1.0, True),
        "source_fatigue": (0.0, 0.5, False),
        "candidate_not_interested": (0.0, 0.25, False),
        "source_disabled_after": (0.0, 1.0, True),
        "music_played_through": (1.0, 0.0, False),
        "music_high_completion": (1.0, 0.0, False),
        "music_mid_completion": (0.5, 0.0, False),
        "music_early_close": (0.0, 1.0, False),
        "music_hard_skip": (0.0, 1.0, False),
    }
    return outcomes.get(event_type)
