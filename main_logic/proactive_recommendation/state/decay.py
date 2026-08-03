"""Shared time-decay mechanics for recommendation evidence state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..normalization import (
    coerce_bounded_evidence_weight,
    coerce_finite_float,
)


def build_empty_evidence_bucket(now: float) -> dict[str, float]:
    return {
        "effective_success": 0.0,
        "effective_failure": 0.0,
        "updated_at": now,
    }


def calculate_half_life_decay_factor(
    recorded_at: float,
    now: float,
    *,
    half_life_seconds: float,
) -> float:
    elapsed_seconds = max(0.0, now - max(0.0, recorded_at))
    return 0.5 ** (elapsed_seconds / half_life_seconds)


def apply_decay_to_evidence_bucket(
    bucket: dict[str, Any],
    now: float,
    *,
    half_life_seconds: float,
) -> None:
    updated_at = max(0.0, coerce_finite_float(bucket.get("updated_at")))
    decay_factor = (
        calculate_half_life_decay_factor(
            updated_at,
            now,
            half_life_seconds=half_life_seconds,
        )
        if updated_at
        else 1.0
    )
    bucket["effective_success"] = (
        coerce_bounded_evidence_weight(bucket.get("effective_success"))
        * decay_factor
    )
    bucket["effective_failure"] = (
        coerce_bounded_evidence_weight(bucket.get("effective_failure"))
        * decay_factor
    )
    bucket["updated_at"] = now


def trim_oldest_outcomes(
    outcomes: dict[str, dict[str, Any]],
    *,
    maximum_count: int,
    timestamp_field: str,
) -> None:
    overflow_count = len(outcomes) - maximum_count
    if overflow_count <= 0:
        return
    oldest_keys = sorted(
        outcomes,
        key=lambda outcome_key: coerce_finite_float(
            outcomes[outcome_key].get(timestamp_field)
        ),
    )[:overflow_count]
    for outcome_key in oldest_keys:
        outcomes.pop(outcome_key, None)


def sanitized_evidence_bucket(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "effective_success": coerce_bounded_evidence_weight(
            value.get("effective_success")
        ),
        "effective_failure": coerce_bounded_evidence_weight(
            value.get("effective_failure")
        ),
        "updated_at": max(0.0, coerce_finite_float(value.get("updated_at"))),
    }


__all__ = [
    "apply_decay_to_evidence_bucket",
    "build_empty_evidence_bucket",
    "calculate_half_life_decay_factor",
    "sanitized_evidence_bucket",
    "trim_oldest_outcomes",
]
