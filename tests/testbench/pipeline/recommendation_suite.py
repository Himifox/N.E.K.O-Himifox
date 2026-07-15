"""Canonical builtin suite manifest and integrity verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tests.testbench.pipeline.recommendation_scenario import read_builtin_scenario

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "recommendation_scenarios" / "builtin_manifest.json"


def load_builtin_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def canonical_builtin_scenarios() -> list[dict[str, Any]]:
    manifest = load_builtin_manifest()
    return [read_builtin_scenario(sid) for sid in manifest.get("scenario_ids") or []]


def suite_content_hash(scenarios: list[dict[str, Any]]) -> str:
    clean = [_clean(item) for item in sorted(scenarios, key=lambda row: row["id"])]
    raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify_builtin_manifest() -> dict[str, Any]:
    manifest = load_builtin_manifest()
    errors = []
    ids = manifest.get("scenario_ids") or []
    if manifest.get("scenario_count") != len(ids):
        errors.append("scenario_count_mismatch")
    if len(ids) != len(set(ids)):
        errors.append("duplicate_scenario_ids")
    try:
        scenarios = canonical_builtin_scenarios()
    except Exception as exc:
        return {"ok": False, "errors": [f"builtin_read_failed:{type(exc).__name__}"], "manifest": manifest}
    actual = suite_content_hash(scenarios)
    if actual != manifest.get("content_hash"):
        errors.append("content_hash_mismatch")
    return {"ok": not errors, "errors": errors, "manifest": manifest, "actual_content_hash": actual}


def _clean(scenario: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in scenario.items()
            if not k.startswith("_") and k not in {"has_builtin", "has_user", "overriding_builtin"}}
