"""Apply and optionally confirm a structured recommendation review batch."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_review_batch import apply_review_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--primary-reviewer-id", default="")
    parser.add_argument("--primary-reviewed-at", default="")
    args = parser.parse_args()
    workbook_path = args.workbook.resolve()
    batch_path = args.batch.resolve()
    workbook = json.loads(workbook_path.read_text(encoding="utf-8"))
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    output, summary = apply_review_batch(
        workbook,
        batch,
        confirm=args.confirm,
        primary_reviewer_id=args.primary_reviewer_id,
        primary_reviewed_at=args.primary_reviewed_at,
    )
    output_path = (args.output or workbook_path).resolve()
    atomic_write_json(output_path, output)
    print(json.dumps({
        "output": str(output_path),
        "summary": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
