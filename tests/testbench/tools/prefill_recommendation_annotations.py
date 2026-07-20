"""Create a conservative Codex first-pass annotation bundle for human review."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_shadow import validate_annotations


STRONG_POSITIVE = {"music_played_through", "music_high_completion", "user_continue"}
WEAK_ENGAGEMENT = {"user_reply", "user_reply_fast"}
DELIVERY_ERROR = {"music_error"}


def _candidate_relevance(
    annotation: dict[str, Any], observation: dict[str, Any], events: list[dict[str, Any]],
    repeat_evidence: dict[str, dict[str, Any]],
    single_recovery: dict[str, Any],
) -> dict[str, int]:
    context = annotation["context_for_review"]
    candidates = list(context.get("candidates") or [])
    reason = str(context.get("reason") or "")
    event_types = {str(row.get("event_type") or "") for row in events}
    strong_candidate_ids = {
        str(row.get("candidate_id") or "") for row in events
        if row.get("event_type") in STRONG_POSITIVE and row.get("candidate_id")
    }
    top_score = float(candidates[0].get("score") or 0) if candidates else 0.0
    result: dict[str, int] = {}
    for rank, candidate in enumerate(candidates, 1):
        candidate_id = str(candidate.get("id") or "")
        source = str(candidate.get("source_type") or "")
        score = float(candidate.get("score") or 0)
        if source == "meme" and reason not in {"PASS_DUPLICATE", "DELIVERY_PREEMPTED"}:
            if candidate_id in strong_candidate_ids or (rank == 1 and event_types & STRONG_POSITIVE):
                value = 3
            elif rank == 1 and event_types & WEAK_ENGAGEMENT:
                value = 2
            else:
                value = 1
        elif candidate_id in strong_candidate_ids:
            value = 3
        elif reason == "PASS_DUPLICATE":
            value = 1 if rank == 1 else 0
        elif reason == "DELIVERY_PREEMPTED":
            value = 1 if rank == 1 else 0
        elif source == "vision" and not str(candidate.get("safe_summary") or "").strip():
            value = 1
        elif rank == 1:
            value = 2
        elif top_score - score <= 0.06:
            value = 2
        else:
            value = 1
        if event_types & DELIVERY_ERROR and candidate_id == str(observation.get("shadow_selected_candidate_id") or ""):
            value = min(value, 1)
        if rank == 1 and single_recovery.get("active") and not event_types & DELIVERY_ERROR:
            value = max(value, int(single_recovery["relevance"]))
        else:
            occurrence = int((repeat_evidence.get(candidate_id) or {}).get("occurrence") or 1)
            if occurrence == 2:
                value = min(value, 1)
            elif occurrence >= 3:
                value = 0
        result[candidate_id] = value
    return result


def _annotate(
    template_row: dict[str, Any], observation: dict[str, Any], events: list[dict[str, Any]],
    repeat_evidence: dict[str, dict[str, Any]],
    single_recovery: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    row = dict(template_row)
    context = row["context_for_review"]
    reason = str(context.get("reason") or "")
    activity = str(context.get("activity") or "unknown")
    source = str(context.get("top1") or "none")
    event_types = {str(item.get("event_type") or "") for item in events}
    relevance = _candidate_relevance(
        row, observation, events, repeat_evidence, single_recovery
    )
    top_id = next(iter(relevance), "")
    top_repeat = repeat_evidence.get(top_id) or {"occurrence": 1, "match": "none"}
    top_occurrence = int(top_repeat["occurrence"])

    if reason == "PASS_DUPLICATE":
        should_recommend = False
        interruption = "none"
        score_diagnosis = "reasonable"
        issue_layer = "none"
        confidence = "high"
        must_filter = [top_id] if top_id else []
        filter_reasons = {top_id: "duplicate"} if top_id else {}
        rationale = f"重复候选已被 dedup 正确拦截；当前 turn 不应再次投递（出现次数 {top_occurrence}）。"
    elif reason == "DELIVERY_PREEMPTED":
        should_recommend = False
        interruption = "none"
        score_diagnosis = "reasonable"
        issue_layer = "none"
        confidence = "high"
        must_filter = []
        filter_reasons = {}
        rationale = "投递被更高优先级交互抢占；当前时机不应主动打断。"
    elif event_types & DELIVERY_ERROR:
        should_recommend = True
        interruption = "acceptable" if activity == "idle" else "borderline"
        score_diagnosis = "not_enough_context"
        issue_layer = "data"
        confidence = "low"
        must_filter = []
        filter_reasons = {}
        rationale = "发生 music_error，只能确认投递链路失败，不能据此判断用户不喜欢内容。"
    elif single_recovery.get("active"):
        should_recommend = True
        interruption = "acceptable" if activity == "idle" else "borderline"
        score_diagnosis = "reasonable"
        issue_layer = "none"
        confidence = "medium"
        must_filter = []
        filter_reasons = {}
        suppressed = int(single_recovery["suppressed_before_delivery"])
        recovered_source = str(single_recovery["source_type"])
        rationale = ((f"单一 {recovered_source} 在上次投递后已有 {suppressed} 次未投递；"
                      if suppressed else f"本 turn 只有单一 {recovered_source} 候选且已实际投递；")
                     + f"恢复为有效 relevance {single_recovery['relevance']}，不再按重复项归零。")
    elif top_occurrence >= 2:
        should_recommend = False
        interruption = "borderline" if top_occurrence == 2 else "interruptive"
        score_diagnosis = "over_scored"
        issue_layer = "filter"
        confidence = "high" if top_repeat["match"] == "candidate_id" else "medium"
        must_filter = [top_id] if top_id else []
        filter_reasons = {top_id: "repeated_material_or_title"} if top_id else {}
        rationale = (f"Top-1 已是第 {top_occurrence} 次出现，依据 {top_repeat['match']} 命中重复；"
                     "主动重复搭话容易形成追问感，应降分并由 anti-repeat 过滤。")
    elif event_types & STRONG_POSITIVE:
        should_recommend = True
        interruption = "acceptable" if activity == "idle" else "borderline"
        score_diagnosis = "reasonable"
        issue_layer = "none"
        confidence = "high"
        must_filter = []
        filter_reasons = {}
        rationale = "存在播放完成、高完成率或继续互动等强正向行为证据。"
    elif source == "vision":
        should_recommend = False
        interruption = "acceptable" if activity == "idle" else "borderline"
        score_diagnosis = "not_enough_context"
        issue_layer = "data"
        confidence = "low"
        must_filter = []
        filter_reasons = {}
        rationale = "vision 仅保留 screen_context 类别且无语义摘要，无法复核实际相关性，保守标为不应推荐。"
    else:
        should_recommend = True
        interruption = "acceptable" if activity == "idle" else "borderline"
        score_diagnosis = "reasonable"
        issue_layer = "none"
        confidence = "medium" if event_types & WEAK_ENGAGEMENT else "low"
        must_filter = []
        filter_reasons = {}
        rationale = ("存在回复/快速回复等互动证据，但无法从脱敏日志判断回复倾向。"
                     if event_types & WEAK_ENGAGEMENT else
                     "候选标题与摘要可复核，但没有显式用户反馈；按保守中等相关性预标。")

    acceptable_sources = sorted({
        str(candidate.get("source_type") or "")
        for candidate in context.get("candidates") or []
        if relevance.get(str(candidate.get("id") or ""), 0) >= 2
    })
    row.update({
        "should_recommend": should_recommend,
        "acceptable_top1_sources": acceptable_sources,
        "relevance": relevance,
        "must_filter_candidate_ids": must_filter,
        "expected_filter_reasons": filter_reasons,
        "interruption_level": interruption,
        "privacy_risk": "none",
        "score_diagnosis": score_diagnosis,
        "issue_layer": issue_layer,
        "comment": f"[Codex first-pass/{confidence}] {rationale}",
        "annotator_id": "codex-first-pass-v1",
        "primary_review_status": "pending",
        "primary_reviewer_id": "",
        "primary_reviewed_at": "",
        "primary_abstain_reason": "",
        "second_review": {
            "required": False,
            "status": "not_required",
            "reviewer_id": "",
            "reviewed_at": "",
            "abstain_reason": "",
            "should_recommend": None,
            "relevance": {},
            "comment": "",
        },
        "reviewed": False,
        "reviewer_id": "",
        "codex_evidence": {
            "top1_repeat_occurrence": top_occurrence,
            "top1_repeat_match": top_repeat["match"],
            "candidate_repeat_evidence": repeat_evidence,
            "feedback_event_types": sorted(event_types),
            "single_candidate_recovery": single_recovery,
        },
    })
    return row, confidence


def _normalized_title(candidate: dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(candidate.get("safe_title") or "").strip().casefold())
    return f"{candidate.get('source_type') or 'unknown'}|{title}" if title else ""


def _repeat_evidence_for_observation(
    observation: dict[str, Any], exact_counts: Counter[str], title_counts: Counter[str],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    labels = list((observation.get("review_context") or {}).get("candidate_labels") or [])
    for candidate in labels:
        candidate_id = str(candidate.get("id") or "")
        if str(candidate.get("source_type") or "") == "meme":
            evidence[candidate_id] = {
                "occurrence": 1,
                "match": "meme_source_unreliable_default_unique",
                "normalized_title_key": "",
            }
            continue
        title_key = _normalized_title(candidate)
        exact_before = exact_counts[candidate_id] if candidate_id else 0
        title_before = title_counts[title_key] if title_key else 0
        occurrence = max(exact_before, title_before) + 1
        match = ("candidate_id" if exact_before and exact_before >= title_before else
                 "source_and_title" if title_before else "none")
        evidence[candidate_id] = {
            "occurrence": occurrence,
            "match": match,
            "normalized_title_key": title_key,
        }
        if candidate_id:
            exact_counts[candidate_id] += 1
        if title_key:
            title_counts[title_key] += 1
    return evidence


def _single_candidate_recovery_evidence(
    observation: dict[str, Any], state: dict[str, int], events: list[dict[str, Any]],
) -> dict[str, Any]:
    labels = list((observation.get("review_context") or {}).get("candidate_labels") or [])
    source_type = str(observation.get("shadow_selected_source_type") or "")
    is_single_source = (
        bool(source_type)
        and len(labels) == 1
        and str(labels[0].get("source_type") or "") == source_type
    )
    result = {
        "active": False,
        "source_type": source_type,
        "suppressed_before_delivery": state.get(source_type, 0),
        "relevance": 0,
    }
    if not is_single_source:
        return result
    event_types = {str(row.get("event_type") or "") for row in events}
    delivery_failed = bool(event_types & DELIVERY_ERROR)
    result["delivery_failed"] = delivery_failed
    if not bool(observation.get("delivered")) or delivery_failed:
        state[source_type] = state.get(source_type, 0) + 1
        result["suppressed_after_observation"] = state[source_type]
        return result
    suppressed = state.get(source_type, 0)
    score = float(observation.get("shadow_selected_score") or 0)
    result["active"] = True
    if suppressed >= 1 and score >= 0.4:
        result["relevance"] = 3
    elif suppressed >= 1:
        result["relevance"] = 2
    else:
        result["relevance"] = 1
    state[source_type] = 0
    result["suppressed_after_observation"] = 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("freeze", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blind-review-output", type=Path)
    args = parser.parse_args()
    template = json.loads(args.template.read_text(encoding="utf-8"))
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    observations = {str(row["turn_id"]): row for row in freeze.get("observations") or []}
    feedback_by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in freeze.get("feedback") or []:
        feedback_by_turn[str(row.get("turn_id") or "")].append(row)

    confidence_counts: Counter[str] = Counter()
    confidence_turn_ids: dict[str, list[str]] = defaultdict(list)
    exact_counts: Counter[str] = Counter()
    title_counts: Counter[str] = Counter()
    repeat_top1_turn_ids: list[str] = []
    single_recovery_turn_ids: list[str] = []
    recovery_state: dict[str, int] = {}
    annotations = []
    second_review_ids = set(
        (template.get("review_assignment") or {}).get("required_turn_ids") or []
    )
    for template_row in template.get("annotations") or []:
        turn_id = str(template_row.get("turn_id") or "")
        repeat_evidence = _repeat_evidence_for_observation(
            observations[turn_id], exact_counts, title_counts
        )
        turn_feedback = feedback_by_turn.get(turn_id, [])
        single_recovery = _single_candidate_recovery_evidence(
            observations[turn_id], recovery_state, turn_feedback
        )
        annotation, confidence = _annotate(
            template_row, observations[turn_id], turn_feedback,
            repeat_evidence, single_recovery,
        )
        if turn_id in second_review_ids:
            annotation["second_review"]["required"] = True
            annotation["second_review"]["status"] = "pending"
        annotations.append(annotation)
        confidence_counts[confidence] += 1
        confidence_turn_ids[confidence].append(turn_id)
        if int(annotation["codex_evidence"]["top1_repeat_occurrence"]) >= 2:
            repeat_top1_turn_ids.append(turn_id)
        if bool(annotation["codex_evidence"]["single_candidate_recovery"].get("active")):
            single_recovery_turn_ids.append(turn_id)
    validation = validate_annotations(freeze, annotations)
    if not validation["ok"]:
        raise ValueError(json.dumps(validation["errors"], ensure_ascii=False))

    output = dict(template)
    output["created_at"] = datetime.now(timezone.utc).isoformat()
    output["quality_preview"] = dict(freeze.get("quality_preview") or {})
    instructions = dict(output.get("instructions") or {})
    instructions.update({
        "causal_order": "resource candidates and scores exist before delivered text generation",
        "delivered_excerpt_usage": (
            "delivery-realization audit only; never use delivered text to infer candidate "
            "relevance, source preference, or a missing candidate"
        ),
        "relevance_evidence": (
            "candidate metadata, pre-delivery context, repeat/filter evidence, and explicit "
            "candidate-linked feedback only"
        ),
    })
    output["instructions"] = instructions
    output["annotation_provenance"] = {
        "method": "codex_assisted_first_pass",
        "policy_version": 6,
        "human_review_required": True,
        "semantic_limit": "safe review_context and explicit event metadata only; no private conversation or screen text",
        "causal_review_rule": (
            "candidate-first: delivered text is downstream evidence and cannot be used "
            "as the oracle for candidate relevance or source ranking"
        ),
        "confidence_distribution": dict(sorted(confidence_counts.items())),
        "repeat_policy": {
            "identity": "candidate_id OR normalized source_type + safe_title",
            "meme_exception": "meme defaults to unique because source IDs and placeholder titles are unreliable",
            "second_occurrence": "cap relevance at 1; repeated Top-1 should_recommend=false",
            "third_or_later": "relevance=0; repeated Top-1 interruption_level=interruptive",
        },
        "meme_policy": {
            "no_explicit_feedback": 1,
            "weak_reply_or_fast_reply": 2,
            "strong_continue_or_candidate_positive_event": 3,
            "diversity_note": "do not force Golden labels into a normal distribution; apply rolling exposure and consecutive-source penalties in ranking after human review",
        },
        "single_candidate_recovery_policy": {
            "scope": "single-candidate Top-1 for every source, counters isolated by source_type",
            "delivered_without_prior_suppression": "minimum relevance 1",
            "one_or_more_suppressed_before_delivery": "restore delivered relevance to at least 2",
            "one_or_more_suppressed_and_score_at_least_0_4": "restore delivered relevance to 3",
            "reset": "suppressed counter resets after an actual delivery",
        },
    }
    output["human_review_queue"] = {
        "priority_order": ["low", "medium", "high"],
        "low_confidence_turn_ids": sorted(confidence_turn_ids["low"]),
        "medium_confidence_turn_ids": sorted(confidence_turn_ids["medium"]),
        "high_confidence_turn_ids": sorted(confidence_turn_ids["high"]),
        "required_second_review_turn_ids": list(
            (template.get("review_assignment") or {}).get("required_turn_ids") or []
        ),
        "repeated_top1_turn_ids": sorted(repeat_top1_turn_ids),
        "meme_top1_turn_ids": sorted(
            str(row["turn_id"]) for row in annotations
            if str((row.get("context_for_review") or {}).get("top1") or "") == "meme"
        ),
        "single_candidate_recovery_turn_ids": sorted(single_recovery_turn_ids),
        "single_candidate_recovery_by_source": dict(sorted(Counter(
            str((row["codex_evidence"]["single_candidate_recovery"] or {}).get("source_type") or "unknown")
            for row in annotations
            if (row["codex_evidence"]["single_candidate_recovery"] or {}).get("active")
        ).items())),
    }
    output["annotations"] = annotations
    atomic_write_json(args.output, output)
    blind_review_output = None
    if args.blind_review_output:
        blind_rows = []
        for annotation in annotations:
            second_review = annotation.get("second_review") or {}
            if not second_review.get("required"):
                continue
            blind_rows.append({
                "turn_id": annotation["turn_id"],
                "context_for_review": annotation.get("context_for_review") or {},
                "second_review": second_review,
            })
        blind_bundle = {
            "schema_version": 1,
            "kind": "recommendation_blind_second_review",
            "source_dataset": output.get("source_dataset"),
            "source_sha256": output.get("source_sha256"),
            "instructions": {
                "should_recommend": "boolean",
                "relevance": "integer 0-3 for every candidate",
                "reviewer_id": "required",
                "reviewed_at": "required ISO-8601 timestamp with timezone",
                "status": "completed after filling labels, or abstained when evidence is insufficient",
                "abstain_reason": "required when status is abstained",
                "blindness": "first-pass and primary-review labels are intentionally omitted",
                "causal_order": "resource candidates and scores exist before delivered text generation",
                "delivered_excerpt_usage": (
                    "delivery-realization audit only; never use delivered text to infer "
                    "candidate relevance, source preference, or a missing candidate"
                ),
            },
            "reviews": blind_rows,
        }
        atomic_write_json(args.blind_review_output, blind_bundle)
        blind_review_output = str(args.blind_review_output.resolve())
    print(json.dumps({
        "output": str(args.output.resolve()),
        "annotation_count": len(annotations),
        "validation_ok": validation["ok"],
        "confidence_distribution": dict(sorted(confidence_counts.items())),
        "should_recommend_distribution": dict(Counter(
            str(row["should_recommend"]).lower() for row in annotations
        )),
        "reviewed_count": sum(row["reviewed"] is True for row in annotations),
        "repeated_top1_count": len(repeat_top1_turn_ids),
        "single_candidate_recovery_count": len(single_recovery_turn_ids),
        "blind_review_output": blind_review_output,
        "blind_review_count": len(second_review_ids),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
