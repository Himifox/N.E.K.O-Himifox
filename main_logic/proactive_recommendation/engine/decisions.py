"""Lightweight candidate ranking for proactive chat source selection.

The module is intentionally pure: it does not fetch sources, call LLMs, deliver
messages, or write history. The proactive endpoint can run it in shadow mode to
observe which source a rule-based recommender would prefer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from main_logic.proactive_recommendation.domain_models import (
    ProactiveRecommendationContext,
    ProactiveRecommendationDecision,
)
from main_logic.proactive_recommendation.engine.candidates import (
    build_candidates,
    build_phase1_material_candidates,
)
from main_logic.proactive_recommendation.engine.scoring import rank_candidates


def resolve_recommendation_activity_state(activity_snapshot: Any) -> str:
    """Return the inferred activity state, never its collapsed propensity.

    Ranking owns state-sensitive costs (for example ``away`` and
    ``focused_work``).  ``propensity`` belongs to the upstream router and
    deliberately collapses several states, so substituting it here silently
    disables those ranking branches.
    """
    if activity_snapshot is None:
        return "unknown"
    return _text(getattr(activity_snapshot, "state", None)) or "unknown"


def build_shadow_recommendation_decision(
    ctx: ProactiveRecommendationContext,
    sources: Mapping[str, Mapping[str, Any]] | None,
) -> ProactiveRecommendationDecision:
    candidates = build_candidates(ctx, sources or {})
    return rank_candidates(ctx, candidates, decision_stage="pre_phase1_source")


def build_phase1_material_shadow_decision(
    ctx: ProactiveRecommendationContext,
    *,
    phase1_topics: Sequence[Any] = (),
    selected_web_link: Mapping[str, Any] | None = None,
    selected_music_link: Mapping[str, Any] | None = None,
    selected_meme_link: Mapping[str, Any] | None = None,
    vision_content: Mapping[str, Any] | None = None,
    active_channels: Sequence[Any] = (),
) -> ProactiveRecommendationDecision:
    """Rank concrete Phase-1 materials without changing the live pipeline."""
    candidates = build_phase1_material_candidates(
        ctx,
        phase1_topics=phase1_topics,
        selected_web_link=selected_web_link,
        selected_music_link=selected_music_link,
        selected_meme_link=selected_meme_link,
        vision_content=vision_content,
        active_channels=active_channels,
    )
    return rank_candidates(ctx, candidates, decision_stage="phase1_material")


def _text(value: Any) -> str:
    return str(value or "").strip()
