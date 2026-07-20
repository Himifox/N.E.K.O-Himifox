"""Prepare the P44-E2 lightweight gate-conflict adjudication workbook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_review_batch import (
    build_lightweight_adjudication_bundle,
)


def _markdown(bundle: dict) -> str:
    counts = bundle["counts"]
    lines = [
        "# P44-E2 轻量分歧裁决工作簿",
        "",
        f"- A级人工裁决：{counts['A']}",
        f"- B级保留主审、低置信：{counts['B']}",
        f"- C级保留主审、轻微分歧：{counts['C']}",
        f"- 至少一方弃权、排除：{counts['excluded']}",
        "",
        "只填写下列 A 级场景。依次判断候选相关性、搭话时机、疲劳抑制和最终推荐。",
        "",
    ]
    for index, item in enumerate(
        (row for row in bundle["items"] if row["grade"] == "A"),
        1,
    ):
        primary = item["primary_review"]
        second = item["second_review"]
        pre_decision = (
            item["context_for_review"].get("pre_decision_context") or {}
        )
        lines.extend([
            f"## {index}. `{item['turn_id']}`",
            "",
            f"- Activity：`{item['context_for_review'].get('activity')}`",
            f"- 上下文可信度：`{pre_decision.get('temporal_confidence')}`；"
            f"距最近消息：`{pre_decision.get('latest_message_gap_seconds')}` 秒",
            f"- 主审：should=`{primary['should_recommend']}` relevance=`{primary['relevance']}`",
            f"- 二审：should=`{second['should_recommend']}` relevance=`{second['relevance']}`",
            f"- 主审备注：{primary['comment'] or '（无）'}",
            f"- 二审备注：{second['comment'] or '（无）'}",
            f"- 单候选恢复证据：`{item['single_candidate_recovery']}`",
            "- 推荐前对话：",
        ])
        for message in pre_decision.get("messages") or []:
            content = str(message.get("content") or "").replace("\n", " ")
            lines.append(
                f"  - `{message.get('timestamp')}` **{message.get('role')}**：{content}"
            )
        lines.extend([
            "- 候选：",
        ])
        for candidate in item["context_for_review"].get("candidates") or []:
            lines.append(
                f"  - `{candidate.get('id')}` · {candidate.get('source_type')} · "
                f"{candidate.get('safe_title') or '（无标题）'} · "
                f"生产分 `{candidate.get('score')}`"
            )
        lines.extend([
            "- 待填：candidate_relevance / timing_ok / fatigue_suppressed / "
            "should_recommend / reason_code / comment",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()
    workbook = json.loads(args.input.resolve().read_text(encoding="utf-8"))
    bundle = build_lightweight_adjudication_bundle(workbook)
    atomic_write_json(args.output.resolve(), bundle)
    atomic_write_bytes(args.markdown.resolve(), _markdown(bundle).encode("utf-8"))
    print(json.dumps({
        "output": str(args.output.resolve()),
        "markdown": str(args.markdown.resolve()),
        "counts": bundle["counts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
