"""Offline PASS/no-op score-threshold analysis over adjudicated Shadow labels."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any

THRESHOLD_ANALYSIS_VERSION = 1
ADJUDICATED_STATUSES = {
    "completed",
    "retain_primary_low_confidence",
    "retain_primary_minor_difference",
}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _value(metric: dict[str, Any], default: float) -> float:
    value = metric.get("value")
    return float(value) if isinstance(value, (int, float)) else default


def _metric_rows(
    rows: list[dict[str, Any]],
    *,
    threshold: float | None,
    incremental: bool,
) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    selected: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for row in rows:
        if threshold is None:
            predicted = row["production_delivered"]
        elif incremental:
            predicted = (
                row["production_delivered"]
                and row["top1_score"] >= threshold
            )
        else:
            predicted = row["top1_score"] >= threshold
        should = row["should_recommend"]
        if predicted and should:
            tp += 1
        elif predicted and not should:
            fp += 1
        elif not predicted and not should:
            tn += 1
        else:
            fn += 1
        (selected if predicted else suppressed).append(row)
    total = tp + fp + tn + fn
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    false_interruption_denominator = fp + tn
    missed_opportunity_denominator = fn + tp
    f1_denominator = 2 * tp + fp + fn
    return {
        "threshold": threshold,
        "mode": "incremental_gate" if incremental else "score_only",
        "selected_count": len(selected),
        "pass_count": len(suppressed),
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "decision_accuracy": {
            "numerator": tp + tn,
            "denominator": total,
            "value": _rate(tp + tn, total),
        },
        "precision": {
            "numerator": tp,
            "denominator": precision_denominator,
            "value": _rate(tp, precision_denominator),
        },
        "recall": {
            "numerator": tp,
            "denominator": recall_denominator,
            "value": _rate(tp, recall_denominator),
        },
        "f1": {
            "numerator": 2 * tp,
            "denominator": f1_denominator,
            "value": _rate(2 * tp, f1_denominator),
        },
        "false_interruption_rate": {
            "numerator": fp,
            "denominator": false_interruption_denominator,
            "value": _rate(fp, false_interruption_denominator),
        },
        "missed_opportunity_rate": {
            "numerator": fn,
            "denominator": missed_opportunity_denominator,
            "value": _rate(fn, missed_opportunity_denominator),
        },
    }


def _effective_label(annotation: dict[str, Any]) -> tuple[bool | None, str]:
    adjudication_status = str(annotation.get("adjudication_status") or "")
    if adjudication_status == "excluded_abstention":
        return None, "excluded_abstention"
    if adjudication_status in ADJUDICATED_STATUSES:
        value = annotation.get("adjudicated_should_recommend")
        return (value if isinstance(value, bool) else None), "adjudicated"
    if annotation.get("primary_review_status") == "abstained":
        return None, "primary_abstained"
    value = annotation.get("should_recommend")
    return (value if isinstance(value, bool) else None), "primary"


def _analysis_rows(workbook: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for annotation in workbook.get("annotations") or []:
        turn_id = str(annotation.get("turn_id") or "")
        should, label_source = _effective_label(annotation)
        context = dict(annotation.get("context_for_review") or {})
        realization = dict(annotation.get("realization_review_context") or {})
        score = context.get("top1_score")
        delivered = context.get("delivered")
        if not isinstance(delivered, bool):
            delivered = realization.get("delivered")
        candidates = list(context.get("candidates") or [])
        top = candidates[0] if candidates else {}
        issues = []
        if not isinstance(should, bool):
            issues.append(label_source)
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            issues.append("missing_top1_score")
        if not isinstance(delivered, bool):
            issues.append("missing_production_delivery")
        if issues:
            excluded.append({"turn_id": turn_id, "issues": issues})
            continue
        rows.append({
            "turn_id": turn_id,
            "top1_score": float(score),
            "top1_candidate_id": str(top.get("id") or ""),
            "top1_source": str(top.get("source_type") or context.get("top1") or "unknown"),
            "activity": str(context.get("activity") or "unknown"),
            "production_delivered": delivered,
            "should_recommend": should,
            "label_source": label_source,
            "adjudication_status": annotation.get("adjudication_status"),
        })
    return rows, excluded


def _roc_auc(rows: list[dict[str, Any]]) -> float | None:
    positives = [row["top1_score"] for row in rows if row["should_recommend"]]
    negatives = [row["top1_score"] for row in rows if not row["should_recommend"]]
    denominator = len(positives) * len(negatives)
    if not denominator:
        return None
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    return round(wins / denominator, 4)


def _candidate_key(metric: dict[str, Any]) -> tuple[float, float, float, float]:
    accuracy = _value(metric["decision_accuracy"], 0.0)
    false_rate = _value(metric["false_interruption_rate"], 1.0)
    missed_rate = _value(metric["missed_opportunity_rate"], 1.0)
    threshold = float(metric["threshold"] or 0.0)
    return (accuracy, -false_rate, -missed_rate, -threshold)


def _safety_key(metric: dict[str, Any]) -> tuple[float, float, float, float]:
    false_rate = _value(metric["false_interruption_rate"], 1.0)
    accuracy = _value(metric["decision_accuracy"], 0.0)
    missed_rate = _value(metric["missed_opportunity_rate"], 1.0)
    threshold = float(metric["threshold"] or 0.0)
    return (-false_rate, accuracy, -missed_rate, -threshold)


def _pareto_frontier(curve: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frontier = []
    for row in curve:
        accuracy = _value(row["decision_accuracy"], 0.0)
        false_rate = _value(row["false_interruption_rate"], 1.0)
        missed_rate = _value(row["missed_opportunity_rate"], 1.0)
        dominated = False
        for other in curve:
            if other is row:
                continue
            other_accuracy = _value(other["decision_accuracy"], 0.0)
            other_false = _value(other["false_interruption_rate"], 1.0)
            other_missed = _value(other["missed_opportunity_rate"], 1.0)
            if (
                other_accuracy >= accuracy
                and other_false <= false_rate
                and other_missed <= missed_rate
                and (
                    other_accuracy > accuracy
                    or other_false < false_rate
                    or other_missed < missed_rate
                )
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(row)
    return frontier


def analyze_pass_noop_thresholds(
    workbook: dict[str, Any],
    *,
    thresholds: list[float] | None = None,
    missed_opportunity_max_increase: float = 0.05,
) -> dict[str, Any]:
    """Evaluate a score gate added after the observed production decision."""
    rows, excluded = _analysis_rows(workbook)
    if not rows:
        raise ValueError("no threshold-eligible annotations")
    if thresholds is None:
        unique_scores = {round(row["top1_score"], 3) for row in rows}
        thresholds = sorted({0.0, *unique_scores, round(max(unique_scores) + 0.001, 3)})
    cleaned = []
    for threshold in thresholds:
        value = float(threshold)
        if not 0.0 <= value <= 1.0:
            raise ValueError("thresholds must be within 0-1")
        cleaned.append(round(value, 6))
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("thresholds must be unique")
    cleaned.sort()

    baseline = _metric_rows(rows, threshold=None, incremental=True)
    curve = [
        _metric_rows(rows, threshold=threshold, incremental=True)
        for threshold in cleaned
    ]
    score_only_curve = [
        _metric_rows(rows, threshold=threshold, incremental=False)
        for threshold in cleaned
    ]
    best_accuracy = max(curve, key=_candidate_key)
    baseline_missed = _value(baseline["missed_opportunity_rate"], 0.0)
    guarded = [
        row
        for row in curve
        if _value(row["missed_opportunity_rate"], 0.0)
        <= baseline_missed + float(missed_opportunity_max_increase)
        and _value(row["false_interruption_rate"], 1.0)
        <= _value(baseline["false_interruption_rate"], 1.0)
    ]
    guarded_candidate = max(guarded, key=_safety_key) if guarded else None
    selected_threshold = (
        guarded_candidate["threshold"]
        if guarded_candidate is not None
        else best_accuracy["threshold"]
    )
    selected_metric = next(
        row for row in curve if row["threshold"] == selected_threshold
    )
    strictly_dominant_nonzero = [
        row
        for row in curve
        if float(row["threshold"] or 0.0) > 0.0
        and _value(row["decision_accuracy"], 0.0)
        >= _value(baseline["decision_accuracy"], 0.0)
        and _value(row["false_interruption_rate"], 1.0)
        <= _value(baseline["false_interruption_rate"], 1.0)
        and _value(row["missed_opportunity_rate"], 1.0)
        <= _value(baseline["missed_opportunity_rate"], 1.0)
        and (
            _value(row["decision_accuracy"], 0.0)
            > _value(baseline["decision_accuracy"], 0.0)
            or _value(row["false_interruption_rate"], 1.0)
            < _value(baseline["false_interruption_rate"], 1.0)
            or _value(row["missed_opportunity_rate"], 1.0)
            < _value(baseline["missed_opportunity_rate"], 1.0)
        )
    ]

    baseline_delivered = {
        row["turn_id"] for row in rows if row["production_delivered"]
    }
    kept = {
        row["turn_id"]
        for row in rows
        if row["production_delivered"] and row["top1_score"] >= selected_threshold
    }
    changed = [
        row
        for row in rows
        if row["turn_id"] in baseline_delivered - kept
    ]
    source_impact = []
    for source in sorted({row["top1_source"] for row in rows}):
        source_rows = [row for row in rows if row["top1_source"] == source]
        delivered_rows = [row for row in source_rows if row["production_delivered"]]
        kept_rows = [
            row for row in delivered_rows
            if row["top1_score"] >= selected_threshold
        ]
        source_impact.append({
            "source": source,
            "eligible_count": len(source_rows),
            "production_delivered_count": len(delivered_rows),
            "kept_count": len(kept_rows),
            "suppressed_count": len(delivered_rows) - len(kept_rows),
            "retention_rate": _rate(len(kept_rows), len(delivered_rows)),
        })
    boundary = sorted(
        (
            {
                "turn_id": row["turn_id"],
                "score": row["top1_score"],
                "source": row["top1_source"],
                "should_recommend": row["should_recommend"],
                "production_delivered": row["production_delivered"],
                "distance": round(abs(row["top1_score"] - selected_threshold), 4),
            }
            for row in rows
            if abs(row["top1_score"] - selected_threshold) <= 0.02
        ),
        key=lambda row: (row["distance"], row["turn_id"]),
    )
    positive_scores = [
        row["top1_score"] for row in rows if row["should_recommend"]
    ]
    negative_scores = [
        row["top1_score"] for row in rows if not row["should_recommend"]
    ]
    score_distribution = {
        "positive_count": len(positive_scores),
        "negative_count": len(negative_scores),
        "positive_mean": (
            round(sum(positive_scores) / len(positive_scores), 4)
            if positive_scores
            else None
        ),
        "negative_mean": (
            round(sum(negative_scores) / len(negative_scores), 4)
            if negative_scores
            else None
        ),
        "roc_auc": _roc_auc(rows),
    }
    label_sources = Counter(row["label_source"] for row in rows)
    input_hash = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "analysis_version": THRESHOLD_ANALYSIS_VERSION,
        "kind": "recommendation_p44f1_pass_noop_threshold_analysis",
        "model": {
            "primary": (
                "incremental safety gate: production_delivered AND "
                "top1_score >= threshold"
            ),
            "secondary": "score-only diagnostic: top1_score >= threshold",
        },
        "eligible_count": len(rows),
        "excluded_count": len(excluded),
        "excluded": excluded,
        "label_source_distribution": dict(sorted(label_sources.items())),
        "input_hash": input_hash,
        "score_distribution": score_distribution,
        "production_baseline": baseline,
        "threshold_curve": curve,
        "score_only_curve": score_only_curve,
        "pareto_frontier": _pareto_frontier(curve),
        "exploratory_candidates": {
            "best_accuracy": best_accuracy,
            "guarded": guarded_candidate,
            "selected_for_report": selected_metric,
            "guardrail": {
                "missed_opportunity_max_increase": float(
                    missed_opportunity_max_increase
                ),
                "false_interruption_must_not_increase": True,
            },
        },
        "selected_threshold_impact": {
            "threshold": selected_threshold,
            "suppressed_deliveries": [
                {
                    "turn_id": row["turn_id"],
                    "score": row["top1_score"],
                    "source": row["top1_source"],
                    "should_recommend": row["should_recommend"],
                    "change": (
                        "false_interruption_removed"
                        if not row["should_recommend"]
                        else "new_missed_opportunity"
                    ),
                }
                for row in sorted(changed, key=lambda item: item["turn_id"])
            ],
            "source_impact": source_impact,
            "boundary_cases": boundary,
        },
        "conclusion": {
            "production_candidate_status": (
                "candidate_available"
                if strictly_dominant_nonzero
                else "no_universal_threshold_candidate"
            ),
            "strictly_dominant_nonzero_thresholds": [
                row["threshold"] for row in strictly_dominant_nonzero
            ],
            "finding": (
                "No non-zero threshold improves or preserves accuracy, "
                "false interruption, and missed opportunity simultaneously."
                if not strictly_dominant_nonzero
                else "At least one non-zero threshold strictly dominates baseline."
            ),
            "next_step": (
                "analyze timing/fatigue and repetition features before any "
                "production threshold proposal"
            ),
        },
        "limitations": [
            "single observational cohort; no train/validation split",
            "eight grade-B labels retain the primary review at low confidence",
            "incremental threshold can suppress deliveries but cannot recover existing PASS decisions",
            "shadow scores are rounded production observation values",
            "timing, fatigue, repetition, and source diversity are held outside P44-F1",
        ],
        "production_config_modified": False,
        "tuning_modified": False,
    }


__all__ = ["analyze_pass_noop_thresholds"]
