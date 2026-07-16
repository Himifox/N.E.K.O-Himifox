"""Freeze an annotation-ready Shadow cohort and prepare human review artifacts."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main_logic.proactive_recommendation_feedback import (
    FEEDBACK_LOG_FILENAME,
    load_recommendation_feedback_jsonl,
)
from main_logic.proactive_recommendation_observer import (
    OBSERVATION_LOG_FILENAME,
    load_recommendation_observations_jsonl,
    validate_recommendation_review_context,
)
from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_shadow import audit_shadow_dataset


def _first_gate_cutoff(
    ready: list[dict[str, Any]], feedback: list[dict[str, Any]],
    minimum_observations: int, minimum_feedback_turns: int,
) -> float:
    observation_times = sorted(float(row["ts"]) for row in ready)
    if len(observation_times) < minimum_observations:
        raise ValueError(f"annotation-ready observations below {minimum_observations}")
    ready_turns = {str(row["turn_id"]) for row in ready}
    first_feedback_by_turn: dict[str, float] = {}
    for row in feedback:
        turn_id = str(row.get("turn_id") or "")
        if not turn_id or turn_id not in ready_turns or not isinstance(row.get("ts"), (int, float)):
            continue
        first_feedback_by_turn[turn_id] = min(
            float(row["ts"]), first_feedback_by_turn.get(turn_id, float("inf"))
        )
    feedback_times = sorted(first_feedback_by_turn.values())
    if len(feedback_times) < minimum_feedback_turns:
        raise ValueError(f"explicit joined feedback turns below {minimum_feedback_turns}")
    return max(
        observation_times[minimum_observations - 1],
        feedback_times[minimum_feedback_turns - 1],
    )


def _annotation_row(observation: dict[str, Any]) -> dict[str, Any]:
    context = observation["review_context"]
    labels = context.get("candidate_labels") or []
    return {
        "turn_id": observation["turn_id"],
        "should_recommend": None,
        "acceptable_top1_sources": [],
        "relevance": {str(row["id"]): None for row in labels},
        "must_filter_candidate_ids": [],
        "expected_filter_reasons": {},
        "interruption_level": None,
        "privacy_risk": None,
        "score_diagnosis": None,
        "issue_layer": None,
        "comment": "",
        "annotator_id": "",
        "reviewed": False,
        "reviewer_id": "",
        "context_for_review": {
            "activity": context.get("activity_state"),
            "top1": observation.get("shadow_selected_source_type"),
            "top1_score": observation.get("shadow_selected_score"),
            "delivered": observation.get("delivered"),
            "reason": observation.get("actual_reason_code"),
            "delivered_excerpt": context.get("delivered_excerpt", ""),
            "candidates": labels,
            "redaction_notes": context.get("redaction_notes") or [],
        },
    }


def _review_sample(observations: list[dict[str, Any]], rate: float) -> list[str]:
    target = math.ceil(len(observations) * rate)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        groups[(str(row.get("shadow_selected_source_type") or "none"),
                str(row.get("activity_state") or "unknown"))].append(row)
    selected: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda row: (float(row.get("ts") or 0), str(row["turn_id"])))
        quota = max(1, round(target * len(group) / len(observations)))
        selected.extend(group[:quota])
    selected_ids = {str(row["turn_id"]) for row in selected}
    if len(selected_ids) < target:
        for row in sorted(observations, key=lambda item: (float(item.get("ts") or 0), str(item["turn_id"]))):
            selected_ids.add(str(row["turn_id"]))
            if len(selected_ids) >= target:
                break
    if len(selected_ids) > target:
        selected_ids = set(sorted(selected_ids)[:target])
    return sorted(selected_ids)


def _markdown(audit: dict[str, Any]) -> str:
    q = audit["quality"]
    lines = [
        "# Recommendation P44-E Golden Cohort 最终审计",
        "",
        f"- 生成时间：`{audit['created_at']}`",
        f"- Freeze：`{audit['freeze_filename']}`",
        f"- SHA-256：`{audit['freeze_sha256']}`",
        f"- 固定截点：`{audit['cutoff_iso']}` (`{audit['cutoff_ts']}`)",
        f"- 算法版本：`{', '.join(q['algorithm_versions'])}`",
        "",
        "## 正式门禁",
        "",
        f"- Annotation-ready observation：**{q['observation_count']}** / 100，通过。",
        f"- 显式 joined feedback turn：**{q['feedback_joined_count']}** / 30，通过。",
        f"- Tuning：`{audit['tuning_mode']}`，必须保持关闭。",
        "",
        "## 分布",
        "",
        f"- 来源：`{json.dumps(q['source_distribution'], ensure_ascii=False, sort_keys=True)}`",
        f"- Activity：`{json.dumps(q['activity_distribution'], ensure_ascii=False, sort_keys=True)}`",
        f"- Feedback 事件：`{json.dumps(audit['feedback_event_distribution'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## 契约与隐私",
        "",
        f"- review_context 安全校验失败：**{q['review_context_invalid_count']}**",
        f"- 缺失 observation turn_id：**{len(q['invalid_observation_indexes'])}**",
        f"- 重复 turn_id：**{len(q['duplicate_turn_ids'])}**",
        f"- Candidate ID 对齐：**{'通过' if q['review_context_invalid_count'] == 0 else '失败'}**",
        f"- Orphan feedback：**{len(q['feedback_orphan_turn_ids'])}**（最终 cohort 已排除无关联记录）",
        f"- Mixed algorithm version：**{q['mixed_algorithm_versions']}**",
        "",
        "## 人工标注与复核",
        "",
        f"- 第二轮模板：`{audit['annotation_filename']}`，共 **{q['observation_count']}** 条待标注。",
        f"- 双人复核抽样：**{audit['review_sample_count']}** 条（{audit['review_sample_rate']:.2%}），已在模板的 `review_assignment.required_turn_ids` 固定。",
        "- 当前只完成了结构与隐私复核；语义 relevance、是否打扰和分数诊断仍需人工填写，不能由程序凭空生成。",
        "- 所有语义标注完成且抽样项由第二位 reviewer 复核后，才允许生成离线权重候选；生产 tuning 继续保持 `off`。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", default="shadow-p44e-golden-final")
    parser.add_argument("--review-rate", type=float, default=0.2)
    parser.add_argument("--tuning-mode", default="off")
    args = parser.parse_args()

    observations = load_recommendation_observations_jsonl(
        args.config_dir / OBSERVATION_LOG_FILENAME, limit=100_000
    )
    feedback = load_recommendation_feedback_jsonl(
        args.config_dir / FEEDBACK_LOG_FILENAME, limit=400_000
    )
    ready = [row for row in observations
             if validate_recommendation_review_context(row).get("annotation_ready")]
    cutoff = _first_gate_cutoff(ready, feedback, 100, 30)
    frozen_observations = [row for row in ready if float(row.get("ts") or 0) <= cutoff]
    frozen_turns = {str(row["turn_id"]) for row in frozen_observations}
    frozen_feedback = [row for row in feedback
                       if str(row.get("turn_id") or "") in frozen_turns
                       and float(row.get("ts") or 0) <= cutoff]
    created_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    freeze_path = args.output_dir / f"{args.prefix}-{stamp}.json"
    template_path = args.output_dir / f"{args.prefix}-{stamp}-annotation-template.json"
    audit_json_path = args.output_dir / f"{args.prefix}-{stamp}-audit.json"
    audit_md_path = args.output_dir / f"{args.prefix}-{stamp}-audit.md"

    freeze = {
        "schema_version": 1,
        "name": f"p44e-golden-{stamp}",
        "kind": "shadow_golden_cohort",
        "created_at": created_at,
        "cutoff_ts": cutoff,
        "observations": frozen_observations,
        "feedback": frozen_feedback,
        "source": {
            "config_dir": str(args.config_dir.resolve()),
            "observation_filename": OBSERVATION_LOG_FILENAME,
            "feedback_filename": FEEDBACK_LOG_FILENAME,
            "selection": "annotation_ready observations and explicitly joined feedback through first 100/30 gate cutoff",
        },
    }
    freeze["quality_preview"] = audit_shadow_dataset(freeze)
    atomic_write_json(freeze_path, freeze)
    digest = hashlib.sha256(freeze_path.read_bytes()).hexdigest().upper()

    review_ids = _review_sample(frozen_observations, args.review_rate)
    template = {
        "schema_version": 1,
        "source_dataset": freeze_path.name,
        "source_sha256": digest,
        "instructions": {
            "should_recommend": "boolean",
            "relevance": "integer 0-3 for every candidate",
            "interruption_level": "acceptable | borderline | interruptive | none",
            "privacy_risk": "none | low | medium | high",
            "score_diagnosis": "missing_candidate | not_enough_context | over_scored | reasonable | under_scored | wrong_source",
            "issue_layer": "candidate | filter | score | bias | data | none",
        },
        "review_assignment": {
            "minimum_rate": args.review_rate,
            "required_count": len(review_ids),
            "required_turn_ids": review_ids,
            "status": "pending_human_annotation_and_second_review",
        },
        "annotations": [_annotation_row(row) for row in frozen_observations],
    }
    atomic_write_json(template_path, template)

    quality = freeze["quality_preview"]
    quality["review_context_invalid_count"] = sum(
        not validate_recommendation_review_context(row).get("annotation_ready")
        for row in frozen_observations
    )
    audit = {
        "schema_version": 1,
        "created_at": created_at,
        "freeze_filename": freeze_path.name,
        "freeze_sha256": digest,
        "annotation_filename": template_path.name,
        "cutoff_ts": cutoff,
        "cutoff_iso": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
        "tuning_mode": args.tuning_mode,
        "quality": quality,
        "feedback_event_distribution": dict(sorted(Counter(
            str(row.get("event_type") or "unknown") for row in frozen_feedback
        ).items())),
        "review_sample_rate": args.review_rate,
        "review_sample_count": len(review_ids),
        "review_sample_turn_ids": review_ids,
        "semantic_annotation_status": "pending",
    }
    atomic_write_json(audit_json_path, audit)
    atomic_write_bytes(audit_md_path, _markdown(audit).encode("utf-8"))
    print(json.dumps({
        "freeze": str(freeze_path), "sha256": digest,
        "annotation_template": str(template_path),
        "audit_json": str(audit_json_path), "audit_markdown": str(audit_md_path),
        "observation_count": quality["observation_count"],
        "feedback_joined_count": quality["feedback_joined_count"],
        "review_sample_count": len(review_ids),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
