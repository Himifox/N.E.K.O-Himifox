"""Schema-v3 timing/fatigue audit helpers for Recommendation Testbench."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import math
import re
from typing import Any, Callable


TIMING_SCHEMA_VERSION = 3
TIMING_FIELDS = (
    "configured_interval_seconds",
    "elapsed_since_last_delivery_seconds",
    "recent_delivery_count_30m",
    "recent_delivery_count_2h",
    "consecutive_unanswered_deliveries",
)
TIMING_OPTIONAL_NUMBER_BOUNDS = {
    "configured_interval_seconds": (0.0, 86_400.0),
    "elapsed_since_last_delivery_seconds": (0.0, 31_536_000.0),
}
TIMING_COUNT_MAX = 1_000_000
_VERSION_RE = re.compile(r"(?:observation-)?v(?P<version>\d+)(?:\D|$)", re.IGNORECASE)


def observation_schema_generation(observation: Mapping[str, Any]) -> int | None:
    """Extract the observation generation without treating app version as schema."""
    text = str(observation.get("algorithm_version") or "").strip()
    matches = list(_VERSION_RE.finditer(text))
    if not matches:
        return None
    return int(matches[-1].group("version"))


def inspect_timing_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one observation without mutating or silently clamping it."""
    generation = observation_schema_generation(observation)
    decision_context = observation.get("decision_context")
    timing = (
        decision_context.get("timing")
        if isinstance(decision_context, Mapping)
        else None
    )
    errors: list[dict[str, str]] = []
    normalized: dict[str, int | float | None] | None = None

    if generation is not None and generation < TIMING_SCHEMA_VERSION:
        return {
            "schema_generation": generation,
            "status": "timing_unavailable_legacy",
            "has_timing_context": isinstance(timing, Mapping),
            "timing_eligible": False,
            "normalized_timing": None,
            "errors": [],
        }
    if generation is not None and generation > TIMING_SCHEMA_VERSION:
        return {
            "schema_generation": generation,
            "status": "timing_unsupported_future_schema",
            "has_timing_context": isinstance(timing, Mapping),
            "timing_eligible": False,
            "normalized_timing": None,
            "errors": [{
                "path": "algorithm_version",
                "code": "unsupported_future_observation_schema",
                "message": (
                    f"schema v{generation} is newer than supported timing "
                    f"schema v{TIMING_SCHEMA_VERSION}"
                ),
            }],
        }
    if generation is None:
        return {
            "schema_generation": None,
            "status": "timing_unknown_algorithm_version",
            "has_timing_context": isinstance(timing, Mapping),
            "timing_eligible": False,
            "normalized_timing": None,
            "errors": [{
                "path": "algorithm_version",
                "code": "unknown_observation_schema",
                "message": "cannot identify observation schema generation",
            }],
        }
    if not isinstance(decision_context, Mapping):
        errors.append({
            "path": "decision_context",
            "code": "missing_decision_context",
            "message": "schema v3 requires a decision_context object",
        })
    elif not isinstance(timing, Mapping):
        errors.append({
            "path": "decision_context.timing",
            "code": "missing_timing_context",
            "message": "schema v3 requires a timing object",
        })
    else:
        normalized = _validate_timing_mapping(timing, errors)

    return {
        "schema_generation": generation,
        "status": "timing_valid_v3" if not errors else "timing_invalid_v3",
        "has_timing_context": isinstance(timing, Mapping),
        "timing_eligible": not errors,
        "normalized_timing": normalized if not errors else None,
        "errors": errors,
    }


