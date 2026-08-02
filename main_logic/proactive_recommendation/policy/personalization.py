"""Bounded source-affinity mapping for proactive recommendation ranking."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from main_logic.proactive_recommendation.state.feedback import (
    PERSISTENT_AFFINITY_MAX,
    PERSISTENT_INTEREST_MIN_EVIDENCE,
)


PERSONALIZATION_MAX_ABS_DELTA = 0.03
PERSONALIZATION_SATURATION_EVIDENCE = 12
VALID_PERSONALIZATION_MODES = frozenset({"off", "shadow_compare", "active"})


def build_personalization_plan(
    feedback_state: Mapping[str, Any] | None,
    *,
    mode: Any,
) -> dict[str, Any]:
    """Map point-in-time persistent affinity into registered gradual_12 deltas."""

    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in VALID_PERSONALIZATION_MODES:
        normalized_mode = "off"
    plan: dict[str, Any] = {
        "mode": normalized_mode,
        "ranking_consumed": normalized_mode == "active",
        "max_abs_delta": PERSONALIZATION_MAX_ABS_DELTA,
        "saturation_evidence": PERSONALIZATION_SATURATION_EVIDENCE,
        "sources": {},
    }
    if normalized_mode == "off" or not isinstance(feedback_state, Mapping):
        return plan
    if feedback_state.get("version") != "feedback_state_preview_v2":
        return plan
    affinity = feedback_state.get("source_affinity")
    persistent = affinity.get("persistent") if isinstance(affinity, Mapping) else None
    sources = persistent.get("sources") if isinstance(persistent, Mapping) else None
    if not isinstance(sources, Mapping):
        return plan

    for raw_source, raw_bucket in sorted(sources.items()):
        source = _source_name(raw_source)
        if not source or not isinstance(raw_bucket, Mapping):
            continue
        positive = _count(raw_bucket.get("positive_evidence_count"))
        negative = _count(raw_bucket.get("negative_evidence_count"))
        evidence = positive + negative
        affinity_preview = _finite_number(raw_bucket.get("affinity_preview"))
        if evidence < PERSISTENT_INTEREST_MIN_EVIDENCE:
            confidence = 0.0
            delta = 0.0
        else:
            direction = _clamp(
                affinity_preview / PERSISTENT_AFFINITY_MAX,
                -1.0,
                1.0,
            )
            confidence = min(
                1.0,
                evidence / PERSONALIZATION_SATURATION_EVIDENCE,
            )
            delta = _clamp(
                direction * confidence * PERSONALIZATION_MAX_ABS_DELTA,
                -PERSONALIZATION_MAX_ABS_DELTA,
                PERSONALIZATION_MAX_ABS_DELTA,
            )
        plan["sources"][source] = {
            "positive_evidence_count": positive,
            "negative_evidence_count": negative,
            "total_evidence_count": evidence,
            "affinity_preview": round(affinity_preview, 6),
            "confidence": round(confidence, 6),
            "delta": round(delta, 6),
        }
    return plan


def personalization_adjustments(plan: Mapping[str, Any] | None) -> dict[str, float]:
    """Return only bounded source deltas from a validated plan."""

    if not isinstance(plan, Mapping):
        return {}
    sources = plan.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    result: dict[str, float] = {}
    for raw_source, raw_details in sources.items():
        source = _source_name(raw_source)
        if not source or not isinstance(raw_details, Mapping):
            continue
        result[source] = round(
            _clamp(
                _finite_number(raw_details.get("delta")),
                -PERSONALIZATION_MAX_ABS_DELTA,
                PERSONALIZATION_MAX_ABS_DELTA,
            ),
            6,
        )
    return result


def _source_name(value: Any) -> str:
    source = str(value or "").strip().lower()
    return source if source.replace("_", "").isalnum() else ""


def _count(value: Any) -> int:
    try:
        return max(0, min(1_000_000, int(value)))
    except (TypeError, ValueError):
        return 0


def _finite_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


__all__ = [
    "PERSONALIZATION_MAX_ABS_DELTA",
    "PERSONALIZATION_SATURATION_EVIDENCE",
    "VALID_PERSONALIZATION_MODES",
    "build_personalization_plan",
    "personalization_adjustments",
]
