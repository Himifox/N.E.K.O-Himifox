"""Recover causally bounded dialogue context into a derived P44-F2-R0 manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_context_recovery import load_time_indexed_archive
from tests.testbench.pipeline.recommendation_timing_annotation import validate_timing_annotation_manifest
from tests.testbench.pipeline.recommendation_timing_context import (
    build_timing_context_recovery_markdown,
    recover_timing_blind_context,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("freeze", type=Path)
    parser.add_argument("time_indexed_db", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit-output", required=True, type=Path)
    parser.add_argument("--max-messages", type=int, default=10)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.resolve().read_text(encoding="utf-8"))
    freeze = json.loads(args.freeze.resolve().read_text(encoding="utf-8"))
    errors = validate_timing_annotation_manifest(manifest)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    turns, source_meta = load_time_indexed_archive(args.time_indexed_db.resolve())
    recovered, audit = recover_timing_blind_context(
        manifest, freeze, turns, source_meta, max_messages=args.max_messages,
    )
    errors = validate_timing_annotation_manifest(recovered)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    atomic_write_json(args.output.resolve(), recovered)
    atomic_write_bytes(args.audit_output.resolve(), build_timing_context_recovery_markdown(audit).encode("utf-8"))
    print(json.dumps({"output": str(args.output.resolve()), "audit": audit}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
