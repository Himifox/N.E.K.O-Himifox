"""Freeze the preview-v1 Shadow cohort used by P44-G1."""
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

from main_logic.proactive_recommendation_feedback import (
    sanitize_recommendation_feedback_event,
)
from main_logic.proactive_recommendation_observer import (
    sanitize_recommendation_observation,
)
from tests.testbench.pipeline.atomic_io import atomic_write_json


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
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
    preview_rows = [
        row for row in raw_observations
        if isinstance(row.get("feedback_state_preview"), Mapping)
        and row["feedback_state_preview"].get("version") == "feedback_state_preview_v1"
    ]
    inferred_cutoff = max((float(row.get("ts") or 0.0) for row in preview_rows), default=0.0)
    cutoff = inferred_cutoff if args.as_of is None else float(args.as_of)
    observations = [
        sanitize_recommendation_observation(row)
        for row in preview_rows
        if float(row.get("ts") or 0.0) <= cutoff
    ]
    turn_keys = {
        (str(row.get("lanlan_name") or ""), str(row.get("turn_id") or ""))
        for row in observations
        if row.get("lanlan_name") and row.get("turn_id")
    }
    feedback = [
        sanitize_recommendation_feedback_event(row)
        for row in _read_jsonl(args.feedback)
        if float(row.get("ts") or 0.0) <= cutoff
        and (str(row.get("lanlan_name") or ""), str(row.get("turn_id") or "")) in turn_keys
    ]
    freeze = {
        "schema_version": 1,
        "dataset_type": "shadow_preview_encounter",
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
    atomic_write_json(args.output, freeze)
    print(json.dumps({
        "as_of": cutoff,
        "observations": len(observations),
        "feedback": len(feedback),
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
