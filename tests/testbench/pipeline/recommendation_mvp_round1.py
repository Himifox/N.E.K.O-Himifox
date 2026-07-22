"""One-shot paired candidate selection for the Recommendation MVP.

The analysis is deliberately bounded to four arms over one immutable Golden
cohort: observed baseline, conservative source calibration, delivered-only
diversity history, and their combination.  It never changes PASS/gating,
production configuration, or tuning.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import math
from typing import Any


ROUND1_VERSION = 1
ADJUDICATED_STATUSES = {
    "completed",
    "retain_primary_low_confidence",
    "retain_primary_minor_difference",
}
ARMS = ("baseline", "source_calibration", "delivered_history", "combined")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _effective_labels(annotation: dict[str, Any]) -> tuple[bool | None, dict[str, int], bool]:
    status = str(annotation.get("adjudication_status") or "")
    if annotation.get("primary_review_status") == "abstained" or status == "excluded_abstention":
        return None, {}, False
    use_adjudicated = status in ADJUDICATED_STATUSES
    should = (
        annotation.get("adjudicated_should_recommend")
        if use_adjudicated
        else annotation.get("should_recommend")
    )
    raw_relevance = (
        annotation.get("adjudicated_relevance")
        if use_adjudicated
        else annotation.get("relevance")
    ) or {}
    relevance = {
        str(candidate_id): int(value)
        for candidate_id, value in raw_relevance.items()
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3
    }
    return should if isinstance(should, bool) else None, relevance, isinstance(should, bool)


def _largest_gap_split(observations: list[dict[str, Any]], minimum_gap_seconds: float) -> tuple[int, float]:
    if len(observations) < 2:
        raise ValueError("at least two observations are required")
    gaps = []
    for index in range(1, len(observations)):
        previous = observations[index - 1].get("ts")
        current = observations[index].get("ts")
        if not isinstance(previous, (int, float)) or not isinstance(current, (int, float)):
            raise ValueError("every observation must have numeric ts")
        gaps.append((float(current) - float(previous), index))
    gap, index = max(gaps)
    if gap < minimum_gap_seconds:
        raise ValueError(
            f"no independent collection split: largest gap {gap:.3f}s < {minimum_gap_seconds:.3f}s"
        )
    return index, gap


def _diversity_penalty(
    source: str,
    candidate_id: str,
    history: deque[tuple[str, str]],
) -> float:
    recent = list(history)
    recent_sources = [item[0] for item in recent][-8:]
    recent_ids = [item[1] for item in recent]
    source_repeat_count = recent_sources.count(source)
    source_streak = 0
    for value in reversed(recent_sources):
        if value != source:
            break
        source_streak += 1
    source_repeat = min(0.16, 0.04 * source_repeat_count)
    streak = min(0.12, 0.06 * source_streak)
    candidate_repeat = 0.12 if candidate_id in recent_ids else 0.0
    return min(0.30, source_repeat + streak + candidate_repeat)


def _derive_source_calibration(
    observations: list[dict[str, Any]],
    annotations: dict[str, dict[str, Any]],
    *,
    cap: float,
    minimum_rows: int,
    minimum_relevance_levels: int,
) -> dict[str, Any]:
    by_source: dict[str, list[tuple[float, int]]] = {}
    all_gaps: list[float] = []
    for observation in observations:
        annotation = annotations.get(str(observation.get("turn_id") or ""))
        if not annotation:
            continue
        _, relevance, eligible = _effective_labels(annotation)
        if not eligible:
            continue
        candidates = list((annotation.get("context_for_review") or {}).get("candidates") or [])
        for candidate in candidates:
            candidate_id = str(candidate.get("id") or "")
            source = str(candidate.get("source_type") or "unknown")
            score = candidate.get("score")
            human = relevance.get(candidate_id)
            if not isinstance(score, (int, float)) or isinstance(score, bool) or human is None:
                continue
            gap = human / 3.0 - float(score)
            all_gaps.append(gap)
            by_source.setdefault(source, []).append((gap, human))
    if not all_gaps:
        raise ValueError("discovery split has no scored human-relevance candidates")
    global_gap = sum(all_gaps) / len(all_gaps)
    rows = []
    adjustments: dict[str, float] = {}
    for source in sorted(by_source):
        values = by_source[source]
        mean_gap = sum(item[0] for item in values) / len(values)
        levels = sorted({item[1] for item in values})
        supported = len(values) >= minimum_rows and len(levels) >= minimum_relevance_levels
        adjustment = _clamp(mean_gap - global_gap, -cap, cap) if supported else 0.0
        adjustments[source] = round(adjustment, 6)
        rows.append({
            "source": source,
            "candidate_rows": len(values),
            "relevance_levels": levels,
            "mean_normalized_relevance_minus_score": round(mean_gap, 6),
            "supported": supported,
            "adjustment": round(adjustment, 6),
        })
    return {
        "global_normalized_relevance_minus_score": round(global_gap, 6),
        "cap": cap,
        "minimum_candidate_rows": minimum_rows,
        "minimum_relevance_levels": minimum_relevance_levels,
        "adjustments": adjustments,
        "sources": rows,
    }


def _simulate_segment(
    observations: list[dict[str, Any]],
    annotations: dict[str, dict[str, Any]],
    adjustments: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected_history: dict[str, deque[tuple[str, str]]] = {}
    delivered_history: dict[str, deque[tuple[str, str]]] = {}
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for observation in observations:
        turn_id = str(observation.get("turn_id") or "")
        character = str(observation.get("lanlan_name") or "unknown")
        selected = selected_history.setdefault(character, deque(maxlen=20))
        delivered = delivered_history.setdefault(character, deque(maxlen=20))
        annotation = annotations.get(turn_id)
        if annotation is None:
            errors.append({"turn_id": turn_id, "issue": "missing_annotation"})
            continue
        should, relevance, metric_eligible = _effective_labels(annotation)
        context = dict(annotation.get("context_for_review") or {})
        candidates = []
        for rank, candidate in enumerate(context.get("candidates") or [], 1):
            candidate_id = str(candidate.get("id") or "")
            source = str(candidate.get("source_type") or "unknown")
            score = candidate.get("score")
            if not candidate_id or not isinstance(score, (int, float)) or isinstance(score, bool):
                errors.append({"turn_id": turn_id, "issue": "invalid_candidate", "rank": rank})
                continue
            baseline = float(score)
            selected_penalty = _diversity_penalty(source, candidate_id, selected)
            delivered_penalty = _diversity_penalty(source, candidate_id, delivered)
            delivered_history_score = _clamp(baseline + selected_penalty - delivered_penalty)
            source_score = _clamp(baseline + float(adjustments.get(source, 0.0)))
            combined_score = _clamp(
                delivered_history_score + float(adjustments.get(source, 0.0))
            )
            candidates.append({
                "id": candidate_id,
                "source": source,
                "baseline_rank": rank,
                "human_relevance": relevance.get(candidate_id),
                "selected_history_repeat": candidate_id in {item[1] for item in selected},
                "delivered_history_repeat": candidate_id in {item[1] for item in delivered},
                "selected_history_penalty": round(selected_penalty, 6),
                "delivered_history_penalty": round(delivered_penalty, 6),
                "scores": {
                    "baseline": round(baseline, 6),
                    "source_calibration": round(source_score, 6),
                    "delivered_history": round(delivered_history_score, 6),
                    "combined": round(combined_score, 6),
                },
            })
        expected = str(observation.get("shadow_selected_candidate_id") or "")
        context_top = candidates[0]["id"] if candidates else ""
        if candidates and expected and context_top != expected:
            errors.append({
                "turn_id": turn_id,
                "issue": "candidate_alignment",
                "observation_top1": expected,
                "annotation_top1": context_top,
            })
        arm_rankings = {}
        for arm in ARMS:
            arm_rankings[arm] = sorted(
                candidates,
                key=lambda item: (-item["scores"][arm], item["baseline_rank"]),
            )
        rows.append({
            "turn_id": turn_id,
            "ts": observation.get("ts"),
            "activity": str(observation.get("activity_state") or context.get("activity") or "unknown"),
            "should_recommend": should,
            "metric_eligible": metric_eligible,
            "high_confidence": str(annotation.get("adjudication_grade") or "") not in {"B", "C"},
            "production_delivered": observation.get("delivered") if isinstance(observation.get("delivered"), bool) else None,
            "candidates": candidates,
            "rankings": arm_rankings,
        })
        selected_source = str(observation.get("shadow_selected_source_type") or "")
        selected_id = str(observation.get("shadow_selected_candidate_id") or "")
        if selected_source and selected_id:
            selected.append((selected_source, selected_id))
            if observation.get("delivered") is True:
                delivered.append((selected_source, selected_id))
    return rows, errors


def _ranking_metrics(rows: list[dict[str, Any]], arm: str, *, high_confidence: bool) -> dict[str, Any]:
    scene_count = positive_count = hits = changed = 0
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    top1_relevance: list[int] = []
    exposure: Counter[str] = Counter()
    repeat_top1 = 0
    for row in rows:
        if not row["metric_eligible"] or (high_confidence and not row["high_confidence"]):
            continue
        ranked = list(row["rankings"][arm])
        baseline = list(row["rankings"]["baseline"])
        if not ranked:
            continue
        scene_count += 1
        top = ranked[0]
        exposure[top["source"]] += 1
        repeat_top1 += int(bool(top["selected_history_repeat"]))
        if baseline and top["id"] != baseline[0]["id"]:
            changed += 1
        if isinstance(top.get("human_relevance"), int):
            top1_relevance.append(int(top["human_relevance"]))
        labels = [
            int(candidate["human_relevance"])
            for candidate in ranked
            if isinstance(candidate.get("human_relevance"), int)
        ]
        best = max(labels, default=None)
        if row["should_recommend"] is not True or best is None or best <= 0:
            continue
        positive_count += 1
        hits += int(top.get("human_relevance") == best)
        first_relevant = next(
            (index for index, candidate in enumerate(ranked, 1)
             if isinstance(candidate.get("human_relevance"), int)
             and candidate["human_relevance"] > 0),
            None,
        )
        reciprocal_ranks.append(1.0 / first_relevant if first_relevant else 0.0)
        dcg = sum(
            (2 ** int(candidate.get("human_relevance") or 0) - 1) / math.log2(rank + 1)
            for rank, candidate in enumerate(ranked[:3], 1)
        )
        ideal = sorted(labels, reverse=True)[:3]
        idcg = sum(
            (2 ** value - 1) / math.log2(rank + 1)
            for rank, value in enumerate(ideal, 1)
        )
        if idcg:
            ndcgs.append(dcg / idcg)
    shares = {
        source: count / scene_count
        for source, count in exposure.items()
    } if scene_count else {}
    return {
        "scene_count": scene_count,
        "positive_rank_case_count": positive_count,
        "hit_at_1": {
            "numerator": hits,
            "denominator": positive_count,
            "value": _rate(hits, positive_count),
        },
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else None,
        "ndcg_at_3": round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else None,
        "mean_top1_relevance": round(sum(top1_relevance) / len(top1_relevance), 4) if top1_relevance else None,
        "changed_top1_count": changed,
        "selected_history_repeat_top1_rate": _rate(repeat_top1, scene_count),
        "source_exposure": dict(sorted(exposure.items())),
        "max_source_exposure": round(max(shares.values()), 4) if shares else None,
        "source_hhi": round(sum(value * value for value in shares.values()), 4) if shares else None,
    }


def _passes_gate(candidate: dict[str, Any], baseline: dict[str, Any], guardrails: dict[str, float]) -> dict[str, Any]:
    checks = {
        "hit_at_1_not_lower": candidate["hit_at_1"]["numerator"] >= baseline["hit_at_1"]["numerator"],
        "mrr_not_lower": float(candidate["mrr"] or 0.0) >= float(baseline["mrr"] or 0.0),
        "ndcg_at_3_not_lower": float(candidate["ndcg_at_3"] or 0.0) >= float(baseline["ndcg_at_3"] or 0.0),
        "strict_quality_improvement": (
            candidate["hit_at_1"]["numerator"] > baseline["hit_at_1"]["numerator"]
            or float(candidate["ndcg_at_3"] or 0.0)
            >= float(baseline["ndcg_at_3"] or 0.0) + guardrails["minimum_ndcg_improvement"]
        ),
        "hhi_guardrail": float(candidate["source_hhi"] or 1.0)
        <= float(baseline["source_hhi"] or 0.0) + guardrails["maximum_hhi_increase"],
        "max_source_exposure_guardrail": float(candidate["max_source_exposure"] or 1.0)
        <= float(baseline["max_source_exposure"] or 0.0) + guardrails["maximum_source_share_increase"],
    }
    return {"passed": all(checks.values()), "checks": checks}


def analyze_recommendation_mvp_round1(
    source_freeze: dict[str, Any],
    adjudicated_workbook: dict[str, Any],
    *,
    source_sha256: str = "",
    labels_sha256: str = "",
    split_index: int | None = None,
    minimum_split_gap_seconds: float = 3600.0,
    source_delta_cap: float = 0.04,
    minimum_source_candidate_rows: int = 12,
    minimum_relevance_levels: int = 2,
    maximum_hhi_increase: float = 0.03,
    maximum_source_share_increase: float = 0.05,
    minimum_ndcg_improvement: float = 0.005,
) -> dict[str, Any]:
    """Run the fixed four-arm round and select at most one MVP candidate."""
    source = deepcopy(source_freeze)
    workbook = deepcopy(adjudicated_workbook)
    observations = sorted(
        list(source.get("observations") or []),
        key=lambda item: float(item.get("ts") or 0.0),
    )
    annotations_list = list(workbook.get("annotations") or [])
    annotations = {str(item.get("turn_id") or ""): item for item in annotations_list}
    if len(annotations) != len(annotations_list):
        raise ValueError("annotation turn_id values must be unique and non-empty")
    if split_index is None:
        split_index, split_gap = _largest_gap_split(observations, minimum_split_gap_seconds)
    else:
        if not 1 <= split_index < len(observations):
            raise ValueError("split_index must separate discovery and holdout observations")
        split_gap = float(observations[split_index]["ts"]) - float(observations[split_index - 1]["ts"])

    discovery_observations = observations[:split_index]
    holdout_observations = observations[split_index:]
    calibration = _derive_source_calibration(
        discovery_observations,
        annotations,
        cap=float(source_delta_cap),
        minimum_rows=int(minimum_source_candidate_rows),
        minimum_relevance_levels=int(minimum_relevance_levels),
    )
    discovery_rows, discovery_errors = _simulate_segment(
        discovery_observations, annotations, calibration["adjustments"]
    )
    holdout_rows, holdout_errors = _simulate_segment(
        holdout_observations, annotations, calibration["adjustments"]
    )
    errors = discovery_errors + holdout_errors
    partitions = {
        "discovery": discovery_rows,
        "holdout": holdout_rows,
        "full": discovery_rows + holdout_rows,
    }
    metrics = {}
    for partition, rows in partitions.items():
        metrics[partition] = {
            arm: {
                "all_eligible": _ranking_metrics(rows, arm, high_confidence=False),
                "high_confidence": _ranking_metrics(rows, arm, high_confidence=True),
            }
            for arm in ARMS
        }

    guardrails = {
        "maximum_hhi_increase": float(maximum_hhi_increase),
        "maximum_source_share_increase": float(maximum_source_share_increase),
        "minimum_ndcg_improvement": float(minimum_ndcg_improvement),
    }
    baseline = metrics["holdout"]["baseline"]
    gates = {}
    passing = []
    for arm in ARMS[1:]:
        all_gate = _passes_gate(
            metrics["holdout"][arm]["all_eligible"],
            baseline["all_eligible"],
            guardrails,
        )
        confidence_gate = _passes_gate(
            metrics["holdout"][arm]["high_confidence"],
            baseline["high_confidence"],
            guardrails,
        )
        gate = {
            "passed": not errors and all_gate["passed"] and confidence_gate["passed"],
            "all_eligible": all_gate,
            "high_confidence": confidence_gate,
        }
        gates[arm] = gate
        if gate["passed"]:
            passing.append(arm)
    complexity = {"source_calibration": 0, "delivered_history": 1, "combined": 2}
    if passing:
        selected_arm = max(
            passing,
            key=lambda arm: (
                metrics["holdout"][arm]["all_eligible"]["hit_at_1"]["numerator"],
                float(metrics["holdout"][arm]["all_eligible"]["ndcg_at_3"] or 0.0),
                float(metrics["holdout"][arm]["all_eligible"]["mrr"] or 0.0),
                -float(metrics["holdout"][arm]["all_eligible"]["source_hhi"] or 1.0),
                -complexity[arm],
            ),
        )
        status = "candidate_selected"
    else:
        selected_arm = "baseline"
        status = "baseline_retained"

    effective_count = sum(
        1 for item in annotations_list if _effective_labels(item)[2]
    )
    preview = source.get("quality_preview") or {}
    input_contract = {
        "observation_count": len(observations),
        "annotation_count": len(annotations_list),
        "metric_eligible_count": effective_count,
        "excluded_count": len(annotations_list) - effective_count,
        "duplicate_turn_ids": list(preview.get("duplicate_turn_ids") or []),
        "invalid_observation_indexes": list(preview.get("invalid_observation_indexes") or []),
        "mixed_algorithm_versions": bool(preview.get("mixed_algorithm_versions")),
        "simulation_errors": errors,
        "passed": (
            len(observations) == len(annotations_list)
            and not preview.get("duplicate_turn_ids")
            and not preview.get("invalid_observation_indexes")
            and not preview.get("mixed_algorithm_versions")
            and not errors
        ),
    }
    if not input_contract["passed"]:
        status = "blocked_input_contract"
        selected_arm = "baseline"

    return {
        "schema_version": 1,
        "analysis_version": ROUND1_VERSION,
        "kind": "recommendation_mvp_candidate_round1",
        "inputs": {
            "source_name": source.get("name"),
            "source_sha256": source_sha256,
            "labels_sha256": labels_sha256,
            "algorithm_versions": dict(preview.get("algorithm_versions") or {}),
        },
        "split": {
            "method": "largest_collection_gap",
            "index": split_index,
            "gap_seconds": round(split_gap, 3),
            "discovery_observations": len(discovery_observations),
            "holdout_observations": len(holdout_observations),
            "histories_reset_at_split": True,
        },
        "input_contract": input_contract,
        "source_calibration": calibration,
        "delivered_history_policy": {
            "baseline": "all shadow selections enter the diversity history",
            "candidate": "only observations with delivered=true enter the diversity history",
            "history_limit": 20,
            "source_window": 8,
            "formula_unchanged": True,
        },
        "guardrails": guardrails,
        "metrics": metrics,
        "candidate_gates": gates,
        "conclusion": {
            "status": status,
            "selected_arm": selected_arm,
            "passing_arms": passing,
            "next_step": (
                "implement only the selected arm behind an MVP Shadow feature flag"
                if status == "candidate_selected"
                else "retain the production baseline and finish the MVP with correctness fixes and opt-in rollout"
                if status == "baseline_retained"
                else "repair the frozen input contract before making any candidate decision"
            ),
        },
        "limitations": [
            "single-user observational Golden cohort",
            "source calibration is learned only from the pre-gap discovery segment",
            "the holdout segment is small and used only for MVP candidate selection",
            "ranking arms do not change PASS, interruption timing, or delivery generation",
            "delivered-history replay assumes the largest collection gap is a process/session reset",
        ],
        "production_config_modified": False,
        "tuning_modified": False,
        "mvp_modified": False,
    }


__all__ = ["analyze_recommendation_mvp_round1"]
