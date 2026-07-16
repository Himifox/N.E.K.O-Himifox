"""Generate JSON and Markdown metrics from a Shadow annotation template."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_annotation_report import (
    build_annotation_report,
    build_annotation_report_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    source = args.input.resolve()
    bundle = json.loads(source.read_text(encoding="utf-8"))
    report = build_annotation_report(bundle)
    output_dir = (args.output_dir or source.parent).resolve()
    stem = source.stem.replace("-annotation-template", "-analysis")
    if stem == source.stem:
        stem = f"{source.stem}-analysis"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    atomic_write_json(json_path, report)
    atomic_write_bytes(markdown_path, build_annotation_report_markdown(report).encode("utf-8"))
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path),
                      "summary": report["summary"],
                      "weight_candidate_gate": report["weight_candidate_gate"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
