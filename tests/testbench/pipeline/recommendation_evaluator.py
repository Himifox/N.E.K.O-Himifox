"""Pure metrics and acceptance gates for recommendation experiments."""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

RECOMMENDATION_EVALUATOR_VERSION = "3"


def evaluate_case(scenario: dict[str, Any], snapshot: dict[str, Any], deterministic: bool) -> dict[str, Any]:
    oracle = scenario.get("oracle") or {}
    ranked = snapshot.get("ranked_candidates") or []
    ids = [str(row.get("id")) for row in ranked]
    sources = [str(row.get("source_type")) for row in ranked]
    top1 = sources[0] if sources else None
    violations = []
    for source in oracle.get("forbidden_sources") or []:
        if source in sources:
            violations.append({"code": "forbidden_source_ranked", "source": source})
    for cid in oracle.get("must_filter_candidate_ids") or []:
        if cid in ids:
            violations.append({"code": "candidate_not_filtered", "candidate_id": cid})
    for cid, reason in (oracle.get("expected_filter_reasons") or {}).items():
        if snapshot.get("filtered_reasons", {}).get(cid) != reason:
            violations.append({"code": "filter_reason_mismatch", "candidate_id": cid, "expected": reason})
    expected_empty = oracle.get("expected_empty")
    if expected_empty is True and ids:
        violations.append({"code": "expected_empty_but_ranked", "candidate_ids": ids})
    if expected_empty is False and not ids:
        violations.append({"code": "unexpected_empty_ranking"})
    if not deterministic:
        violations.append({"code": "non_deterministic"})
    if snapshot.get("diagnostics", {}).get("non_finite_candidate_ids"):
        violations.append({"code": "non_finite_score"})
    expected_bias = oracle.get("active_bias_expected")
    actual_bias = (snapshot.get("active_bias") or {}).get("applied")
    if expected_bias is not None and bool(expected_bias) != bool(actual_bias):
        violations.append({"code": "active_bias_mismatch", "expected": expected_bias})
    expected_bias_reason = oracle.get("expected_active_bias_reason")
    actual_bias_reason = (snapshot.get("active_bias") or {}).get("fallback_reason")
    if expected_bias_reason is not None and expected_bias_reason != actual_bias_reason:
        violations.append({"code": "active_bias_reason_mismatch", "expected": expected_bias_reason,
                           "actual": actual_bias_reason})

    relevance = dict(oracle.get("relevance") or {})
    evaluation_mode = scenario.get("evaluation_mode") or ("sequence" if scenario.get("kind") == "sequence"
                                                            else "ranking" if relevance else "contract")
    relevance_mode = "candidate" if relevance else "unlabelled"
    acceptable = set(oracle.get("acceptable_top1_sources") or [])
    unknown_relevance = sorted(set(relevance) - set(ids))
    if unknown_relevance:
        violations.append({"code": "unknown_relevance_candidate_ids", "candidate_ids": unknown_relevance})
    explicit_should_recommend = oracle.get("should_recommend")
    if isinstance(explicit_should_recommend, bool):
        should_recommend = explicit_should_recommend
    elif isinstance(expected_empty, bool):
        should_recommend = not expected_empty
    elif any(int(value) > 0 for value in relevance.values()):
        # Compatibility for ranking fixtures created before an explicit gate
        # label existed. All-zero relevance never implies a positive decision.
        should_recommend = True
    else:
        should_recommend = None
    predicted_recommend = bool(ids)
    gate_eligible = isinstance(should_recommend, bool)
    decision_correct = predicted_recommend == should_recommend if gate_eligible else None
    false_interruption = predicted_recommend and should_recommend is False if gate_eligible else None
    missed_opportunity = not predicted_recommend and should_recommend is True if gate_eligible else None

    best = max((int(value) for value in relevance.values()), default=None)
    positive_case_eligible = should_recommend is True and best is not None and best > 0
    hit1 = (bool(ids and ids[0] in relevance and int(relevance[ids[0]]) == best)
            if positive_case_eligible else None)
    hit3 = (bool(any(int(relevance.get(cid, 0)) == best for cid in ids[:3]))
            if positive_case_eligible else None)
    acceptable_top1 = (top1 in acceptable if positive_case_eligible and acceptable else None)
    quality_failures = []
    expected_candidate = oracle.get("expected_top1_candidate_id")
    if expected_candidate is not None and (ids[0] if ids else None) != expected_candidate:
        quality_failures.append({"code": "top1_candidate_mismatch", "expected": expected_candidate,
                                 "actual": ids[0] if ids else None})
    if acceptable_top1 is False:
        quality_failures.append({"code": "unacceptable_top1_source", "actual": top1,
                                 "acceptable": sorted(acceptable)})
    mrr = None
    ndcg3 = None
    if positive_case_eligible:
        mrr = 0.0
        for rank, cid in enumerate(ids, 1):
            if int(relevance.get(cid, 0)) > 0:
                mrr = 1.0 / rank
                break
        dcg = sum((2 ** int(relevance.get(cid, 0)) - 1) / math.log2(rank + 1)
                  for rank, cid in enumerate(ids[:3], 1))
        ideal = sorted((int(v) for v in relevance.values()), reverse=True)[:3]
        idcg = sum((2 ** rel - 1) / math.log2(rank + 1) for rank, rel in enumerate(ideal, 1))
        ndcg3 = round(dcg / idcg, 4) if idcg else None
    return {"violations": violations, "quality_failures": quality_failures,
            "passed": not violations and not quality_failures,
            "evaluation_mode": evaluation_mode, "relevance_mode": relevance_mode,
            "gate_eligible": gate_eligible, "should_recommend": should_recommend,
            "predicted_recommend": predicted_recommend, "decision_correct": decision_correct,
            "false_interruption": false_interruption, "missed_opportunity": missed_opportunity,
            "positive_case_eligible": positive_case_eligible,
            "hit1": hit1, "hit3": hit3, "acceptable_top1": acceptable_top1,
            "mrr": round(mrr, 4) if mrr is not None else None, "ndcg3": ndcg3,
            "top1_source": top1, "top1_candidate_id": ids[0] if ids else None, "score_gap": snapshot.get("score_gap")}


