# -*- coding: utf-8 -*-
"""Proactive-chat adapter for the recommendation domain package."""

from main_logic.proactive_recommendation.service import (
    RecommendationTurn,
    _record_proactive_recommendation_observation,
    _record_proactive_recommendation_shadow_selection,
    _recent_proactive_recommendation_shadow_candidate_ids,
    _recent_proactive_recommendation_shadow_sources,
)

__all__ = [
    "RecommendationTurn",
    "_record_proactive_recommendation_observation",
    "_record_proactive_recommendation_shadow_selection",
    "_recent_proactive_recommendation_shadow_candidate_ids",
    "_recent_proactive_recommendation_shadow_sources",
]
