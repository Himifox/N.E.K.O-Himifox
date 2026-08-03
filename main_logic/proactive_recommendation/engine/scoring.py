"""Filtering, scoring, and ranking for proactive candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from main_logic.proactive_recommendation.domain_models import (
    ProactiveCandidate,
    ProactiveRecommendationContext,
    ProactiveRecommendationDecision,
)
from main_logic.proactive_recommendation.normalization import (
    clamp_to_unit_interval,
    coerce_float_or_default,
    to_stripped_text,
)

_PRIVACY_CLOSED_STATES = {"closed", "private", "privacy", "privacy_closed"}
_BUSY_STATES = {"busy", "voice_busy", "delivery_busy", "active", "away"}
_RESTRICTED_STATES = {"restricted_screen_only", "focused_work", "gaming"}
_PRIVACY_SENSITIVE_SOURCES = {"vision", "window", "personal"}
_CONTEXT_MATCH_WEIGHT = 0.25
_DIVERSITY_RECENT_SOURCE_LIMIT = 8
_DIVERSITY_SOURCE_REPEAT_STEP = 0.04
_DIVERSITY_SOURCE_REPEAT_MAX = 0.16
_DIVERSITY_SOURCE_STREAK_STEP = 0.06
_DIVERSITY_SOURCE_STREAK_MAX = 0.12
_DIVERSITY_CANDIDATE_REPEAT_PENALTY = 0.12
_DIVERSITY_PENALTY_MAX = 0.30
_SOURCE_TYPE_SCORE_ADJUSTMENTS = {"news": -0.05}
PERSONALIZATION_MAX_ABS_DELTA = 0.03


def rank_candidates(
    context: ProactiveRecommendationContext,
    candidates: Sequence[ProactiveCandidate],
    *,
    decision_stage: str,
) -> ProactiveRecommendationDecision:
    filtered: dict[str, str] = {}
    score_breakdown: dict[str, dict[str, float]] = {}
    ranked: list[ProactiveCandidate] = []

    for candidate in candidates:
        filter_reason = candidate_filter_reason(context, candidate)
        if filter_reason:
            filtered[candidate.id] = filter_reason
            continue
        score, breakdown = score_candidate(context, candidate)
        candidate.score = score
        score_breakdown[candidate.id] = breakdown
        ranked.append(candidate)

    ranked.sort(key=lambda item: item.score, reverse=True)
    selected = ranked[0] if ranked else None
    personalization = _personalization_diagnostics(
        context,
        ranked,
        score_breakdown,
    )
    return ProactiveRecommendationDecision(
        candidate_count=len(candidates),
        selected_candidate=selected,
        decision_stage=decision_stage,
        ranked_candidates=tuple(ranked),
        filtered_reasons=filtered,
        score_breakdown=score_breakdown,
        shadow_selected_source_type=selected.source_type if selected else None,
        personalization=personalization,
    )


def candidate_filter_reason(
    context: ProactiveRecommendationContext, candidate: ProactiveCandidate
) -> str | None:
    if candidate.source_type not in ("topic_hook", "mini_game"):
        enabled = set(context.enabled_modes or ())
        if enabled and candidate.source_type not in enabled:
            return "source_disabled"

    if context.privacy_state in _PRIVACY_CLOSED_STATES and (
        candidate.source_type in _PRIVACY_SENSITIVE_SOURCES
        or "screen" in candidate.risk_flags
        or "privacy" in candidate.risk_flags
    ):
        return "privacy_sensitive"

    if "duplicate" in candidate.risk_flags:
        return "duplicate"

    if context.activity_state in _BUSY_STATES and candidate.source_type not in (
        "topic_hook",
        "vision",
    ):
        return "activity_busy"

    return None


def score_candidate(
    context: ProactiveRecommendationContext,
    candidate: ProactiveCandidate,
) -> tuple[float, dict[str, float]]:
    source_weight = clamp_to_unit_interval(float(context.source_weights.get(candidate.source_type, 0.5)))
    freshness = clamp_to_unit_interval(candidate.freshness)
    context_match = _context_relevance_score(context, candidate)
    user_interest_match = _user_interest_score(candidate)
    novelty = _novelty_score(context, candidate)
    source_quality = clamp_to_unit_interval(candidate.quality)
    interaction_value = _interaction_value_score(candidate)
    interruption_cost = _interruption_cost_score(context, candidate)
    risk_penalty = _risk_penalty_score(context, candidate)
    source_type_adjustment = _source_type_score_delta(candidate)
    tuning_adjustment = _tuning_score_delta(context, candidate)
    diversity_penalty, diversity_stats = _diversity_penalty_score(context, candidate)

    base_score = (
        0.20 * source_weight
        + 0.15 * freshness
        + _CONTEXT_MATCH_WEIGHT * context_match
        + 0.15 * user_interest_match
        + 0.15 * novelty
        + 0.10 * source_quality
        + 0.05 * interaction_value
        - 0.25 * interruption_cost
        - 0.30 * risk_penalty
    )
    baseline_score = clamp_to_unit_interval(
        base_score + source_type_adjustment + tuning_adjustment - diversity_penalty
    )
    personalization_mode = _personalization_mode(context.personalization_mode)
    personalization_adjustment = 0.0
    if personalization_mode in {"shadow_compare", "active"}:
        personalization_adjustment = max(
            -PERSONALIZATION_MAX_ABS_DELTA,
            min(
                PERSONALIZATION_MAX_ABS_DELTA,
                coerce_float_or_default(
                    context.personalization_adjustments.get(candidate.source_type),
                    default=0.0,
                ),
            ),
        )
    personalized_score = clamp_to_unit_interval(
        baseline_score + personalization_adjustment
    )
    score = personalized_score if personalization_mode == "active" else baseline_score
    breakdown = {
        "source_weight": source_weight,
        "freshness": freshness,
        "context_match": context_match,
        "user_interest_match": user_interest_match,
        "novelty": novelty,
        "source_quality": source_quality,
        "interaction_value": interaction_value,
        "interruption_cost": interruption_cost,
        "risk_penalty": risk_penalty,
        "base_score": base_score,
        "source_type_adjustment": source_type_adjustment,
        "tuning_adjustment": tuning_adjustment,
        "diversity_penalty": diversity_penalty,
        "shadow_source_repeat_count": float(
            diversity_stats["shadow_source_repeat_count"]
        ),
        "shadow_source_streak": float(diversity_stats["shadow_source_streak"]),
        "candidate_repeat_count": float(diversity_stats["candidate_repeat_count"]),
        "score": score,
    }
    if personalization_mode != "off":
        breakdown.update(
            {
                "baseline_score": baseline_score,
                "personalization_adjustment": personalization_adjustment,
                "personalized_score": personalized_score,
            }
        )
    return score, breakdown


def _personalization_diagnostics(
    context: ProactiveRecommendationContext,
    ranked: Sequence[ProactiveCandidate],
    score_breakdown: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    mode = _personalization_mode(context.personalization_mode)
    if mode == "off" or not ranked:
        return {}

    baseline_ranked = sorted(
        ranked,
        key=lambda candidate: coerce_float_or_default(
            score_breakdown.get(candidate.id, {}).get("baseline_score"),
            default=candidate.score,
        ),
        reverse=True,
    )
    personalized_ranked = sorted(
        ranked,
        key=lambda candidate: coerce_float_or_default(
            score_breakdown.get(candidate.id, {}).get("personalized_score"),
            default=candidate.score,
        ),
        reverse=True,
    )
    baseline_top = baseline_ranked[0]
    personalized_top = personalized_ranked[0]
    baseline_positions = {
        candidate.id: index for index, candidate in enumerate(baseline_ranked, start=1)
    }
    personalized_positions = {
        candidate.id: index
        for index, candidate in enumerate(personalized_ranked, start=1)
    }
    rows = []
    for candidate in baseline_ranked:
        breakdown = score_breakdown.get(candidate.id, {})
        rows.append(
            {
                "id": candidate.id,
                "source_type": candidate.source_type,
                "baseline_rank": baseline_positions[candidate.id],
                "personalized_rank": personalized_positions[candidate.id],
                "baseline_score": round(
                    coerce_float_or_default(
                        breakdown.get("baseline_score"), default=candidate.score
                    ),
                    6,
                ),
                "delta": round(
                    coerce_float_or_default(
                        breakdown.get("personalization_adjustment"), default=0.0
                    ),
                    6,
                ),
                "personalized_score": round(
                    coerce_float_or_default(
                        breakdown.get("personalized_score"), default=candidate.score
                    ),
                    6,
                ),
            }
        )
    return {
        "mode": mode,
        "ranking_consumed": mode == "active",
        "baseline_selected_candidate_id": baseline_top.id,
        "baseline_selected_source_type": baseline_top.source_type,
        "personalized_selected_candidate_id": personalized_top.id,
        "personalized_selected_source_type": personalized_top.source_type,
        "top1_changed": baseline_top.id != personalized_top.id,
        "candidates": rows,
    }


def _personalization_mode(value: Any) -> str:
    mode = to_stripped_text(value)
    return mode if mode in {"off", "shadow_compare", "active"} else "off"


def _context_relevance_score(
    context: ProactiveRecommendationContext, candidate: ProactiveCandidate
) -> float:
    if context.activity_state == "restricted_screen_only":
        return 1.0 if candidate.source_type == "vision" else 0.2
    if context.activity_state in _RESTRICTED_STATES:
        if candidate.source_type in ("vision", "window", "topic_hook"):
            return 0.8
        return 0.35
    if context.activity_state == "stale_returning":
        return 0.9 if candidate.source_type in ("topic_hook", "personal") else 0.65
    return 0.7 if candidate.source_type != "mini_game" else 0.55


def _user_interest_score(candidate: ProactiveCandidate) -> float:
    if candidate.source_type == "topic_hook":
        return clamp_to_unit_interval(
            coerce_float_or_default(
                candidate.payload.get("relevance"), default=70.0
            )
            / 100.0
        )
    if candidate.source_type == "personal":
        return 0.75
    if candidate.source_type in ("music", "meme"):
        return 0.55
    return 0.5


def _novelty_score(
    context: ProactiveRecommendationContext, candidate: ProactiveCandidate
) -> float:
    recent = [str(item or "") for item in (context.recent_sources or ())]
    if candidate.source_type not in recent:
        return 1.0
    repeats = sum(1 for item in recent if item == candidate.source_type)
    return max(0.15, 1.0 - 0.35 * repeats)


def _interaction_value_score(candidate: ProactiveCandidate) -> float:
    if candidate.source_type == "topic_hook":
        return 0.85
    if candidate.source_type == "meme":
        return 0.7
    if candidate.source_type == "mini_game":
        return 0.65
    if candidate.source_type == "music":
        return 0.6
    if candidate.source_type in ("news", "video", "home"):
        return 0.55
    return 0.5


def _interruption_cost_score(
    context: ProactiveRecommendationContext, candidate: ProactiveCandidate
) -> float:
    if context.activity_state in _BUSY_STATES:
        return 0.9
    if context.activity_state in _RESTRICTED_STATES:
        return 0.65 if candidate.source_type not in ("vision", "topic_hook") else 0.25
    if context.activity_state == "stale_returning":
        return 0.15
    return 0.25 if candidate.source_type in ("meme", "mini_game") else 0.2


def _risk_penalty_score(
    context: ProactiveRecommendationContext, candidate: ProactiveCandidate
) -> float:
    penalty = 0.0
    flags = set(candidate.risk_flags)
    if "privacy" in flags:
        penalty += 0.8
    if "duplicate" in flags:
        penalty += 0.8
    if "screen" in flags:
        penalty += 0.35 if context.privacy_state in _PRIVACY_CLOSED_STATES else 0.1
    if "placeholder" in flags:
        penalty += 0.15
    if "topic_risk" in flags:
        penalty += 0.3
    return clamp_to_unit_interval(penalty)


def _source_type_score_delta(candidate: ProactiveCandidate) -> float:
    return float(_SOURCE_TYPE_SCORE_ADJUSTMENTS.get(candidate.source_type, 0.0))


def _tuning_score_delta(
    context: ProactiveRecommendationContext,
    candidate: ProactiveCandidate,
) -> float:
    try:
        value = float(context.source_type_adjustments.get(candidate.source_type, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(-0.15, min(0.15, value))


def _diversity_penalty_score(
    context: ProactiveRecommendationContext,
    candidate: ProactiveCandidate,
) -> tuple[float, dict[str, int]]:
    recent_sources = _clean_recent_items(context.recent_shadow_sources)[
        -_DIVERSITY_RECENT_SOURCE_LIMIT:
    ]
    recent_candidate_ids = _clean_recent_items(context.recent_candidate_ids)
    source_repeat_count = sum(
        1 for source in recent_sources if source == candidate.source_type
    )
    source_streak = 0
    for source in reversed(recent_sources):
        if source != candidate.source_type:
            break
        source_streak += 1
    candidate_repeat_count = sum(
        1 for item in recent_candidate_ids if item == candidate.id
    )
    source_repeat_penalty = min(
        _DIVERSITY_SOURCE_REPEAT_MAX,
        _DIVERSITY_SOURCE_REPEAT_STEP * source_repeat_count,
    )
    source_streak_penalty = min(
        _DIVERSITY_SOURCE_STREAK_MAX,
        _DIVERSITY_SOURCE_STREAK_STEP * source_streak,
    )
    candidate_repeat_penalty = (
        _DIVERSITY_CANDIDATE_REPEAT_PENALTY if candidate_repeat_count > 0 else 0.0
    )
    penalty = min(
        _DIVERSITY_PENALTY_MAX,
        source_repeat_penalty + source_streak_penalty + candidate_repeat_penalty,
    )
    return penalty, {
        "shadow_source_repeat_count": source_repeat_count,
        "shadow_source_streak": source_streak,
        "candidate_repeat_count": candidate_repeat_count,
    }


def _clean_recent_items(values: Sequence[Any]) -> list[str]:
    return [
        text
        for item in values or ()
        if (text := to_stripped_text(item))
    ]
