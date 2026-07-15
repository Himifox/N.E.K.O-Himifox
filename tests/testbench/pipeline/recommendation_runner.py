"""Experiment orchestration and persistence for recommendation testbench runs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.testbench import config as tb_config
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_adapter import run_material_stage, run_source_stage
from tests.testbench.pipeline.recommendation_evaluator import aggregate_variant, compare_variants, evaluate_case
from tests.testbench.pipeline.recommendation_scenario import list_scenarios, read_scenario
from tests.testbench.pipeline.recommendation_sequence_runner import run_sequence_scenario
from tests.testbench.pipeline.recommendation_coverage import build_coverage_report
from tests.testbench.pipeline.recommendation_suite import canonical_builtin_scenarios, load_builtin_manifest, verify_builtin_manifest

MAX_SCENARIOS = 50
MAX_VARIANTS = 5
GATE_POLICY = {"gate_policy_id": "recommendation_gate_v1", "hit_at_1_max_drop": 0.02,
               "ndcg_at_3_max_drop": 0.02, "repeat_rate_max_increase": 0.05,
               "max_source_exposure": 0.60, "max_source_hhi": 0.55,
               "allow_major_paired_losses": False}


class RecommendationRunError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message); self.code, self.message, self.status = code, message, status


def _select_scenarios(filters: dict[str, Any], suite_mode: str) -> list[dict[str, Any]]:
    ids = set(filters.get("ids") or [])
    tags = set(filters.get("tags") or [])
    if suite_mode == "canonical_builtin":
        scenarios = canonical_builtin_scenarios()
        return [row for row in scenarios if (not ids or row["id"] in ids)
                and (not tags or tags.intersection(row.get("tags") or []))]
    metas = list_scenarios()
    include_user = bool(filters.get("include_user"))
    chosen = [m for m in metas if (not ids or m["id"] in ids) and (not tags or tags.intersection(m.get("tags") or []))]
    if suite_mode == "user_only":
        chosen = [m for m in chosen if m.get("source") == "user"]
    # A standard unfiltered benchmark must not be skewed by UI-created copies.
    # Explicit IDs always win; include_user supports intentional custom suites.
    if suite_mode not in {"mixed_exploration", "user_only"} and not ids and not include_user:
        chosen = [m for m in chosen if m.get("source") == "builtin"]
    return [read_scenario(m["id"]) for m in chosen]


def _clean_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in scenario.items() if not k.startswith("_") and k not in {"has_builtin", "has_user", "overriding_builtin"}}


def preview_scenario(scenario: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    clean = _clean_scenario(scenario)
    if clean.get("kind") == "sequence":
        return run_sequence_scenario(clean, variant)
    runner = run_material_stage if clean.get("stage") == "material" else run_source_stage
    first = runner(clean, variant)
    second = runner(clean, variant)
    stable_keys = ["stage", "candidate_count", "filtered_reasons", "ranked_candidates", "score_breakdown", "active_bias", "phase1_topics_after"]
    deterministic = all(first.get(key) == second.get(key) for key in stable_keys)
    evaluation = evaluate_case(clean, first, deterministic)
    return {"scenario_id": clean["id"], "evaluation_mode": evaluation.get("evaluation_mode"),
            "snapshot": first, "evaluation": evaluation, "violations": evaluation["violations"]}


def run_experiment(spec: dict[str, Any]) -> dict[str, Any]:
    suite_mode = str(spec.get("suite_mode") or "canonical_builtin")
    if suite_mode not in {"canonical_builtin", "mixed_exploration", "user_only"}:
        raise RecommendationRunError("RecommendationSuiteModeInvalid", "invalid suite_mode", 422)
    scenarios = _select_scenarios(spec.get("scenario_filter") or {}, suite_mode)
    variants = spec.get("variants") or []
    if not scenarios:
        raise RecommendationRunError("RecommendationRunEmpty", "no scenarios matched", 422)
    if len(scenarios) > MAX_SCENARIOS or not 1 <= len(variants) <= MAX_VARIANTS:
        raise RecommendationRunError("RecommendationRunLimit", "maximum is 50 scenarios and 5 variants", 422)
    variant_ids = [str(v.get("id") or "") for v in variants]
    if len(set(variant_ids)) != len(variant_ids) or any(not item for item in variant_ids):
        raise RecommendationRunError("RecommendationVariantInvalid", "variant ids must be unique and non-empty", 422)
    baseline_id = str(spec.get("baseline_variant") or variant_ids[0])
    if baseline_id not in variant_ids:
        raise RecommendationRunError("RecommendationBaselineInvalid", "baseline_variant is not in variants", 422)

    cases_by_variant: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        rows = []
        for scenario in scenarios:
            try:
                rows.append(preview_scenario(scenario, variant))
            except Exception as exc:
                rows.append({"scenario_id": scenario["id"], "error": f"{type(exc).__name__}: {exc}", "violations": []})
        cases_by_variant[variant["id"]] = rows
    metrics = {vid: aggregate_variant(rows) for vid, rows in cases_by_variant.items()}
    comparisons = {vid: compare_variants(cases_by_variant[baseline_id], rows) for vid, rows in cases_by_variant.items() if vid != baseline_id}
    resource_weight_states = {vid: _resource_weight_state(rows, metrics[vid])
                              for vid, rows in cases_by_variant.items()}
    weight_changes = {vid: _weight_changes(resource_weight_states[baseline_id], resource_weight_states[vid])
                      for vid in cases_by_variant if vid != baseline_id}
    warnings = _warnings(metrics)
    manifest_check = verify_builtin_manifest() if suite_mode == "canonical_builtin" else {"ok": True, "errors": []}
    selection = _selection_summary(scenarios, suite_mode)
    statuses = _layered_status(metrics, baseline_id, comparisons, manifest_check, selection)
    status = "failed" if statuses["execution_status"] == "failed" or statuses["contract_status"] == "failed" else (
        "regressed" if statuses["quality_gate"] == "rejected" else "passed_with_warnings" if warnings else "passed")
    run_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {"schema_version": 1, "id": run_id, "name": str(spec.get("name") or run_id), "created_at": created_at,
               "status": status, **statuses, "baseline_variant": baseline_id, "scenario_count": len(scenarios), "variants": variants,
               "suite_mode": suite_mode, "suite_manifest": load_builtin_manifest() if suite_mode == "canonical_builtin" else None,
               "selection": selection, "gate_policy": dict(GATE_POLICY),
               "scenario_snapshots": [_clean_scenario(s) for s in scenarios], "cases_by_variant": cases_by_variant,
               "metrics": metrics, "comparisons": comparisons, "warnings": warnings,
               "resource_weight_states": resource_weight_states, "weight_changes": weight_changes,
               "production_config_modified": False, "coverage_snapshot": build_coverage_report()}
    payload["input_hash"] = hashlib.sha256(json.dumps({"suite_mode": suite_mode, "suite_manifest": payload.get("suite_manifest"),
        "scenarios": payload["scenario_snapshots"], "variants": variants, "gate_policy": payload["gate_policy"]},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    tb_config.RECOMMENDATION_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(tb_config.RECOMMENDATION_RUNS_DIR / f"{run_id}.json", payload)
    return run_summary(payload)


def _status(metrics: dict[str, dict[str, Any]], baseline_id: str,
            comparisons: dict[str, dict[str, Any]], warnings: list[dict[str, Any]]) -> str:
    if any(m["hard_violation_count"] or m["errored"] for m in metrics.values()): return "failed"
    base = metrics[baseline_id]
    for vid, metric in metrics.items():
        if vid == baseline_id: continue
        if (_drop(metric.get("hit1"), base.get("hit1")) < -0.02
                or _drop(metric.get("ndcg3"), base.get("ndcg3")) < -0.02
                or _drop(metric.get("acceptable_top1_rate"), base.get("acceptable_top1_rate")) < -0.02
                or metric.get("candidate_repeat_rate", 0) - base.get("candidate_repeat_rate", 0) > 0.05
                or comparisons.get(vid, {}).get("losses", 0) > comparisons.get(vid, {}).get("wins", 0)):
            return "regressed"
    if warnings: return "passed_with_warnings"
    return "passed"


def _selection_summary(scenarios: list[dict[str, Any]], suite_mode: str) -> dict[str, Any]:
    modes = [_scenario_mode(row) for row in scenarios]
    return {"suite_mode": suite_mode,
            "builtin_selected": sum(row.get("_source") == "builtin" for row in scenarios),
            "user_selected": sum(row.get("_source") == "user" for row in scenarios),
            "ranking_eligible": modes.count("ranking"),
            "relevance_labeled": sum(bool((row.get("oracle") or {}).get("relevance")) for row in scenarios),
            "contract_only": modes.count("contract"), "sequence_cases": modes.count("sequence"),
            "no_candidate_cases": sum((row.get("oracle") or {}).get("expected_empty") is True for row in scenarios),
            "partially_or_fully_filtered_cases": sum(bool((row.get("oracle") or {}).get("must_filter_candidate_ids")) for row in scenarios)}


def _scenario_mode(scenario: dict[str, Any]) -> str:
    return str(scenario.get("evaluation_mode") or ("sequence" if scenario.get("kind") == "sequence"
                                                     else "ranking" if (scenario.get("oracle") or {}).get("relevance")
                                                     else "contract"))


def _layered_status(metrics: dict[str, dict[str, Any]], baseline_id: str,
                    comparisons: dict[str, dict[str, Any]], manifest_check: dict[str, Any],
                    selection: dict[str, Any]) -> dict[str, Any]:
    execution_failed = any(metric.get("errored") for metric in metrics.values())
    contract_failed = any(metric.get("hard_violation_count") for metric in metrics.values())
    data_reasons = list(manifest_check.get("errors") or [])
    if selection["ranking_eligible"] != selection["relevance_labeled"]:
        data_reasons.append("ranking_annotation_coverage_below_100_percent")
    quality_reasons = []
    base = metrics[baseline_id]
    for variant, metric in metrics.items():
        if float(metric.get("max_source_exposure") or 0.0) > GATE_POLICY["max_source_exposure"]:
            quality_reasons.append(f"{variant}:source_exposure_guardrail")
        if float(metric.get("source_hhi") or 0.0) > GATE_POLICY["max_source_hhi"]:
            quality_reasons.append(f"{variant}:source_hhi_guardrail")
        if variant == baseline_id:
            continue
        if _drop(metric.get("hit1"), base.get("hit1")) < -GATE_POLICY["hit_at_1_max_drop"]:
            quality_reasons.append(f"{variant}:hit_at_1_regression")
        if _drop(metric.get("ndcg3"), base.get("ndcg3")) < -GATE_POLICY["ndcg_at_3_max_drop"]:
            quality_reasons.append(f"{variant}:ndcg_at_3_regression")
        if metric.get("candidate_repeat_rate", 0) - base.get("candidate_repeat_rate", 0) > GATE_POLICY["repeat_rate_max_increase"]:
            quality_reasons.append(f"{variant}:repeat_rate_guardrail")
        comparison = comparisons.get(variant, {})
        if comparison.get("losses", 0) > comparison.get("wins", 0):
            quality_reasons.append(f"{variant}:paired_loss_without_offsetting_win")
    if data_reasons:
        quality_reasons.append("data_quality_not_passed")
    return {"execution_status": "failed" if execution_failed else "passed",
            "contract_status": "failed" if contract_failed else "passed",
            "data_quality_status": "failed" if data_reasons else "passed",
            "quality_gate": "rejected" if quality_reasons else "accepted",
            "status_reasons": {"data_quality": data_reasons, "quality_gate": quality_reasons}}


def _warnings(metrics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for variant, metric in metrics.items():
        exposure = float(metric.get("max_source_exposure") or 0.0)
        if exposure > 0.6:
            distribution = metric.get("source_distribution") or {}
            source = max(distribution, key=distribution.get) if distribution else None
            result.append({"code": "source_exposure_over_limit", "variant": variant,
                           "source": source, "actual": exposure, "limit": 0.6})
        if metric.get("hit1") is None or metric.get("ndcg3") is None:
            result.append({"code": "insufficient_relevance_labels", "variant": variant})
    return result


def _drop(value: Any, base: Any) -> float:
    return float(value or 0) - float(base or 0)


def _resource_weight_state(cases: list[dict[str, Any]], metric: dict[str, Any]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, list[float]]] = {}
    for case in cases:
        if case.get("error"):
            continue
        snapshot = case.get("snapshot") or {}
        for candidate_id, part in (snapshot.get("score_breakdown") or {}).items():
            source = str(candidate_id).split(":", 1)[0]
            bucket = buckets.setdefault(source, {"source_weight": [], "source_type_adjustment": [],
                                                  "tuning_adjustment": [], "score": []})
            for key in bucket:
                value = part.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    bucket[key].append(float(value))
    distribution = metric.get("source_distribution") or {}
    result = {}
    for source in sorted(set(buckets) | set(distribution)):
        bucket = buckets.get(source, {})
        result[source] = {key: (round(sum(values) / len(values), 4) if values else None)
                          for key, values in bucket.items()}
        result[source]["top1_exposure"] = float(distribution.get(source, 0.0))
        result[source]["sample_count"] = len(bucket.get("score", []))
    return result


def _weight_changes(baseline: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for source in sorted(set(baseline) | set(current)):
        before, after = baseline.get(source, {}), current.get(source, {})
        row: dict[str, Any] = {"source": source, "baseline": before, "current": after}
        row["delta"] = {}
        for key in ("source_weight", "source_type_adjustment", "tuning_adjustment", "score", "top1_exposure"):
            left, right = before.get(key), after.get(key)
            row["delta"][key] = round(float(right) - float(left), 4) if left is not None and right is not None else None
        rows.append(row)
    return rows


def run_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ("id", "name", "created_at", "status", "execution_status", "contract_status",
            "data_quality_status", "quality_gate", "status_reasons", "suite_mode", "suite_manifest", "selection", "gate_policy",
            "baseline_variant", "scenario_count", "metrics", "comparisons", "warnings", "resource_weight_states", "weight_changes",
            "production_config_modified", "input_hash")}


def list_runs() -> list[dict[str, Any]]:
    rows = []
    for path in tb_config.RECOMMENDATION_RUNS_DIR.glob("*.json") if tb_config.RECOMMENDATION_RUNS_DIR.exists() else []:
        try: rows.append(run_summary(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError): continue
    return sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)


def read_run(run_id: str) -> dict[str, Any]:
    path = tb_config.RECOMMENDATION_RUNS_DIR / f"{run_id}.json"
    if not path.exists(): raise RecommendationRunError("RecommendationRunNotFound", "run not found", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def delete_run(run_id: str) -> dict[str, Any]:
    path = tb_config.RECOMMENDATION_RUNS_DIR / f"{run_id}.json"
    if not path.exists(): raise RecommendationRunError("RecommendationRunNotFound", "run not found", 404)
    path.unlink(); return {"deleted": run_id}


def check_reproducibility(spec: dict[str, Any]) -> dict[str, Any]:
    """Execute the same canonical specification twice and compare stable outputs."""
    canonical = {**spec, "suite_mode": "canonical_builtin"}
    first_summary, second_summary = run_experiment(canonical), run_experiment(canonical)
    first, second = read_run(first_summary["id"]), read_run(second_summary["id"])
    checks = {
        "suite_id": (first.get("suite_manifest") or {}).get("suite_id") == (second.get("suite_manifest") or {}).get("suite_id"),
        "suite_content_hash": (first.get("suite_manifest") or {}).get("content_hash") == (second.get("suite_manifest") or {}).get("content_hash"),
        "input_hash": first.get("input_hash") == second.get("input_hash"),
        "scenario_count": first.get("scenario_count") == second.get("scenario_count"),
        "top1_results": _top1_map(first) == _top1_map(second),
        "metrics": first.get("metrics") == second.get("metrics"),
        "user_copy_isolation": first.get("selection", {}).get("user_selected") == 0 == second.get("selection", {}).get("user_selected"),
    }
    return {"reproducible": all(checks.values()), "checks": checks,
            "first_run_id": first["id"], "second_run_id": second["id"]}


def _top1_map(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {variant: {row["scenario_id"]: (row.get("snapshot") or {}).get("top1_candidate_id")
                      for row in rows if not row.get("error")}
            for variant, rows in (run.get("cases_by_variant") or {}).items()}
