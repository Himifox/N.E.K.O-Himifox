"""Recover candidate-first context for a recommendation human-review bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_context_recovery import (
    DEFAULT_MAX_MESSAGES,
    DEFAULT_TIMEZONE,
    build_context_recovery_audit_markdown,
    load_time_indexed_archive,
    recover_candidate_review_context,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("freeze", type=Path)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES)
    args = parser.parse_args()

    freeze_path = args.freeze.resolve()
    workbook_path = args.workbook.resolve()
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    workbook = json.loads(workbook_path.read_text(encoding="utf-8"))
    turns, source_meta = load_time_indexed_archive(
        args.database,
        timezone_name=args.timezone,
    )
    recovered, summary = recover_candidate_review_context(
        freeze,
        workbook,
        turns,
        source_meta,
        max_messages=args.max_messages,
    )
    recovered["context_recovery"]["created_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    recovered["context_recovery"]["source_freeze"] = freeze_path.name
    recovered["context_recovery"]["source_workbook"] = workbook_path.name
    output = args.output.resolve()
    atomic_write_json(output, recovered)
    audit_output = (
        args.audit_output.resolve()
        if args.audit_output
        else output.with_name(f"{output.stem}-audit.md")
    )
    atomic_write_bytes(
        audit_output,
        build_context_recovery_audit_markdown(summary, source_meta).encode("utf-8"),
    )
    print(json.dumps({
        "output": str(output),
        "audit_output": str(audit_output),
        "summary": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
