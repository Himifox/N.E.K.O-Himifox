"""Canonical technical-baseline signoff and known-regression verification."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tests.testbench import config as tb_config
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_adapter import RECOMMENDATION_ADAPTER_VERSION
from tests.testbench.pipeline.recommendation_evaluator import RECOMMENDATION_EVALUATOR_VERSION
from tests.testbench.pipeline.recommendation_runner import check_reproducibility, read_run, run_experiment


class BaselineSignoffError(RuntimeError):
    pass


DEFAULT_BASELINE_VARIANT = {"id": "production_default", "source_weights": {},
                            "source_type_adjustments": {}, "active_min_score_gap": 0.05}


def signoff_canonical_baseline(baseline_id: str = "canonical-production-default-v1", *, overwrite: bool = False) -> dict[str, Any]:
    target = _path(baseline_id)
    if target.exists() and not overwrite:
        raise BaselineSignoffError("baseline already exists")
    spec = {"name": "canonical-production-default-signoff", "suite_mode": "canonical_builtin",
            "baseline_variant": "production_default", "variants": [dict(DEFAULT_BASELINE_VARIANT)]}
    reproducibility = check_reproducibility(spec)
    if not reproducibility["reproducible"]:
        raise BaselineSignoffError("canonical double run is not reproducible")
    first, second = read_run(reproducibility["first_run_id"]), read_run(reproducibility["second_run_id"])
    checks = _signoff_checks(first, second)
    if not all(checks.values()):
        raise BaselineSignoffError("baseline signoff checks failed: " + ", ".join(k for k, ok in checks.items() if not ok))
    manifest, selection = first["suite_manifest"], first["selection"]
    metric = first["metrics"]["production_default"]
    artifact = {
        "schema_version": 1, "kind": "canonical_technical_baseline", "baseline_id": baseline_id,
        "baseline_run_id": first["id"], "verification_run_id": second["id"],
        "suite_id": manifest["suite_id"], "suite_version": manifest["suite_version"],
        "suite_content_hash": manifest["content_hash"], "input_hash": first["input_hash"],
        "gate_policy_id": first["gate_policy"]["gate_policy_id"],
        "gate_policy_hash": _hash(first["gate_policy"]),
        "evaluator_version": RECOMMENDATION_EVALUATOR_VERSION,
        "adapter_version": RECOMMENDATION_ADAPTER_VERSION,
        "git_revision": _git_revision(first), "scenario_count": first["scenario_count"],
        "builtin_selected": selection["builtin_selected"], "user_selected": selection["user_selected"],
        "ranking_eligible_count": selection["ranking_eligible"],
        "relevance_labeled_count": selection["relevance_labeled"],
        "metrics": metric, "execution_status": first["execution_status"],
        "contract_status": first["contract_status"], "data_quality_status": first["data_quality_status"],
        "baseline_quality_gate": first["quality_gate"], "created_at": first["created_at"],
        "signed_off_at": datetime.now(timezone.utc).isoformat(), "signoff_checks": checks,
        "signoff_kind": "technical_baseline", "annotation_review_complete": False,
        "production_release_approved": False,
    }
    tb_config.RECOMMENDATION_BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, artifact)
    return artifact


def validate_known_regression(baseline_id: str) -> dict[str, Any]:
    baseline = read_baseline(baseline_id)
    spec = {"name": "known-regression-news-minus-002", "suite_mode": "canonical_builtin",
            "baseline_variant": "production_default", "variants": [dict(DEFAULT_BASELINE_VARIANT),
            {"id": "known_regression_news_minus_002", "source_weights": {},
             "source_type_adjustments": {"news": -0.02}, "active_min_score_gap": 0.05}]}
    first_summary, second_summary = run_experiment(spec), run_experiment(spec)
    first, second = read_run(first_summary["id"]), read_run(second_summary["id"])
    variant = "known_regression_news_minus_002"
    reasons = first.get("status_reasons", {}).get("quality_gate", [])
    checks = {
        "baseline_suite_matches": first.get("suite_manifest", {}).get("suite_id") == baseline.get("suite_id"),
        "double_run_input_hash": first.get("input_hash") == second.get("input_hash"),
        "double_run_metrics": first.get("metrics") == second.get("metrics"),
        "double_run_reasons": first.get("status_reasons") == second.get("status_reasons"),
        "double_run_losses": first.get("comparisons", {}).get(variant, {}).get("loss_details") == second.get("comparisons", {}).get(variant, {}).get("loss_details"),
        "execution_passed": first.get("execution_status") == "passed" == second.get("execution_status"),
        "contracts_passed": first.get("contract_status") == "passed" == second.get("contract_status"),
        "quality_rejected": first.get("quality_gate") == "rejected" == second.get("quality_gate"),
        "ranking_regression_reason": any("hit_at_1_regression" in reason or "ndcg_at_3_regression" in reason for reason in reasons),
        "known_privacy_loss": any(row.get("scenario_id") == "privacy_04" for row in first.get("comparisons", {}).get(variant, {}).get("loss_details", [])),
    }
    artifact = {"schema_version": 1, "kind": "known_regression_verification",
                "verification_id": f"{baseline_id}-news-minus-002", "baseline_id": baseline_id,
                "first_run_id": first["id"], "second_run_id": second["id"],
                "candidate_id": variant, "checks": checks, "passed": all(checks.values()),
                "quality_gate_reasons": reasons,
                "loss_details": first.get("comparisons", {}).get(variant, {}).get("loss_details", []),
                "created_at": datetime.now(timezone.utc).isoformat()}
    atomic_write_json(tb_config.RECOMMENDATION_BASELINES_DIR / f"{artifact['verification_id']}.json", artifact)
    return artifact


def list_baselines() -> list[dict[str, Any]]:
    rows = []
    for path in tb_config.RECOMMENDATION_BASELINES_DIR.glob("*.json") if tb_config.RECOMMENDATION_BASELINES_DIR.exists() else []:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("kind") == "canonical_technical_baseline": rows.append(row)
        except (OSError, ValueError): pass
    return sorted(rows, key=lambda row: row.get("signed_off_at") or "", reverse=True)


def read_baseline(baseline_id: str) -> dict[str, Any]:
    path = _path(baseline_id)
    if not path.exists(): raise BaselineSignoffError("baseline not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _signoff_checks(first: dict[str, Any], second: dict[str, Any]) -> dict[str, bool]:
    fm, sm = first.get("suite_manifest") or {}, second.get("suite_manifest") or {}
    return {"suite_id": fm.get("suite_id") == sm.get("suite_id"),
            "suite_version": fm.get("suite_version") == sm.get("suite_version"),
            "suite_content_hash": fm.get("content_hash") == sm.get("content_hash"),
            "input_hash": first.get("input_hash") == second.get("input_hash"),
            "scenario_count": first.get("scenario_count") == second.get("scenario_count"),
            "selection": first.get("selection") == second.get("selection"),
            "metrics": first.get("metrics") == second.get("metrics"),
            "top1_results": _top1(first) == _top1(second),
            "execution_zero_errors": first.get("metrics", {}).get("production_default", {}).get("errored") == 0,
            "hard_constraints_zero": first.get("metrics", {}).get("production_default", {}).get("hard_violation_count") == 0,
            "statuses_passed": all(first.get(key) == "passed" for key in ("execution_status", "contract_status", "data_quality_status")),
            "user_copies_zero": first.get("selection", {}).get("user_selected") == 0,
            "annotation_coverage": first.get("selection", {}).get("ranking_eligible") == first.get("selection", {}).get("relevance_labeled")}


def _top1(run: dict[str, Any]) -> dict[str, Any]:
    return {row["scenario_id"]: (row.get("snapshot") or {}).get("top1_candidate_id")
            for row in run.get("cases_by_variant", {}).get("production_default", []) if not row.get("error")}


def _git_revision(run: dict[str, Any]) -> str:
    for row in run.get("cases_by_variant", {}).get("production_default", []):
        revision = (row.get("snapshot") or {}).get("git_revision")
        if revision: return str(revision)
    return "unknown"


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _path(baseline_id: str) -> Path:
    if not baseline_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in baseline_id):
        raise BaselineSignoffError("invalid baseline id")
    return tb_config.RECOMMENDATION_BASELINES_DIR / f"{baseline_id}.json"
