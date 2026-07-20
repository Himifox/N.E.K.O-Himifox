"""Expand a source-keyed recommendation review seed into a full batch."""
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
    apply_review_batch,
    expand_review_seed,
)


def _render_markdown(batch: dict) -> str:
    lines = [
        f"# {batch['batch_id']}（待确认）",
        "",
        "本批只使用推荐决策前上下文和候选内容。生成后文本与后续反馈不用于倒推候选相关性。",
        "",
        "| # | Turn ID | Gate | 来源分数 | 诊断 | 置信度 |",
        "|---:|---|---:|---|---|---|",
    ]
    for index, item in enumerate(batch["items"], 1):
        fields = item["fields"]
        scores = " / ".join(
            f"{candidate_id.split(':', 1)[0]} {score}"
            for candidate_id, score in fields["relevance"].items()
        )
        diagnosis = f"{fields['score_diagnosis']} / {fields['issue_layer']}"
        lines.append(
            f"| {index} | `{item['turn_id']}` | "
            f"{str(fields['should_recommend']).lower()} | {scores} | "
            f"{diagnosis} | {item['confidence']} |"
        )
    lines.extend(["", "## 逐条理由", ""])
    for index, item in enumerate(batch["items"], 1):
        lines.append(f"{index}. `{item['turn_id']}`：{item['fields']['comment']}")
        if item["fields"]["must_filter_candidate_ids"]:
            filtered = ", ".join(item["fields"]["must_filter_candidate_ids"])
            lines.append(f"   - 必须过滤：`{filtered}`")
    lines.extend([
        "",
        "确认前，本批不会改变正式工作簿的 `primary_review_status` 或指标。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("seed", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    workbook = json.loads(args.workbook.resolve().read_text(encoding="utf-8"))
    seed = json.loads(args.seed.resolve().read_text(encoding="utf-8"))
    batch = expand_review_seed(workbook, seed)
    _, summary = apply_review_batch(workbook, batch)
    output = args.output.resolve()
    atomic_write_json(output, batch)
    markdown = args.markdown.resolve() if args.markdown else output.with_suffix(".md")
    atomic_write_bytes(markdown, _render_markdown(batch).encode("utf-8"))
    print(json.dumps({
        "output": str(output),
        "markdown": str(markdown),
        "summary": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
