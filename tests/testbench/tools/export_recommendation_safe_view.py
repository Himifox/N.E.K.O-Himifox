"""Create a new safe recommendation artifact without rewriting its sources."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_safe_export import (
    prepare_recommendation_safe_view,
    write_new_recommendation_safe_export,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        rows.append(value)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="Freeze or Golden JSON")
    source.add_argument("--observations", type=Path, help="Observation JSONL")
    parser.add_argument("--feedback", type=Path, help="Feedback JSONL")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.source is not None:
        if args.feedback is not None:
            parser.error("--feedback is only valid with --observations")
        artifact = _read_json(args.source)
    else:
        artifact = {
            "schema_version": 1,
            "dataset_type": "shadow_safe_view",
            "observations": _read_jsonl(args.observations),
            "feedback": _read_jsonl(args.feedback) if args.feedback else [],
        }
    prepared = prepare_recommendation_safe_view(artifact)
    target = write_new_recommendation_safe_export(args.output, prepared)
    print(json.dumps({
        "output": str(target),
        "observation_count": len(prepared.get("observations") or []),
        "feedback_count": len(prepared.get("feedback") or []),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