def _validate_timing_mapping(
    timing: Mapping[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, int | float | None]:
    normalized: dict[str, int | float | None] = {}
    unknown = sorted(set(timing) - set(TIMING_FIELDS))
    for key in unknown:
        errors.append({
            "path": f"decision_context.timing.{key}",
            "code": "unknown_timing_field",
            "message": "field is not part of observation timing schema v3",
        })
    for key in TIMING_FIELDS:
        if key not in timing:
            errors.append({
                "path": f"decision_context.timing.{key}",
                "code": "missing_timing_field",
                "message": "field is required by observation timing schema v3",
            })
            continue
        value = timing.get(key)
        if key in TIMING_OPTIONAL_NUMBER_BOUNDS:
            normalized[key] = _optional_bounded_number(
                value,
                key=key,
                bounds=TIMING_OPTIONAL_NUMBER_BOUNDS[key],
                errors=errors,
            )
        else:
            normalized[key] = _bounded_count(value, key=key, errors=errors)

    count_30m = normalized.get("recent_delivery_count_30m")
    count_2h = normalized.get("recent_delivery_count_2h")
    elapsed = normalized.get("elapsed_since_last_delivery_seconds")
    if (
        isinstance(count_30m, int)
        and isinstance(count_2h, int)
        and count_30m > count_2h
    ):
        errors.append({
            "path": "decision_context.timing.recent_delivery_count_30m",
            "code": "timing_count_window_inconsistent",
            "message": "30m delivery count cannot exceed 2h delivery count",
        })
    if (
        elapsed is None
        and (
            (isinstance(count_30m, int) and count_30m > 0)
            or (isinstance(count_2h, int) and count_2h > 0)
        )
    ):
        errors.append({
            "path": "decision_context.timing.elapsed_since_last_delivery_seconds",
            "code": "timing_elapsed_missing_with_history",
            "message": "elapsed cannot be null when recent delivery counts are non-zero",
        })
    if isinstance(elapsed, (int, float)) and elapsed <= 1_800:
        if count_30m == 0 or count_2h == 0:
            errors.append({
                "path": "decision_context.timing",
                "code": "timing_recent_delivery_not_counted",
                "message": "a delivery within 30m must appear in both delivery counts",
            })
    return normalized


def _optional_bounded_number(
    value: Any,
    *,
    key: str,
    bounds: tuple[float, float],
    errors: list[dict[str, str]],
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append({
            "path": f"decision_context.timing.{key}",
            "code": "timing_number_type_invalid",
            "message": "must be a finite number or null",
        })
        return None
    number = float(value)
    if not math.isfinite(number) or not bounds[0] <= number <= bounds[1]:
        errors.append({
            "path": f"decision_context.timing.{key}",
            "code": "timing_number_out_of_bounds",
            "message": f"must be within {bounds[0]}-{bounds[1]} or null",
        })
        return None
    return round(number, 3)


def _bounded_count(
    value: Any,
    *,
    key: str,
    errors: list[dict[str, str]],
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append({
            "path": f"decision_context.timing.{key}",
            "code": "timing_count_type_invalid",
            "message": "must be an integer",
        })
        return 0
    if not 0 <= value <= TIMING_COUNT_MAX:
        errors.append({
            "path": f"decision_context.timing.{key}",
            "code": "timing_count_out_of_bounds",
            "message": f"must be within 0-{TIMING_COUNT_MAX}",
        })
        return 0
    return value


def sanitized_timing_decision_context(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return strict normalized timing data, or an empty mapping if ineligible."""
    inspected = inspect_timing_observation(observation)
    timing = inspected.get("normalized_timing")
    return {"timing": timing} if inspected["timing_eligible"] and timing else {}


def prepare_observation_for_timing_import(
    observation: Mapping[str, Any],
    production_sanitizer: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Apply production sanitization while preserving only valid v3 timing."""
    inspected = inspect_timing_observation(observation)
    generation = inspected["schema_generation"]
    if (
        generation is not None
        and generation >= TIMING_SCHEMA_VERSION
        and not inspected["timing_eligible"]
    ):
        return {
            "accepted": False,
            "observation": None,
            "reason": "invalid_timing_context",
            "errors": inspected["errors"],
        }
    safe = production_sanitizer(observation)
    decision_context = sanitized_timing_decision_context(observation)
    if decision_context:
        # Idempotent once the production v3 sanitizer is present, and a safe
        # compatibility bridge while Testbench is still based on production v2.
        safe["decision_context"] = decision_context
    return {
        "accepted": True,
        "observation": safe,
        "reason": None,
        "errors": [],
    }


def audit_timing_dataset(dataset: Mapping[str, Any]) -> dict[str, Any]:
    observations = list(dataset.get("observations") or [])
    status_counts: Counter[str] = Counter()
    schema_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    field_presence: Counter[str] = Counter()
    invalid_rows: list[dict[str, Any]] = []
    eligible_rows: list[tuple[Mapping[str, Any], dict[str, Any]]] = []

    for index, observation in enumerate(observations):
        inspected = inspect_timing_observation(observation)
        generation = inspected["schema_generation"]
        schema_counts[f"v{generation}" if generation is not None else "unknown"] += 1
        status_counts[inspected["status"]] += 1
        timing = (
            (observation.get("decision_context") or {}).get("timing")
            if isinstance(observation.get("decision_context"), Mapping)
            else None
        )
        if isinstance(timing, Mapping):
            for key in TIMING_FIELDS:
                if key in timing:
                    field_presence[key] += 1
        if inspected["timing_eligible"]:
            eligible_rows.append((observation, inspected))
        if inspected["errors"]:
            for error in inspected["errors"]:
                issue_counts[error["code"]] += 1
            invalid_rows.append({
                "index": index,
                "turn_id": str(observation.get("turn_id") or ""),
                "algorithm_version": str(
                    observation.get("algorithm_version") or ""
                ),
                "status": inspected["status"],
                "errors": inspected["errors"],
            })

    buckets = _timing_buckets(eligible_rows)
    valid_count = len(eligible_rows)
    v3_count = schema_counts[f"v{TIMING_SCHEMA_VERSION}"]
    return {
        "observation_count": len(observations),
        "schema_distribution": dict(sorted(schema_counts.items())),
        "status_distribution": dict(sorted(status_counts.items())),
        "v3_observation_count": v3_count,
        "timing_context_present_count": sum(
            1
            for row in observations
            if isinstance(row.get("decision_context"), Mapping)
            and isinstance(row["decision_context"].get("timing"), Mapping)
        ),
        "timing_valid_count": valid_count,
        "timing_invalid_count": status_counts["timing_invalid_v3"],
        "timing_unavailable_legacy_count": status_counts[
            "timing_unavailable_legacy"
        ],
        "timing_unknown_version_count": status_counts[
            "timing_unknown_algorithm_version"
        ],
        "timing_unsupported_future_count": status_counts[
            "timing_unsupported_future_schema"
        ],
        "timing_coverage_rate": (
            round(valid_count / v3_count, 4) if v3_count else 0.0
        ),
        "whole_dataset_timing_coverage_rate": (
            round(valid_count / len(observations), 4) if observations else 0.0
        ),
        "field_presence": {
            key: field_presence[key]
            for key in TIMING_FIELDS
        },
        "issue_distribution": dict(sorted(issue_counts.items())),
        "invalid_rows": invalid_rows,
        "bucket_distribution": buckets,
    }


def timing_analysis_readiness(
    dataset: Mapping[str, Any],
    *,
    min_observations: int = 100,
    min_joined_feedback: int = 30,
) -> dict[str, Any]:
    audit = audit_timing_dataset(dataset)
    observations = list(dataset.get("observations") or [])
    feedback = list(dataset.get("feedback") or [])
    valid_turns = {
        str(row.get("turn_id"))
        for row in observations
        if row.get("turn_id")
    }
    feedback_turns = {
        str(row.get("turn_id"))
        for row in feedback
        if row.get("turn_id")
    }
    joined = len(valid_turns & feedback_turns)
    sources = {
        str(row.get("shadow_selected_source_type") or "none")
        for row in observations
    }
    activities = {
        str(row.get("activity_state") or "unknown")
        for row in observations
    }
    blockers: list[str] = []
    if audit["timing_valid_count"] < min_observations:
        blockers.append("timing_observation_count_below_threshold")
    if joined < min_joined_feedback:
        blockers.append("feedback_joined_count_below_threshold")
    if audit["timing_invalid_count"]:
        blockers.append("invalid_v3_timing_records")
    if audit["timing_unavailable_legacy_count"]:
        blockers.append("legacy_observations_not_timing_eligible")
    if audit["timing_unknown_version_count"]:
        blockers.append("unknown_observation_schema")
    if audit["timing_unsupported_future_count"]:
        blockers.append("unsupported_future_observation_schema")
    if len(sources) < 3:
        blockers.append("source_coverage_below_3")
    if len(activities) < 3:
        blockers.append("activity_coverage_below_3")
    elapsed_buckets = audit["bucket_distribution"]["elapsed_since_last_delivery"]
    if sum(count > 0 for count in elapsed_buckets.values()) < 3:
        blockers.append("elapsed_bucket_diversity_below_3")
    unanswered_buckets = audit["bucket_distribution"]["consecutive_unanswered"]
    if sum(count > 0 for count in unanswered_buckets.values()) < 3:
        blockers.append("unanswered_bucket_diversity_below_3")
    return {
        "ready_for_timing_strategy_scan": not blockers,
        "pilot_contract_ready": (
            audit["timing_valid_count"] >= 10
            and audit["timing_valid_count"] == len(observations)
        ),
        "blockers": blockers,
        "requirements": {
            "min_timing_observations": min_observations,
            "min_joined_feedback": min_joined_feedback,
            "min_sources": 3,
            "min_activities": 3,
            "min_elapsed_buckets": 3,
            "min_unanswered_buckets": 3,
        },
        "feedback_joined_count": joined,
        "source_count": len(sources),
        "activity_count": len(activities),
        "audit": audit,
        "production_config_modified": False,
        "tuning_modified": False,
    }


def _timing_buckets(
    rows: list[tuple[Mapping[str, Any], dict[str, Any]]],
) -> dict[str, dict[str, int]]:
    elapsed = Counter({"first_or_no_history": 0, "lt_5m": 0, "5_to_10m": 0,
                       "10_to_30m": 0, "gte_30m": 0})
    count_30m = Counter({"0": 0, "1": 0, "2": 0, "3_plus": 0})
    count_2h = Counter({"0_to_1": 0, "2_to_4": 0, "5_plus": 0})
    unanswered = Counter({"0": 0, "1": 0, "2": 0, "3_plus": 0})
    for _observation, inspected in rows:
        timing = inspected["normalized_timing"]
        elapsed_value = timing["elapsed_since_last_delivery_seconds"]
        if elapsed_value is None:
            elapsed["first_or_no_history"] += 1
        elif elapsed_value < 300:
            elapsed["lt_5m"] += 1
        elif elapsed_value < 600:
            elapsed["5_to_10m"] += 1
        elif elapsed_value < 1_800:
            elapsed["10_to_30m"] += 1
        else:
            elapsed["gte_30m"] += 1
        _increment_count_bucket(
            count_30m,
            timing["recent_delivery_count_30m"],
            [(0, "0"), (1, "1"), (2, "2")],
            "3_plus",
        )
        value_2h = timing["recent_delivery_count_2h"]
        count_2h["0_to_1" if value_2h <= 1 else "2_to_4" if value_2h <= 4
                 else "5_plus"] += 1
        _increment_count_bucket(
            unanswered,
            timing["consecutive_unanswered_deliveries"],
            [(0, "0"), (1, "1"), (2, "2")],
            "3_plus",
        )
    return {
        "elapsed_since_last_delivery": dict(elapsed),
        "recent_delivery_count_30m": dict(count_30m),
        "recent_delivery_count_2h": dict(count_2h),
        "consecutive_unanswered": dict(unanswered),
    }


def _increment_count_bucket(
    counter: Counter[str],
    value: int,
    exact: list[tuple[int, str]],
    fallback: str,
) -> None:
    for expected, label in exact:
        if value == expected:
            counter[label] += 1
            return
    counter[fallback] += 1


__all__ = [
    "TIMING_FIELDS",
    "TIMING_SCHEMA_VERSION",
    "audit_timing_dataset",
    "inspect_timing_observation",
    "observation_schema_generation",
    "prepare_observation_for_timing_import",
    "sanitized_timing_decision_context",
    "timing_analysis_readiness",
]
