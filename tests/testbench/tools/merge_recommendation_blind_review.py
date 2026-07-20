"""Merge completed blind second reviews into the primary human workbook."""
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
    merge_blind_second_reviews,
)


def _markdown(agreement: dict) -> str:
    gate = agreement["gate_exact_agreement"]
    exact = agreement["candidate_score_exact_agreement"]
    within = agreement["candidate_score_within_one"]
    top = agreement["top_relevance_set_agreement"]
    return "\n".join([
        "# Recommendation 盲二审一致性报告",
        "",
        f"- 必审：{agreement['handled_count']} / {agreement['required_count']}",
        f"- 完成：{agreement['completed_count']}",
        f"- 弃权：{agreement['abstained_count']}",
        f"- 双方均可计量：{agreement['jointly_eligible_count']}",
        f"- 是否推荐一致率：{gate['value']} ({gate['numerator']}/{gate['denominator']})",
        f"- 是否推荐 Cohen's kappa：{agreement['gate_cohen_kappa']}",
        f"- 候选分数完全一致率：{exact['value']} ({exact['numerator']}/{exact['denominator']})",
        f"- 候选分数相差不超过 1：{within['value']} ({within['numerator']}/{within['denominator']})",
        f"- 候选分数 MAE：{agreement['candidate_score_mae']}",
        f"- 最高相关候选集合一致率：{top['value']} ({top['numerator']}/{top['denominator']})",
        f"- 存在任意分歧的场景：{agreement['disagreement_count']}",
        "",
        "> 本报告只描述主审与盲二审的一致性，不自动修改生产权重或 tuning。",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("blind_review", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--agreement-json", type=Path, required=True)
    parser.add_argument("--agreement-markdown", type=Path, required=True)
    args = parser.parse_args()
    workbook = json.loads(args.workbook.resolve().read_text(encoding="utf-8"))
    blind = json.loads(args.blind_review.resolve().read_text(encoding="utf-8"))
    merged, agreement = merge_blind_second_reviews(workbook, blind)
    atomic_write_json(args.output.resolve(), merged)
    atomic_write_json(args.agreement_json.resolve(), agreement)
    atomic_write_bytes(
        args.agreement_markdown.resolve(),
        _markdown(agreement).encode("utf-8"),
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "agreement_json": str(args.agreement_json.resolve()),
        "agreement_markdown": str(args.agreement_markdown.resolve()),
        "summary": {
            key: value
            for key, value in agreement.items()
            if key not in {"disagreements"}
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
