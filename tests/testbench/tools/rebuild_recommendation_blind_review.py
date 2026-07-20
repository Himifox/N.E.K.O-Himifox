"""Rebuild the fixed second-review sample with causal recovered context."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_review_batch import (
    build_context_recovered_blind_bundle,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("fixed_blind_bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    workbook = json.loads(args.workbook.resolve().read_text(encoding="utf-8"))
    fixed = json.loads(
        args.fixed_blind_bundle.resolve().read_text(encoding="utf-8")
    )
    fixed_turn_ids = [
        str(review.get("turn_id") or "")
        for review in fixed.get("reviews") or []
    ]
    bundle = build_context_recovered_blind_bundle(workbook, fixed_turn_ids)
    output = args.output.resolve()
    atomic_write_json(output, bundle)
    print(json.dumps({
        "output": str(output),
        "review_count": len(bundle["reviews"]),
        "turn_ids_sha256": bundle["selection"]["turn_ids_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
