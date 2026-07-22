"""Generate the P44-G1 encounter acceptance JSON and Markdown reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_encounter_acceptance import (
    analyze_encounter_acceptance,
    render_encounter_acceptance_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--as-of", type=float)
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = analyze_encounter_acceptance(dataset, as_of=args.as_of)
    atomic_write_json(args.json_output, report)
    atomic_write_bytes(
        args.markdown_output,
        render_encounter_acceptance_markdown(report).encode("utf-8"),
    )
    print(json.dumps(report["conclusion"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
