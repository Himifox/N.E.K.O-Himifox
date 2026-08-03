"""Proactive recommendation domain package."""

from .domain_models import (
    ProactiveActiveBias,
    ProactiveCandidate,
    ProactiveRecommendationContext,
    ProactiveRecommendationDecision,
)
from .engine.active_source_bias import (
    build_active_source_bias,
    reorder_phase1_topics_for_bias,
    source_type_to_phase2_tag,
)
from .engine.candidate_builder import build_candidates
from .engine.source_selection import (
    build_phase1_material_shadow_decision,
    build_shadow_recommendation_decision,
    resolve_recommendation_activity_state,
)
from .observation.builder import (
    PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
    PROACTIVE_RECOMMENDATION_GIT_REVISION,
    build_recommendation_observation,
    build_recommendation_review_context,
)

__all__ = [
    "PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION",
    "PROACTIVE_RECOMMENDATION_GIT_REVISION",
    "ProactiveActiveBias",
    "ProactiveCandidate",
    "ProactiveRecommendationContext",
    "ProactiveRecommendationDecision",
    "build_active_source_bias",
    "build_candidates",
    "build_phase1_material_shadow_decision",
    "build_recommendation_observation",
    "build_recommendation_review_context",
    "build_shadow_recommendation_decision",
    "reorder_phase1_topics_for_bias",
    "resolve_recommendation_activity_state",
    "source_type_to_phase2_tag",
]
