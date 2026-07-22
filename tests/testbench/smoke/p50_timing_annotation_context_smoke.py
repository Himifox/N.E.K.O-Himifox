"""P44-F2-R0 causal dialogue-context recovery smoke."""
from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_timing_annotation import (
    build_timing_annotation_manifest,
    validate_timing_annotation_manifest,
)
from tests.testbench.pipeline.recommendation_timing_context import recover_timing_blind_context


def main() -> int:
    freeze = {"observations": [{
        "turn_id": "turn-1", "ts": 100.0, "delivered": True,
        "review_context": {"activity_state": "idle", "redaction_notes": [], "candidate_labels": [{
            "id": "news:1", "source_type": "news", "safe_title": "title", "safe_summary": "summary", "score": 0.9,
        }]},
    }]}
    manifest = build_timing_annotation_manifest(
        freeze, source_freeze_filename="freeze.json", source_freeze_sha256="b" * 64,
        created_at="2026-07-21T12:00:00+08:00",
    )
    turns = [
        {"ts_epoch": 95.0, "message_id": "before", "role": "user", "content": "relevant context"},
        {"ts_epoch": 101.0, "message_id": "after", "role": "assistant", "content": "must not leak"},
    ]
    recovered, audit = recover_timing_blind_context(
        manifest, freeze, turns, {"source_db_name": "fixture", "source_db_sha256": "c" * 64},
    )
    assert audit["recovered_count"] == 1 and audit["causal_violation_count"] == 0
    context = recovered["items"][0]["context_for_blind_review"]["pre_decision_context"]
    assert context["messages"] == [{"role": "user", "content": "relevant context"}]
    serialised = json.dumps(recovered, ensure_ascii=False)
    assert "must not leak" not in serialised
    assert '"score"' not in serialised
    assert not validate_timing_annotation_manifest(recovered)
    print("P50 TIMING ANNOTATION CONTEXT SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
