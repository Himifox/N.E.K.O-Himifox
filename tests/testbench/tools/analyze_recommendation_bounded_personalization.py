"""Run P44-G1-R1 bounded source-affinity impact analysis."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_bounded_personalization import (
    analyze_bounded_personalization,
    render_bounded_personalization_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("output_markdown", type=Path)
    parser.add_argument("--max-abs-delta", type=float, default=0.03)
    parser.add_argument("--as-of", type=float)
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = analyze_bounded_personalization(
        dataset,
        max_abs_delta=args.max_abs_delta,
        as_of=args.as_of,
    )
    atomic_write_json(args.output_json, report)
    atomic_write_bytes(
        args.output_markdown,
        render_bounded_personalization_markdown(report).encode("utf-8"),
    )
    print(json.dumps({
        "status": report["conclusion"]["status"],
        "warm_state_observations": report["impact"]["warm_state_observation_count"],
        "top1_flips": report["impact"]["top1_flip_count"],
        "output_json": str(args.output_json),
        "output_markdown": str(args.output_markdown),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
