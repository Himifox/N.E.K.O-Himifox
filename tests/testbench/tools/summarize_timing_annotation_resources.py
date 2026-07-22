"""Summarize source-level assistant-draft scores for a timing annotation manifest.

This report deliberately contains only blind-review candidate metadata and the
assistant's 0–3 comparison scores.  It does not read production rank, delivery,
feedback, or timing values.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json


def summarize(document: dict[str, Any]) -> dict[str, Any]:
    """Build a transparent aggregate of the draft's candidate comparison scores."""
    sources: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "candidate_count": 0,
        "score_counts": Counter(),
        "score_total": 0,
        "competitive_candidate_count": 0,
        "top_ranked_scenario_count": 0,
        "preferred_candidate_count": 0,
        "repeat_penalty_counts": Counter(),
    })
    vision_interruptibility: Counter[str] = Counter()
    proposal_counts: Counter[str] = Counter()
    scenario_count = 0

    for item in document.get("items") or []:
        proposal = dict(item.get("assistant_pre_annotation") or {})
        status = str(proposal.get("status") or "unknown")
        if status == "abstained":
            proposal_counts["abstained"] += 1
        elif proposal.get("should_recommend") is True:
            proposal_counts["proposed_true"] += 1
        else:
            proposal_counts["proposed_false"] += 1
        scenario_count += 1
        # Abstained rows lack enough independent review evidence.  Do not turn
        # their assistant placeholders into source-score evidence.
        if status == "abstained":
            continue
        candidates = list((item.get("context_for_blind_review") or {}).get("candidates") or [])
        relevance = dict(proposal.get("candidate_relevance") or {})
        repeats = dict(proposal.get("candidate_repeat_evidence") or {})
        preferred = set(proposal.get("preferred_candidate_ids") or [])
        scored = [(candidate, int(relevance.get(str(candidate.get("id") or ""), 0))) for candidate in candidates]
        top_score = max((score for _candidate, score in scored), default=None)
        top_sources = {
            str(candidate.get("source_type") or "unknown")
            for candidate, score in scored
            if top_score is not None and top_score >= 2 and score == top_score
        }
        for candidate, score in scored:
            candidate_id = str(candidate.get("id") or "")
            source = str(candidate.get("source_type") or "unknown")
            record = sources[source]
            record["candidate_count"] += 1
            record["score_counts"][str(score)] += 1
            record["score_total"] += score
            if score >= 2:
                record["competitive_candidate_count"] += 1
            if source in top_sources:
                record["top_ranked_scenario_count"] += 1
            if candidate_id in preferred:
                record["preferred_candidate_count"] += 1
            penalty = str((repeats.get(candidate_id) or {}).get("penalty") or "not_recorded")
            record["repeat_penalty_counts"][penalty] += 1
            if source == "vision":
                interruptibility = dict(proposal.get("interruptibility") or {})
                level = str(interruptibility.get("level") or "not_recorded")
                vision_interruptibility[f"{level}:score_{score}"] += 1

    normalized_sources: dict[str, Any] = {}
    for source, record in sorted(sources.items()):
        count = record["candidate_count"]
        normalized_sources[source] = {
            "candidate_count": count,
            "score_counts": {str(score): record["score_counts"].get(str(score), 0) for score in range(4)},
            "mean_competition_score": round(record["score_total"] / count, 4) if count else None,
            "competitive_candidate_count": record["competitive_candidate_count"],
            "top_ranked_scenario_count": record["top_ranked_scenario_count"],
            "preferred_candidate_count": record["preferred_candidate_count"],
            "repeat_penalty_counts": dict(sorted(record["repeat_penalty_counts"].items())),
        }
    return {
        "report_type": "timing_annotation_assistant_draft_resource_summary",
        "policy_version": (document.get("assistant_pre_annotation_provenance") or {}).get("policy_version"),
        "scenario_count": scenario_count,
        "scored_scenario_count": scenario_count - proposal_counts.get("abstained", 0),
        "proposal_counts": dict(proposal_counts),
        "sources": normalized_sources,
        "vision_interruptibility_distribution": dict(sorted(vision_interruptibility.items())),
        "scope": {
            "included": ["blind candidate metadata", "assistant 0-3 comparison scores", "repeat evidence", "vision interruptibility class"],
            "excluded": ["production scores/ranks", "delivery", "feedback", "timing values", "human review labels"],
            "not_a_product_evaluation": True,
        },
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# P44-F2-R0 资源候选评分总览（助手草案）",
        "",
        f"- Policy version: `{summary.get('policy_version')}`",
        f"- 场景数：`{summary['scenario_count']}`",
        f"- 纳入资源评分场景：`{summary['scored_scenario_count']}`（弃权行不计入资源分布）",
        f"- 建议推荐：`{summary['proposal_counts'].get('proposed_true', 0)}`；建议不推荐：`{summary['proposal_counts'].get('proposed_false', 0)}`；弃权：`{summary['proposal_counts'].get('abstained', 0)}`",
        "- 这是离线助手草案的候选比较分，不是生产权重、投递结果、反馈或产品效果结论。",
        "",
        "## 各资源最终竞争分",
        "",
        "| 资源 | 候选数 | 0 分 | 1 分 | 2 分 | 3 分 | 平均分 | ≥2 分候选 | ≥2 分时场景最高* | preferred 候选 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source, record in summary["sources"].items():
        scores = record["score_counts"]
        lines.append(
            f"| `{source}` | {record['candidate_count']} | {scores['0']} | {scores['1']} | "
            f"{scores['2']} | {scores['3']} | {record['mean_competition_score']:.2f} | "
            f"{record['competitive_candidate_count']} | {record['top_ranked_scenario_count']} | "
            f"{record['preferred_candidate_count']} |"
        )
    lines.extend([
        "",
        "* 同分并列时，各并列资源均记一次；它不是生产最终选择次数。",
        "",
        "## 重复证据",
        "",
        "| 资源 | none | minus_1 | meme_cap_1 | zero | 不适用 / 未记录 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    named_penalties = {"none", "minus_1", "meme_cap_1", "zero"}
    for source, record in summary["sources"].items():
        penalties = record["repeat_penalty_counts"]
        other = sum(count for key, count in penalties.items() if key not in named_penalties)
        lines.append(
            f"| `{source}` | {penalties.get('none', 0)} | {penalties.get('minus_1', 0)} | "
            f"{penalties.get('meme_cap_1', 0)} | {penalties.get('zero', 0)} | {other} |"
        )
    lines.extend([
        "",
        "## Vision：相关性与可打扰性",
        "",
        "Vision 的语义相关性固定为 3；下表是它进入与其它资源同一 0–3 标尺后的最终竞争分。",
        "",
        "| 可打扰性 : 最终分 | 候选数 |",
        "|---|---:|",
    ])
    for key, count in summary["vision_interruptibility_distribution"].items():
        lines.append(f"| `{key}` | {count} |")
    lines.extend([
        "",
        "## 解读边界",
        "",
        "- Vision 的 `screen_context` 是脱敏占位，不作为候选内容重复识别；相同因果 review context 的后续 Vision 行会在助手草案中弃权，不计入本表。",
        "- music/news/meme/video 的重复证据以 `source + safe_title + safe_summary` 识别，首现 `repeat #1 (none)` 不扣分。",
        "- 本报告用于人工复核草案；不得据此自动修改生产权重或 tuning。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    args = parser.parse_args()
    document = json.loads(args.draft.resolve().read_text(encoding="utf-8"))
    summary = summarize(document)
    atomic_write_json(args.json_output.resolve(), summary)
    atomic_write_bytes(args.markdown_output.resolve(), render_markdown(summary).encode("utf-8"))
    print(json.dumps({"scenario_count": summary["scenario_count"], "sources": list(summary["sources"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
