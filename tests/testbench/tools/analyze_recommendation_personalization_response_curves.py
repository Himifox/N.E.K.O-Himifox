"""Generate the registered P44-G1-R2 response-curve reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_personalization_response_curve import (
    analyze_personalization_response_curves,
    render_personalization_response_curves_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--as-of", type=float)
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = analyze_personalization_response_curves(dataset, as_of=args.as_of)
    stem = f"{args.dataset.stem}-personalization-response-curves-v1"
    output_json = args.output_dir / f"{stem}.json"
    output_markdown = args.output_dir / f"{stem}.md"
    atomic_write_json(output_json, report)
    atomic_write_bytes(
        output_markdown,
        render_personalization_response_curves_markdown(report).encode("utf-8"),
    )
    print(json.dumps({
        "status": report["conclusion"]["status"],
        "default_curve_mechanical_pass": report["conclusion"][
            "default_curve_mechanical_pass"
        ],
        "output_json": str(output_json),
        "output_markdown": str(output_markdown),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