def aggregate_variant(cases: list[dict[str, Any]]) -> dict[str, Any]:
    evaluations = [row["evaluation"] for row in cases if not row.get("error")]
    ranking = [row for row in evaluations if row.get("positive_case_eligible")]
    gate = [row for row in evaluations if row.get("gate_eligible")]
    def avg(key: str, rows=ranking):
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)]
        return round(sum(values) / len(values), 4) if values else None
    def rate(key: str, rows=ranking):
        values = [row[key] for row in rows if isinstance(row.get(key), bool)]
        return round(sum(bool(v) for v in values) / len(values), 4) if values else None
    def fraction(key: str, rows=ranking):
        values = [row[key] for row in rows if isinstance(row.get(key), bool)]
        numerator = sum(bool(v) for v in values)
        return {"numerator": numerator, "denominator": len(values),
                "value": round(numerator / len(values), 4) if values else None}
    sources = Counter(row.get("top1_source") for row in evaluations if row.get("top1_source"))
    total = sum(sources.values())
    shares = {k: round(v / total, 4) for k, v in sorted(sources.items())} if total else {}
    hhi = round(sum(v * v for v in shares.values()), 4)
    candidates = [row.get("top1_candidate_id") for row in evaluations if row.get("top1_candidate_id")]
    repeat_rate = round(1 - len(set(candidates)) / len(candidates), 4) if candidates else 0.0
    error_count = sum(bool(row.get("error")) for row in cases)
    hard_count = sum(len(row.get("violations") or []) for row in evaluations)
    ndcg_values = [float(row["ndcg3"]) for row in ranking if isinstance(row.get("ndcg3"), (int, float))]
    gate_tp = sum(row.get("predicted_recommend") is True and row.get("should_recommend") is True for row in gate)
    gate_fp = sum(row.get("predicted_recommend") is True and row.get("should_recommend") is False for row in gate)
    gate_tn = sum(row.get("predicted_recommend") is False and row.get("should_recommend") is False for row in gate)
    gate_fn = sum(row.get("predicted_recommend") is False and row.get("should_recommend") is True for row in gate)
    precision_denominator = gate_tp + gate_fp
    recall_denominator = gate_tp + gate_fn
    precision = gate_tp / precision_denominator if precision_denominator else None
    recall = gate_tp / recall_denominator if recall_denominator else None
    false_interruption_denominator = gate_fp + gate_tn
    missed_opportunity_denominator = gate_fn + gate_tp
    return {"case_count": len(cases), "errored": error_count,
            "hard_violation_count": hard_count,
            "hit1": rate("hit1"), "hit3": rate("hit3"), "acceptable_top1_rate": rate("acceptable_top1"),
            "mrr": avg("mrr"), "ndcg3": avg("ndcg3"), "average_score_gap": avg("score_gap"),
            "source_distribution": shares, "max_source_exposure": max(shares.values(), default=0.0),
            "source_hhi": hhi, "candidate_repeat_rate": repeat_rate,
            "transparent_metrics": {
                "decision_accuracy_with_noop": fraction("decision_correct", gate),
                "gate_precision": {"numerator": gate_tp, "denominator": precision_denominator,
                                   "value": round(precision, 4) if precision is not None else None},
                "gate_recall": {"numerator": gate_tp, "denominator": recall_denominator,
                                "value": round(recall, 4) if recall is not None else None},
                "gate_f1": {"numerator": 2 * gate_tp,
                            "denominator": 2 * gate_tp + gate_fp + gate_fn,
                            "value": round((2 * gate_tp) / (2 * gate_tp + gate_fp + gate_fn), 4)
                            if 2 * gate_tp + gate_fp + gate_fn else None},
                "false_interruption_rate": {"numerator": gate_fp,
                                            "denominator": false_interruption_denominator,
                                            "value": round(gate_fp / false_interruption_denominator, 4)
                                            if false_interruption_denominator else None},
                "missed_opportunity_rate": {"numerator": gate_fn,
                                           "denominator": missed_opportunity_denominator,
                                           "value": round(gate_fn / missed_opportunity_denominator, 4)
                                           if missed_opportunity_denominator else None},
                "gate_confusion_matrix": {"tp": gate_tp, "fp": gate_fp, "tn": gate_tn, "fn": gate_fn},
                "positive_case_hit_at_1": fraction("hit1"),
                "positive_case_hit_at_3": fraction("hit3"),
                "acceptable_top1": fraction("acceptable_top1"),
                "positive_case_ndcg_at_3": {"sum": round(sum(ndcg_values), 4), "denominator": len(ndcg_values),
                                             "value": round(sum(ndcg_values) / len(ndcg_values), 4) if ndcg_values else None},
                # Compatibility aliases keep the old response shape with the
                # corrected positive-case denominator.
                "hit_at_1": fraction("hit1"), "hit_at_3": fraction("hit3"),
                "ndcg_at_3": {"sum": round(sum(ndcg_values), 4), "denominator": len(ndcg_values),
                               "value": round(sum(ndcg_values) / len(ndcg_values), 4) if ndcg_values else None},
                "hard_constraints": {"violations": hard_count, "evaluated_cases": len(evaluations)},
                "execution_errors": {"errors": error_count, "executed_cases": len(cases)},
            },
            "gate_eligible_count": len(gate), "ranking_eligible_count": len(ranking)}


