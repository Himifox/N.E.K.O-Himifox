"""Create a P44-F2-R0 blind annotation manifest from an immutable freeze."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_timing_annotation import (
    build_timing_annotation_manifest,
    timing_annotation_readiness,
    validate_timing_annotation_manifest,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _markdown(manifest: dict[str, object], readiness: dict[str, object]) -> str:
    counts = readiness["counts"]
    return "\n".join([
        "# P44-F2-R0 Timing Evidence Restart preflight",
        "",
        f"- Freeze: `{manifest['source_freeze']['filename']}`",
        f"- SHA-256: `{manifest['source_freeze']['sha256']}`",
        f"- Observation: {manifest['source_freeze']['observation_count']}",
        f"- Analysis-eligible after technical/privacy exclusion: {counts['structurally_eligible_count']}",
        f"- Initial readiness: `{readiness['status']}`",
        "",
        "## Blindness contract",
        "",
        "The reviewer manifest contains only sanitized candidate title/summary/source,",
        "activity state and redaction notes. It excludes production score/rank/source,",
        "delivery outcome/reason/text, feedback/inferred ignored, and all timing values.",
        "",
        "## Review protocol",
        "",
        "- Primary label: `should_recommend=true|false|abstain`, confidence and reason.",
        "- A deterministic, stratified >=20% sample receives an independent blind second review.",
        "- A completed primary/second disagreement requires adjudication; raw reviews remain intact.",
        "- Abstentions, privacy blocks, technical failures and inferred ignored never enter outcome denominators.",
        "",
        "## Rerun gate",
        "",
        f"- Qualified delivered >= {readiness['requirements']['minimum_qualified_delivered']}",
        f"- Qualified pass >= {readiness['requirements']['minimum_qualified_pass']}",
        f"- Every delivered/pass × true/false cell >= {readiness['requirements']['minimum_each_delivery_label_cell']}",
        "- All primary reviews, required blind second reviews and disagreements must be handled.",
        "",
        "This preflight does not label cases, alter the freeze, run F2, or change MVP behavior.",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("freeze", type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument("--preflight-output", required=True, type=Path)
    parser.add_argument("--created-at", default=None)
    args = parser.parse_args()
    freeze_path = args.freeze.resolve()
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    created_at = args.created_at or datetime.now(timezone.utc).isoformat()
    manifest = build_timing_annotation_manifest(
        freeze,
        source_freeze_filename=freeze_path.name,
        source_freeze_sha256=_sha256(freeze_path),
        created_at=created_at,
    )
    errors = validate_timing_annotation_manifest(manifest)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    readiness = timing_annotation_readiness(freeze, manifest)
    atomic_write_json(args.manifest_output.resolve(), manifest)
    atomic_write_bytes(args.preflight_output.resolve(), _markdown(manifest, readiness).encode("utf-8"))
    print(json.dumps({
        "manifest": str(args.manifest_output.resolve()),
        "preflight": str(args.preflight_output.resolve()),
        "readiness": readiness["status"],
        "structurally_eligible_count": readiness["counts"]["structurally_eligible_count"],
        "blockers": readiness["blockers"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
