"""Pure P44-F2 timing/fatigue association analysis over a frozen dataset."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import random
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping

from tests.testbench.pipeline.recommendation_timing_audit import (
    TIMING_FIELDS,
    audit_timing_dataset,
)


TIMING_ANALYSIS_VERSION = 1
MIN_ASSOCIATION_SAMPLE_COUNT = 20
MIN_BINARY_CLASS_COUNT = 8
DEFAULT_BOOTSTRAP_REPETITIONS = 1_000
DEFAULT_BOOTSTRAP_SEED = 44_020


def analyze_timing_fatigue_baseline(
    frozen_dataset: Mapping[str, Any],
    *,
    bootstrap_repetitions: int = DEFAULT_BOOTSTRAP_REPETITIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Describe timing associations without proposing a production formula.

    A candidate is intentionally eligible only when human decision labels make
    false-interruption and missed-opportunity outcomes observable. Explicit
    feedback is treated as supporting evidence, never as a replacement label.
    """
    if bootstrap_repetitions < 100:
        raise ValueError("bootstrap_repetitions must be at least 100")
    audit = audit_timing_dataset(frozen_dataset)
    rows = _analysis_rows(frozen_dataset)
    if not rows:
        raise ValueError("no timing-valid observations in frozen dataset")

    outcomes = _outcome_availability(rows, frozen_dataset)
    associations = {
        "production_delivery": _analyze_outcome(
            rows,
            outcome_name="production_delivery",
            outcome_getter=lambda row: row["delivered"],
            binary=True,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        "explicit_feedback_join": _analyze_outcome(
            [row for row in rows if row["delivered"]],
            outcome_name="explicit_feedback_join",
            outcome_getter=lambda row: row["feedback_joined"],
            binary=True,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        "explicit_feedback_score": _analyze_outcome(
            [
                row for row in rows
                if row["delivered"] and row["feedback_score"] is not None
            ],
            outcome_name="explicit_feedback_score",
            outcome_getter=lambda row: row["feedback_score"],
            binary=False,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
    }
    if outcomes["false_interruption"]["available"]:
        associations["false_interruption"] = _analyze_outcome(
            [row for row in rows if row["delivered"] and row["human_should"] is not None],
            outcome_name="false_interruption",
            outcome_getter=lambda row: not row["human_should"],
            binary=True,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        )
    else:
        associations["false_interruption"] = _unavailable_outcome(
            outcomes["false_interruption"]["reason"]
        )
    if outcomes["missed_opportunity"]["available"]:
        associations["missed_opportunity"] = _analyze_outcome(
            [row for row in rows if not row["delivered"] and row["human_should"] is not None],
            outcome_name="missed_opportunity",
            outcome_getter=lambda row: row["human_should"],
            binary=True,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        )
    else:
        associations["missed_opportunity"] = _unavailable_outcome(
            outcomes["missed_opportunity"]["reason"]
        )

    conclusion = _conclusion(outcomes, associations)
    material = {
        "schema_version": 1,
        "analysis_version": TIMING_ANALYSIS_VERSION,
        "kind": "recommendation_p44f2_timing_fatigue_baseline",
        "input": {
            "observation_count": len(rows),
            "feedback_event_count": len(frozen_dataset.get("feedback") or []),
            "input_sha256": _sha256(frozen_dataset),
            "timing_field_names": list(TIMING_FIELDS),
        },
        "method": {
            "elapsed_time": "continuous seconds; no absolute elapsed-time bucket gate",
            "association": "Spearman rank correlation with deterministic bootstrap",
            "bootstrap_repetitions": bootstrap_repetitions,
            "bootstrap_seed": bootstrap_seed,
            "candidate_scope": "analysis only; no fatigue formula or simulation is run",
        },
        "timing_quality": audit,
        "outcomes": outcomes,
        "associations": associations,
        "conclusion": conclusion,
        "limitations": [
            "observational cohort; associations are not causal attribution",
            "explicit feedback absence is not interpreted as negative feedback",
            "explicit feedback is not a substitute for human should_recommend labels",
            "no scheduler mode, backoff tier, or scheduled delay is recorded in v3",
            "no production configuration, weight, interval, or tuning was modified",
        ],
        "production_config_modified": False,
        "tuning_modified": False,
    }
    return material


def _analysis_rows(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    feedback_by_turn: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in dataset.get("feedback") or []:
        turn_id = str(event.get("turn_id") or "")
        if turn_id:
            feedback_by_turn[turn_id].append(event)
    labels = _human_labels(dataset)
    rows: list[dict[str, Any]] = []
    for observation in dataset.get("observations") or []:
        timing = ((observation.get("decision_context") or {}).get("timing") or {})
        if not all(field in timing for field in TIMING_FIELDS):
            continue
        turn_id = str(observation.get("turn_id") or "")
        events = feedback_by_turn.get(turn_id, [])
        scores = [
            float(event["report_score_v1"])
            for event in events
            if isinstance(event.get("report_score_v1"), (int, float))
            and not isinstance(event.get("report_score_v1"), bool)
            and math.isfinite(float(event["report_score_v1"]))
        ]
        rows.append({
            "turn_id": turn_id,
            "ts": _finite_number(observation.get("ts")),
            "source": str(observation.get("shadow_selected_source_type") or "unknown"),
            "activity": str(observation.get("activity_state") or "unknown"),
            "delivered": observation.get("delivered") is True,
            "feedback_joined": bool(events),
            "feedback_score": round(fmean(scores), 6) if scores else None,
            "human_should": labels.get(turn_id),
            "timing": {field: _finite_number(timing.get(field)) for field in TIMING_FIELDS},
        })
    return rows


def _human_labels(dataset: Mapping[str, Any]) -> dict[str, bool]:
    labels: dict[str, bool] = {}
    for annotation in dataset.get("annotations") or []:
        turn_id = str(annotation.get("turn_id") or "")
        value = annotation.get("adjudicated_should_recommend")
        if not isinstance(value, bool):
            value = annotation.get("should_recommend")
        if turn_id and isinstance(value, bool):
            labels[turn_id] = value
    return labels


def _outcome_availability(
    rows: list[dict[str, Any]],
    dataset: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    labeled = [row for row in rows if row["human_should"] is not None]
    delivered_labeled = [row for row in labeled if row["delivered"]]
    passed_labeled = [row for row in labeled if not row["delivered"]]
    delivered = [row for row in rows if row["delivered"]]
    feedback_rows = [row for row in delivered if row["feedback_joined"]]
    return {
        "human_should_recommend": {
            "available": bool(labeled),
            "labeled_count": len(labeled),
            "unlabeled_count": len(rows) - len(labeled),
            "coverage_rate": _rate(len(labeled), len(rows)),
        },
        "false_interruption": {
            "available": bool(delivered_labeled),
            "labeled_delivery_count": len(delivered_labeled),
            "event_count": sum(not row["human_should"] for row in delivered_labeled),
            "reason": None if delivered_labeled else "human_should_recommend_labels_unavailable",
        },
        "missed_opportunity": {
            "available": bool(passed_labeled),
            "labeled_pass_count": len(passed_labeled),
            "event_count": sum(row["human_should"] for row in passed_labeled),
            "reason": None if passed_labeled else "human_should_recommend_labels_unavailable",
        },
        "explicit_feedback": {
            "available": bool(feedback_rows),
            "delivered_count": len(delivered),
            "joined_turn_count": len(feedback_rows),
            "joined_rate": _rate(len(feedback_rows), len(delivered)),
            "score_available_turn_count": sum(
                row["feedback_score"] is not None for row in feedback_rows
            ),
            "event_count": len(dataset.get("feedback") or []),
            "reason": None if feedback_rows else "no_explicit_feedback_joined",
        },
    }


def _analyze_outcome(
    rows: list[dict[str, Any]],
    *,
    outcome_name: str,
    outcome_getter: Callable[[dict[str, Any]], bool | float | None],
    binary: bool,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    outcomes = [outcome_getter(row) for row in rows]
    available = [value for value in outcomes if value is not None]
    if not available:
        return _unavailable_outcome("outcome_values_unavailable")
    result = {
        "available": True,
        "outcome_name": outcome_name,
        "sample_count": len(rows),
        "binary": binary,
        "outcome_summary": _outcome_summary(available, binary),
        "features": {},
    }
    for feature in TIMING_FIELDS:
        pairs = [
            (row["timing"].get(feature), outcome_getter(row), row)
            for row in rows
            if row["timing"].get(feature) is not None and outcome_getter(row) is not None
        ]
        result["features"][feature] = _association(
            pairs,
            feature=feature,
            outcome_name=outcome_name,
            binary=binary,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        )
    return result


def _association(
    pairs: list[tuple[float, bool | float, dict[str, Any]]],
    *,
    feature: str,
    outcome_name: str,
    binary: bool,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    x = [float(pair[0]) for pair in pairs]
    y = [float(pair[1]) for pair in pairs]
    rho = _spearman(x, y)
    support = _support(y, binary)
    if rho is None:
        return {
            "sample_count": len(pairs),
            "spearman_rho": None,
            "stable": False,
            "reason": "insufficient_feature_or_outcome_variation",
            "support": support,
        }
    ci = _bootstrap_ci(
        x,
        y,
        repetitions=bootstrap_repetitions,
        seed=_seed(bootstrap_seed, feature, outcome_name),
    )
    temporal = _temporal_stability(pairs)
    source = _source_stability(pairs)
    stable = (
        len(pairs) >= MIN_ASSOCIATION_SAMPLE_COUNT
        and support["sufficient"]
        and abs(rho) >= 0.25
        and ci["excludes_zero"]
        and temporal["stable"]
        and source["stable"]
    )
    return {
        "sample_count": len(pairs),
        "spearman_rho": round(rho, 4),
        "bootstrap_95ci": ci,
        "support": support,
        "temporal_stability": temporal,
        "source_stability": source,
        "stable": stable,
        "reason": None if stable else "stability_requirements_not_met",
    }


def _outcome_summary(values: list[bool | float], binary: bool) -> dict[str, Any]:
    numeric = [float(value) for value in values]
    if binary:
        positive = sum(value > 0 for value in numeric)
        return {
            "positive_count": positive,
            "negative_count": len(numeric) - positive,
            "positive_rate": _rate(positive, len(numeric)),
        }
    return {
        "mean": round(fmean(numeric), 4),
        "minimum": round(min(numeric), 4),
        "maximum": round(max(numeric), 4),
        "distinct_value_count": len(set(numeric)),
    }


def _support(values: list[float], binary: bool) -> dict[str, Any]:
    if binary:
        positive = sum(value > 0 for value in values)
        negative = len(values) - positive
        return {
            "positive_count": positive,
            "negative_count": negative,
            "minimum_class_count": min(positive, negative),
            "sufficient": min(positive, negative) >= MIN_BINARY_CLASS_COUNT,
        }
    distinct = len(set(values))
    return {
        "distinct_value_count": distinct,
        "sufficient": distinct >= 3,
    }


def _temporal_stability(
    pairs: list[tuple[float, bool | float, dict[str, Any]]],
) -> dict[str, Any]:
    ordered = sorted(pairs, key=lambda pair: pair[2]["ts"] if pair[2]["ts"] is not None else -1.0)
    midpoint = len(ordered) // 2
    early, late = ordered[:midpoint], ordered[midpoint:]
    early_rho = _spearman([float(x) for x, _y, _row in early], [float(y) for _x, y, _row in early])
    late_rho = _spearman([float(x) for x, _y, _row in late], [float(y) for _x, y, _row in late])
    stable = (
        early_rho is not None
        and late_rho is not None
        and early_rho * late_rho > 0
        and abs(early_rho) >= 0.15
        and abs(late_rho) >= 0.15
    )
    return {
        "early_rho": round(early_rho, 4) if early_rho is not None else None,
        "late_rho": round(late_rho, 4) if late_rho is not None else None,
        "stable": stable,
    }


def _source_stability(
    pairs: list[tuple[float, bool | float, dict[str, Any]]],
) -> dict[str, Any]:
    counts = Counter(str(row["source"] or "unknown") for _x, _y, row in pairs)
    eligible_sources = sorted(source for source, count in counts.items() if count >= 5)
    overall = _spearman([float(x) for x, _y, _row in pairs], [float(y) for _x, y, _row in pairs])
    leave_one_out: dict[str, float | None] = {}
    for source in eligible_sources:
        retained = [pair for pair in pairs if pair[2]["source"] != source]
        rho = _spearman(
            [float(x) for x, _y, _row in retained],
            [float(y) for _x, y, _row in retained],
        )
        leave_one_out[source] = round(rho, 4) if rho is not None else None
    stable = bool(eligible_sources) and overall is not None and all(
        rho is not None and rho * overall > 0 for rho in leave_one_out.values()
    )
    return {
        "eligible_source_count": len(eligible_sources),
        "leave_one_source_out_rho": leave_one_out,
        "stable": stable,
    }


def _bootstrap_ci(
    x: list[float],
    y: list[float],
    *,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    randomizer = random.Random(seed)
    values = []
    for _ in range(repetitions):
        indexes = [randomizer.randrange(len(x)) for _ in x]
        rho = _spearman([x[index] for index in indexes], [y[index] for index in indexes])
        if rho is not None:
            values.append(rho)
    if not values:
        return {"lower": None, "upper": None, "excludes_zero": False, "valid_repetitions": 0}
    values.sort()
    lower = values[int((len(values) - 1) * 0.025)]
    upper = values[int((len(values) - 1) * 0.975)]
    return {
        "lower": round(lower, 4),
        "upper": round(upper, 4),
        "excludes_zero": lower > 0 or upper < 0,
        "valid_repetitions": len(values),
    }


def _spearman(x: Iterable[float], y: Iterable[float]) -> float | None:
    x_values, y_values = list(x), list(y)
    if len(x_values) < 3 or len(x_values) != len(y_values):
        return None
    x_ranks, y_ranks = _average_ranks(x_values), _average_ranks(y_values)
    x_mean, y_mean = fmean(x_ranks), fmean(y_ranks)
    numerator = sum((left - x_mean) * (right - y_mean) for left, right in zip(x_ranks, y_ranks))
    x_denominator = sum((value - x_mean) ** 2 for value in x_ranks)
    y_denominator = sum((value - y_mean) ** 2 for value in y_ranks)
    if not x_denominator or not y_denominator:
        return None
    return numerator / math.sqrt(x_denominator * y_denominator)


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[start][1]:
            end += 1
        rank = (start + end + 2) / 2
        for index in range(start, end + 1):
            ranks[ordered[index][0]] = rank
        start = end + 1
    return ranks


def _conclusion(outcomes: dict[str, Any], associations: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    labels = outcomes["human_should_recommend"]
    if not labels["available"]:
        reasons.extend([
            "human_should_recommend_labels_unavailable",
            "false_interruption_outcome_unavailable",
            "missed_opportunity_outcome_unavailable",
        ])
    stable_feedback_join = [
        feature for feature, result in associations["explicit_feedback_join"].get("features", {}).items()
        if result.get("stable")
    ]
    stable_feedback_score = [
        feature for feature, result in associations["explicit_feedback_score"].get("features", {}).items()
        if result.get("stable")
    ]
    stable_feedback = sorted(set(stable_feedback_join + stable_feedback_score))
    stable_false_interruption = [
        feature for feature, result in associations["false_interruption"].get("features", {}).items()
        if result.get("stable")
    ]
    if not stable_feedback:
        reasons.append("no_stable_explicit_feedback_relationship")
    if labels["available"] and not stable_false_interruption:
        reasons.append("no_stable_false_interruption_relationship")
    candidate = labels["coverage_rate"] == 1.0 and bool(stable_feedback) and bool(stable_false_interruption)
    return {
        "status": "candidate_for_shadow" if candidate else "no_candidate",
        "candidate_simulation_required": candidate,
        "stable_explicit_feedback_features": stable_feedback,
        "stable_explicit_feedback_join_features": stable_feedback_join,
        "stable_explicit_feedback_score_features": stable_feedback_score,
        "stable_false_interruption_features": stable_false_interruption,
        "reason_codes": [] if candidate else reasons,
        "statement": (
            "A timing relationship is eligible for one separate Testbench-only shadow simulation."
            if candidate
            else "No timing/fatigue candidate is eligible from this frozen evidence."
        ),
    }


def _unavailable_outcome(reason: str | None) -> dict[str, Any]:
    return {"available": False, "reason": reason, "features": {}}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seed(base: int, feature: str, outcome: str) -> int:
    digest = hashlib.sha256(f"{feature}:{outcome}".encode("utf-8")).digest()
    return base + int.from_bytes(digest[:4], "big")


__all__ = ["analyze_timing_fatigue_baseline"]
