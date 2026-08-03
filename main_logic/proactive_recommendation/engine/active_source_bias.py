"""Active-source bias over already generated recommendation materials."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from main_logic.proactive_recommendation.domain_models import (
    ProactiveActiveBias,
    ProactiveCandidate,
    ProactiveRecommendationDecision,
)
from main_logic.proactive_recommendation.normalization import (
    coerce_float_or_default,
    to_stripped_text,
)

_ACTIVE_DIVERSITY_OVERUSE_THRESHOLD = 0.12


def source_type_to_phase2_tag(source_type: Any) -> str | None:
    """Map active-safe material source types to existing Phase-2 tags."""
    normalized = to_stripped_text(source_type)
    if normalized in {"web", "news", "video", "home"}:
        return "WEB"
    if normalized == "music":
        return "MUSIC"
    if normalized == "meme":
        return "MEME"
    return None


def build_active_source_bias(
    decision: ProactiveRecommendationDecision | None,
    *,
    min_score_gap: float = 0.05,
) -> ProactiveActiveBias:
    """Return the minimal active-source pilot bias for a material decision."""
    if decision is None:
        return _active_bias_fallback("no_decision")
    if decision.decision_stage != "phase1_material":
        return _active_bias_fallback("not_phase1_material")
    ranked = list(decision.ranked_candidates or ())
    if not ranked:
        return _active_bias_fallback("no_candidate")

    top = ranked[0]
    preferred_tag = source_type_to_phase2_tag(top.source_type)
    if preferred_tag is None:
        return _active_bias_fallback(
            "unsupported_source",
            candidate=top,
            score_gap=_score_gap(ranked),
        )
    if not _candidate_has_material_link(top):
        return _active_bias_fallback(
            "missing_material_link",
            candidate=top,
            preferred_tag=preferred_tag,
            score_gap=_score_gap(ranked),
        )
    top_breakdown = decision.score_breakdown.get(top.id, {})
    diversity_penalty = coerce_float_or_default(
        top_breakdown.get("diversity_penalty"), default=0.0
    )
    if diversity_penalty >= _ACTIVE_DIVERSITY_OVERUSE_THRESHOLD:
        return _active_bias_fallback(
            "diversity_overuse",
            candidate=top,
            preferred_tag=preferred_tag,
            score_gap=_score_gap(ranked),
        )

    effective_min_score_gap = max(0.0, float(min_score_gap))
    if (
        decision.personalization.get("ranking_consumed") is True
        and decision.personalization.get("top1_changed") is True
    ):
        effective_min_score_gap = 0.0
    gap = _score_gap(ranked)
    if gap is not None and gap < effective_min_score_gap:
        return _active_bias_fallback(
            "score_gap_too_small",
            candidate=top,
            preferred_tag=preferred_tag,
            score_gap=gap,
        )

    return ProactiveActiveBias(
        applied=True,
        preferred_source_type=top.source_type,
        preferred_source_tag=preferred_tag,
        preferred_candidate_id=top.id,
        score_gap=gap,
    )


def reorder_phase1_topics_for_bias(
    phase1_topics: Sequence[Any],
    bias: ProactiveActiveBias | Mapping[str, Any] | None,
) -> list[Any]:
    """Move the preferred material channel first without dropping candidates."""
    info = serialize_active_source_bias(bias)
    if info.get("applied") is not True:
        return list(phase1_topics or ())
    target = {
        "WEB": "web",
        "MUSIC": "music",
        "MEME": "meme",
    }.get(to_stripped_text(info.get("preferred_source_tag")).upper())
    if not target:
        return list(phase1_topics or ())

    preferred: list[Any] = []
    rest: list[Any] = []
    for item in phase1_topics or ():
        channel = ""
        if (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes))
            and len(item) >= 1
        ):
            channel = to_stripped_text(item[0])
        if channel == target:
            preferred.append(item)
        else:
            rest.append(item)
    return preferred + rest


def candidate_material_url(candidate: ProactiveCandidate) -> str:
    link = candidate.payload.get("link")
    if isinstance(link, Mapping):
        return to_stripped_text(link.get("url"))
    return ""


def _candidate_has_material_link(candidate: ProactiveCandidate) -> bool:
    return bool(candidate_material_url(candidate))


def _score_gap(candidates: Sequence[ProactiveCandidate]) -> float | None:
    if len(candidates) < 2:
        return None
    return float(candidates[0].score) - float(candidates[1].score)


def _active_bias_fallback(
    reason: str,
    *,
    candidate: ProactiveCandidate | None = None,
    preferred_tag: str | None = None,
    score_gap: float | None = None,
) -> ProactiveActiveBias:
    return ProactiveActiveBias(
        applied=False,
        preferred_source_type=candidate.source_type if candidate else None,
        preferred_source_tag=preferred_tag,
        preferred_candidate_id=candidate.id if candidate else None,
        score_gap=score_gap,
        fallback_reason=reason,
    )


def serialize_active_source_bias(
    active_bias: ProactiveActiveBias | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(active_bias, ProactiveActiveBias):
        return active_bias.to_log_dict()
    if isinstance(active_bias, Mapping):
        return {
            "applied": active_bias.get("applied") is True,
            "preferred_source_type": to_stripped_text(active_bias.get("preferred_source_type"))
            or None,
            "preferred_source_tag": to_stripped_text(
                active_bias.get("preferred_source_tag")
            ).upper()
            or None,
            "preferred_candidate_id": to_stripped_text(active_bias.get("preferred_candidate_id"))
            or None,
            "score_gap": (
                coerce_float_or_default(
                    active_bias.get("score_gap"), default=0.0
                )
                if active_bias.get("score_gap") is not None
                else None
            ),
            "fallback_reason": to_stripped_text(active_bias.get("fallback_reason")) or None,
        }
    return {
        "applied": False,
        "preferred_source_type": None,
        "preferred_source_tag": None,
        "preferred_candidate_id": None,
        "score_gap": None,
        "fallback_reason": None,
    }


