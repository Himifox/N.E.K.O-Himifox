"""Freeze a read-only feedback_state_preview_v2 Shadow cohort."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_safe_export import (
    prepare_recommendation_safe_view,
    write_new_recommendation_safe_export,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"{path}:{line_number} must contain an object")
        rows.append(dict(value))
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("observations", type=Path)
    parser.add_argument("feedback", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--as-of", type=float)
    args = parser.parse_args()

    raw_observations = _read_jsonl(args.observations)
    v2_rows = [
        row
        for row in raw_observations
        if isinstance(row.get("feedback_state_preview"), Mapping)
        and row["feedback_state_preview"].get("version")
        == "feedback_state_preview_v2"
    ]
    inferred_cutoff = max((float(row.get("ts") or 0.0) for row in v2_rows), default=0.0)
    cutoff = inferred_cutoff if args.as_of is None else float(args.as_of)
    observations = [row for row in v2_rows if float(row.get("ts") or 0.0) <= cutoff]
    turn_keys = {
        (str(row.get("lanlan_name") or ""), str(row.get("turn_id") or ""))
        for row in observations
        if row.get("lanlan_name") and row.get("turn_id")
    }
    feedback = [
        row
        for row in _read_jsonl(args.feedback)
        if float(row.get("ts") or 0.0) <= cutoff
        and (str(row.get("lanlan_name") or ""), str(row.get("turn_id") or ""))
        in turn_keys
    ]
    artifact = {
        "schema_version": 1,
        "dataset_type": "shadow_feedback_state_preview_v2",
        "as_of": cutoff,
        "source": {
            "observations_filename": args.observations.name,
            "observations_sha256": _sha256(args.observations),
            "feedback_filename": args.feedback.name,
            "feedback_sha256": _sha256(args.feedback),
        },
        "observations": observations,
        "feedback": feedback,
    }
    safe = prepare_recommendation_safe_view(artifact)
    write_new_recommendation_safe_export(args.output, safe)
    print(json.dumps({
        "as_of": cutoff,
        "observations": len(safe["observations"]),
        "feedback": len(safe["feedback"]),
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
