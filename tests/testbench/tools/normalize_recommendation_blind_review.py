"""Normalize a hand-edited blind review and emit an unresolved checklist."""
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
    apply_blind_second_review_corrections,
    normalize_blind_second_reviews,
    reposition_blind_second_reviews,
)


def _render_markdown(audit: dict) -> str:
    lines = [
        "# 盲二审填写检查",
        "",
        f"- 有效完成：{audit['completed_count']}",
        f"- 有效弃权：{audit['abstained_count']}",
        f"- 仍需修正：{audit['unresolved_count']}",
        "",
        "## 仍需人工修正",
        "",
    ]
    for row in audit["rows"]:
        if row["state"] != "needs_human_correction":
            continue
        candidate_lines = [
            (
                f"  - `{candidate['id']}` · {candidate['source_type']} · "
                f"{candidate['safe_title'] or '（无标题）'}"
            )
            for candidate in row["candidates"]
        ]
        lines.extend([
            f"### {row['index']}. `{row['turn_id']}`",
            "",
            "- 实际候选：",
            *candidate_lines,
            f"- 已填 should_recommend：`{row['raw_should_recommend']}`",
            f"- 已填 relevance：`{row['raw_relevance']}`",
            f"- 缺少评分：`{row['missing_candidate_ids']}`",
            f"- 不属于本条的键：`{row['dropped_unknown_keys']}`",
            f"- 问题：`{row['issues']}`",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--audit-json", type=Path, required=True)
    parser.add_argument("--audit-markdown", type=Path, required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--completed-at", required=True)
    parser.add_argument(
        "--move",
        action="append",
        default=[],
        metavar="SOURCE:TARGET",
        help="Move a complete review payload between 1-based row indexes.",
    )
    parser.add_argument(
        "--swap",
        action="append",
        default=[],
        metavar="LEFT:RIGHT",
        help="Swap complete review payloads between 1-based row indexes.",
    )
    parser.add_argument(
        "--corrections",
        type=Path,
        help="JSON file containing explicit missing-field corrections.",
    )
    args = parser.parse_args()
    bundle = json.loads(args.input.resolve().read_text(encoding="utf-8"))
    parse_pair = lambda value: tuple(int(part) for part in value.split(":", 1))
    moves = [parse_pair(value) for value in args.move]
    swaps = [parse_pair(value) for value in args.swap]
    if moves or swaps:
        bundle, _ = reposition_blind_second_reviews(
            bundle,
            moves=moves,
            swaps=swaps,
        )
    if args.corrections:
        correction_payload = json.loads(
            args.corrections.resolve().read_text(encoding="utf-8")
        )
        bundle, _ = apply_blind_second_review_corrections(
            bundle,
            list(correction_payload.get("corrections") or []),
        )
    normalized, audit = normalize_blind_second_reviews(
        bundle,
        default_reviewer_id=args.reviewer_id,
        completed_at=args.completed_at,
    )
    atomic_write_json(args.output.resolve(), normalized)
    atomic_write_json(args.audit_json.resolve(), audit)
    atomic_write_bytes(
        args.audit_markdown.resolve(),
        _render_markdown(audit).encode("utf-8"),
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "audit_json": str(args.audit_json.resolve()),
        "audit_markdown": str(args.audit_markdown.resolve()),
        "normalized_count": audit["normalized_count"],
        "completed_count": audit["completed_count"],
        "abstained_count": audit["abstained_count"],
        "unresolved_count": audit["unresolved_count"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