def compare_variants(baseline_cases: list[dict[str, Any]], candidate_cases: list[dict[str, Any]]) -> dict[str, Any]:
    base = {row["scenario_id"]: row for row in baseline_cases}
    wins_detail, losses_detail, ties_detail, not_comparable = [], [], [], []
    changes = []
    for row in candidate_cases:
        other = base.get(row["scenario_id"])
        if not other or row.get("error") or other.get("error"):
            continue
        current, baseline = row["evaluation"], other["evaluation"]
        if not current.get("positive_case_eligible") or not baseline.get("positive_case_eligible"):
            not_comparable.append({"scenario_id": row["scenario_id"], "reason": "not_ranking_eligible"})
            continue
        a = _quality_tuple(current)
        b = _quality_tuple(baseline)
        detail = {"scenario_id": row["scenario_id"], "baseline_top1": baseline.get("top1_source"),
                  "candidate_top1": current.get("top1_source"),
                  "baseline_acceptable": baseline.get("acceptable_top1"),
                  "candidate_acceptable": current.get("acceptable_top1"),
                  "baseline_ndcg": baseline.get("ndcg3"), "candidate_ndcg": current.get("ndcg3")}
        if a == b:
            detail["reason"] = "ranking_quality_unchanged"; ties_detail.append(detail)
        elif a > b:
            detail["reason"] = "ranking_quality_improved"; wins_detail.append(detail)
        else:
            detail["reason"] = "ranking_quality_regressed"; losses_detail.append(detail)
        if current.get("top1_source") != baseline.get("top1_source"):
            changes.append({"scenario_id": row["scenario_id"],
                            "baseline_top1": baseline.get("top1_source"),
                            "candidate_top1": current.get("top1_source"),
                            "outcome": "win" if a > b else "loss" if a < b else "tie"})
    return {"wins": len(wins_detail), "losses": len(losses_detail), "ties": len(ties_detail),
            "not_comparable": len(not_comparable), "win_details": wins_detail,
            "loss_details": losses_detail, "tie_details": ties_detail,
            "not_comparable_details": not_comparable, "top1_changes": changes}


def _quality_tuple(evaluation: dict[str, Any]) -> tuple[float, float, float]:
    """Compare oracle acceptance first, then graded ranking quality."""
    acceptable = evaluation.get("acceptable_top1")
    return (0.5 if acceptable is None else float(acceptable),
            float(evaluation.get("ndcg3") or 0.0),
            float(evaluation.get("hit1") or 0.0))
