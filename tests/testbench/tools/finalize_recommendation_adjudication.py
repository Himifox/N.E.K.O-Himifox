"""Finalize confirmed P44-E2 decisions without replacing review history."""
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
    finalize_lightweight_adjudication,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("adjudication_bundle", type=Path)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("output_workbook", type=Path)
    parser.add_argument("output_adjudication_bundle", type=Path)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--adjudicator-id", required=True)
    parser.add_argument("--adjudicated-at", required=True)
    args = parser.parse_args()
    workbook = json.loads(args.workbook.resolve().read_text(encoding="utf-8"))
    bundle = json.loads(
        args.adjudication_bundle.resolve().read_text(encoding="utf-8")
    )
    decision_payload = json.loads(
        args.decisions.resolve().read_text(encoding="utf-8")
    )
    finalized_workbook, finalized_bundle, audit = (
        finalize_lightweight_adjudication(
            workbook,
            bundle,
            list(decision_payload.get("decisions") or []),
            adjudicator_id=args.adjudicator_id,
            adjudicated_at=args.adjudicated_at,
        )
    )
    atomic_write_json(args.output_workbook.resolve(), finalized_workbook)
    atomic_write_json(
        args.output_adjudication_bundle.resolve(),
        finalized_bundle,
    )
    atomic_write_json(args.audit.resolve(), audit)
    print(json.dumps({
        "output_workbook": str(args.output_workbook.resolve()),
        "output_adjudication_bundle": str(
            args.output_adjudication_bundle.resolve()
        ),
        "audit": str(args.audit.resolve()),
        "summary": audit,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
