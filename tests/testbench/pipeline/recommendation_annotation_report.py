"""Transparent metrics for a frozen human-annotated Shadow review bundle."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from typing import Any


def build_annotation_report(bundle: dict[str, Any]) -> dict[str, Any]:
    annotations = list(bundle.get("annotations") or [])
    feedback_joined_count = (bundle.get("quality_preview") or {}).get("feedback_joined_count")
    scenarios: list[dict[str, Any]] = []
    source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    hits = 0
    positive_case_count = 0
    acceptable_hits = 0
    acceptable_count = 0
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    top1_sources: Counter[str] = Counter()
    gate = Counter()
    metric_scenario_count = 0

    for annotation in annotations:
        primary_status = str(annotation.get("primary_review_status") or "pending")
        adjudication_status = str(annotation.get("adjudication_status") or "")
        use_adjudicated = adjudication_status in {
            "completed",
            "retain_primary_low_confidence",
            "retain_primary_minor_difference",
        }
        metric_eligible = (
            primary_status != "abstained"
            and adjudication_status != "excluded_abstention"
        )
        if metric_eligible:
            metric_scenario_count += 1
        context = annotation.get("context_for_review") or {}
        candidates = list(context.get("candidates") or [])
        relevance = dict(
            annotation.get("adjudicated_relevance")
            if use_adjudicated
            else annotation.get("relevance")
            or {}
        )
        ranked = []
        best = max((int(value) for value in relevance.values()), default=None)
        first_relevant_rank = None
        for rank, candidate in enumerate(candidates, 1):
            candidate_id = str(candidate.get("id") or "")
            source = str(candidate.get("source_type") or "unknown")
            human = relevance.get(candidate_id)
            row = {
                "rank": rank,
                "candidate_id": candidate_id,
                "resource": source,
                "title": candidate.get("safe_title") or "",
                "production_score": candidate.get("score"),
                "human_relevance": human,
            }
            ranked.append(row)
            if metric_eligible:
                source_rows[source].append(row)
            if first_relevant_rank is None and isinstance(human, int) and human > 0:
                first_relevant_rank = rank
        top1 = ranked[0] if ranked else None
        if top1 and metric_eligible:
            top1_sources[top1["resource"]] += 1
        should_recommend = (
            annotation.get("adjudicated_should_recommend")
            if use_adjudicated
            else annotation.get("should_recommend")
        )
        realization = annotation.get("realization_review_context") or {}
        delivered = (
            context.get("delivered")
            if isinstance(context.get("delivered"), bool)
            else realization.get("delivered")
        )
        delivery_reason = context.get("reason") or realization.get("reason")
        predicted_recommend = delivered if isinstance(delivered, bool) else None
        if metric_eligible and isinstance(should_recommend, bool) and isinstance(predicted_recommend, bool):
            gate[(predicted_recommend, should_recommend)] += 1
        positive_case_eligible = (
            metric_eligible and should_recommend is True and best is not None and best > 0
        )
        hit = (bool(top1 and top1["human_relevance"] == best)
               if positive_case_eligible else None)
        ndcg = None
        reciprocal_rank = None
        acceptable_top1 = None
        if positive_case_eligible:
            positive_case_count += 1
            hits += int(bool(hit))
            reciprocal_rank = 1.0 / first_relevant_rank if first_relevant_rank else 0.0
            reciprocal_ranks.append(reciprocal_rank)
            dcg = sum((2 ** int(row["human_relevance"] or 0) - 1) / math.log2(row["rank"] + 1)
                      for row in ranked[:3])
            ideal = sorted((int(value) for value in relevance.values()), reverse=True)[:3]
            idcg = sum((2 ** value - 1) / math.log2(rank + 1) for rank, value in enumerate(ideal, 1))
            ndcg = dcg / idcg if idcg else None
            if ndcg is not None:
                ndcgs.append(ndcg)
            acceptable_sources = set(annotation.get("acceptable_top1_sources") or [])
            if acceptable_sources:
                acceptable_count += 1
                acceptable_top1 = bool(top1 and top1["resource"] in acceptable_sources)
                acceptable_hits += int(acceptable_top1)
        scenarios.append({
            "turn_id": annotation.get("turn_id"),
            "activity": context.get("activity"),
            "should_recommend": annotation.get("should_recommend"),
            "delivered": delivered,
            "delivery_reason": delivery_reason,
            "predicted_recommend": predicted_recommend,
            "primary_review_status": primary_status,
            "adjudication_status": adjudication_status or None,
            "label_source": "adjudicated" if use_adjudicated else "primary",
            "metric_eligible": metric_eligible,
            "positive_case_eligible": positive_case_eligible,
            "top1_hit": hit,
            "acceptable_top1": acceptable_top1,
            "reciprocal_rank": round(reciprocal_rank, 4) if reciprocal_rank is not None else None,
            "ndcg_at_3": round(ndcg, 4) if ndcg is not None else None,
            "score_diagnosis": annotation.get("score_diagnosis"),
            "issue_layer": annotation.get("issue_layer"),
            "resources": ranked,
        })

    source_summary = []
    scenario_count = len(scenarios)
    for source, rows in sorted(source_rows.items()):
        scores = [float(row["production_score"]) for row in rows
                  if isinstance(row.get("production_score"), (int, float))]
        labels = [int(row["human_relevance"]) for row in rows
                  if isinstance(row.get("human_relevance"), int)]
        mean_score = sum(scores) / len(scores) if scores else None
        mean_relevance = sum(labels) / len(labels) if labels else None
        normalized_relevance = mean_relevance / 3.0 if mean_relevance is not None else None
        pressure = normalized_relevance - mean_score if normalized_relevance is not None and mean_score is not None else None
        source_summary.append({
            "resource": source,
            "candidate_count": len(rows),
            "top1_count": top1_sources[source],
            "top1_exposure": round(top1_sources[source] / metric_scenario_count, 4)
                             if metric_scenario_count else 0.0,
            "average_production_score": round(mean_score, 4) if mean_score is not None else None,
            "average_human_relevance": round(mean_relevance, 4) if mean_relevance is not None else None,
            "normalized_human_relevance": round(normalized_relevance, 4) if normalized_relevance is not None else None,
            "diagnostic_weight_pressure": round(pressure, 4) if pressure is not None else None,
            "pressure_direction": "increase" if pressure is not None and pressure > 0.05 else
                                  "decrease" if pressure is not None and pressure < -0.05 else "hold",
        })

    human_confirmed = sum(
        annotation.get("primary_review_status") in {"accepted", "corrected"}
        and bool(annotation.get("primary_reviewer_id"))
        and bool(annotation.get("primary_reviewed_at")) for annotation in annotations
    )
    primary_abstained = sum(
        annotation.get("primary_review_status") == "abstained"
        and bool(annotation.get("primary_abstain_reason"))
        and bool(annotation.get("primary_reviewer_id"))
        and bool(annotation.get("primary_reviewed_at")) for annotation in annotations
    )
    primary_handled = human_confirmed + primary_abstained
    adjudication_status_distribution = dict(Counter(
        annotation.get("adjudication_status") or "not_adjudicated"
        for annotation in annotations
    ))
    second_required = [annotation for annotation in annotations
                       if (annotation.get("second_review") or {}).get("required")]
    second_completed = sum((annotation.get("second_review") or {}).get("status") in {"completed", "abstained"}
                           and bool((annotation.get("second_review") or {}).get("reviewed_at"))
                           for annotation in second_required)
    tp = gate[(True, True)]; fp = gate[(True, False)]
    tn = gate[(False, False)]; fn = gate[(False, True)]
    gate_count = tp + fp + tn + fn
    def metric(numerator: int | float, denominator: int) -> dict[str, Any]:
        return {"numerator": numerator, "denominator": denominator,
                "value": round(numerator / denominator, 4) if denominator else None}

    blockers = []
    if scenario_count < 100:
        blockers.append("observation_count_below_100")
    if not isinstance(feedback_joined_count, int) or feedback_joined_count < 30:
        blockers.append("feedback_joined_count_not_available_or_below_30")
    if primary_handled < scenario_count:
        blockers.append("primary_human_review_incomplete")
    if len(second_required) < math.ceil(scenario_count * 0.2):
        blockers.append("second_review_sample_below_20_percent")
    elif second_completed < len(second_required):
        blockers.append("second_review_incomplete")
    return {
        "schema_version": 1,
        "source_dataset": bundle.get("source_dataset"),
        "summary": {
            "scenario_count": scenario_count,
            "metric_eligible_count": metric_scenario_count,
            "metric_excluded_abstained_count": (
                scenario_count - metric_scenario_count
            ),
            "candidate_count": sum(len(row["resources"]) for row in scenarios),
            "gate_eligible_count": gate_count,
            "positive_case_count": positive_case_count,
            "decision_accuracy_with_noop": metric(tp + tn, gate_count),
            "gate_precision": metric(tp, tp + fp),
            "gate_recall": metric(tp, tp + fn),
            "gate_f1": metric(2 * tp, 2 * tp + fp + fn),
            "false_interruption_rate": metric(fp, fp + tn),
            "missed_opportunity_rate": metric(fn, fn + tp),
            "gate_confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
            "positive_case_hit_at_1": metric(hits, positive_case_count),
            "positive_case_mrr": metric(round(sum(reciprocal_ranks), 4), len(reciprocal_ranks)),
            "positive_case_ndcg_at_3": metric(round(sum(ndcgs), 4), len(ndcgs)),
            "positive_case_acceptable_top1": metric(acceptable_hits, acceptable_count),
            "hit_at_1": round(hits / positive_case_count, 4) if positive_case_count else None,
            "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else None,
            "ndcg_at_3": round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else None,
            "should_recommend_rate": round(
                sum(row["metric_eligible"] and row["should_recommend"] is True for row in scenarios)
                / metric_scenario_count, 4
            ) if metric_scenario_count else None,
            "human_confirmed_count": human_confirmed,
            "primary_abstained_count": primary_abstained,
            "primary_handled_count": primary_handled,
            "second_review_required_count": len(second_required),
            "second_review_completed_count": second_completed,
            "adjudication_status_distribution": adjudication_status_distribution,
            "feedback_joined_count": feedback_joined_count,
            "diagnosis_distribution": dict(Counter(row["score_diagnosis"] for row in scenarios)),
            "issue_layer_distribution": dict(Counter(row["issue_layer"] for row in scenarios)),
        },
        "source_summary": source_summary,
        "scenarios": scenarios,
        "weight_candidate_gate": {
            "ready": not blockers,
            "blockers": blockers,
            "production_config_modified": False,
            "note": "Weight pressure is diagnostic only; it is not an automatic tuning delta.",
        },
    }


def build_annotation_report_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    decision = summary["decision_accuracy_with_noop"]
    interruption = summary["false_interruption_rate"]
    missed = summary["missed_opportunity_rate"]
    hit = summary["positive_case_hit_at_1"]
    ndcg = summary["positive_case_ndcg_at_3"]
    lines = ["# Recommendation Shadow 人工标注分析", "",
             f"- 场景：{summary['scenario_count']}", f"- 候选：{summary['candidate_count']}",
             f"- 指标有效场景：{summary['metric_eligible_count']}",
             f"- 人工弃权排除：{summary['metric_excluded_abstained_count']}",
             f"- 显式 joined feedback turn：{summary['feedback_joined_count']}",
             f"- 人工主审处理：{summary['primary_handled_count']} / {summary['scenario_count']}"
             f"（确认 {summary['human_confirmed_count']}，弃权 {summary['primary_abstained_count']}）",
             f"- 二审完成：{summary['second_review_completed_count']} / {summary['second_review_required_count']}",
             f"- Decision accuracy (含 PASS)：{decision['value']} ({decision['numerator']}/{decision['denominator']})",
             f"- False interruption rate：{interruption['value']} ({interruption['numerator']}/{interruption['denominator']})",
             f"- Missed opportunity rate：{missed['value']} ({missed['numerator']}/{missed['denominator']})",
             f"- Positive-case Hit@1：{hit['value']} ({hit['numerator']}/{hit['denominator']})",
             f"- Positive-case MRR：{summary['mrr']}",
             f"- Positive-case nDCG@3：{ndcg['value']} ({ndcg['numerator']}/{ndcg['denominator']})",
             f"- 应推荐率：{summary['should_recommend_rate']}", "",
             "## 资源汇总", "",
             "| 资源 | 候选数 | Top-1曝光 | 平均生产分 | 平均人工分(0-3) | 权重压力 | 方向 |",
             "|---|---:|---:|---:|---:|---:|---|"]
    for row in report["source_summary"]:
        lines.append(f"| {row['resource']} | {row['candidate_count']} | {row['top1_exposure']} | "
                     f"{row['average_production_score']} | {row['average_human_relevance']} | "
                     f"{row['diagnostic_weight_pressure']} | {row['pressure_direction']} |")
    lines.extend(["", "## 逐场景资源分数", ""])
    for row in report["scenarios"]:
        lines.extend([f"### {row['turn_id']}", "",
                      f"Activity: `{row['activity']}` · Should recommend: `{row['should_recommend']}` · "
                      f"Delivered: `{row['predicted_recommend']}` · Metric eligible: `{row['metric_eligible']}` · "
                      f"Positive rank case: `{row['positive_case_eligible']}` · "
                      f"Top-1 hit: `{row['top1_hit']}` · nDCG@3: `{row['ndcg_at_3']}`", "",
                      "| 排名 | 资源 | 标题 | 生产分 | 人工分 |", "|---:|---|---|---:|---:|"])
        for resource in row["resources"]:
            title = str(resource["title"]).replace("|", "\\|")
            lines.append(f"| {resource['rank']} | {resource['resource']} | {title} | "
                         f"{resource['production_score']} | {resource['human_relevance']} |")
        lines.append("")
    gate = report["weight_candidate_gate"]
    lines.extend(["## 权重候选门禁", "", f"Ready: `{gate['ready']}`",
                  f"Blockers: `{', '.join(gate['blockers'])}`", "",
                  "> 当前只展示诊断权重压力；没有修改生产配置，也未生成可应用权重。", ""])
    return "\n".join(lines)
