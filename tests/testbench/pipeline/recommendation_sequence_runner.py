"""Multi-step semantic runner; all scores come from production adapters."""
from __future__ import annotations

import copy
import math
from typing import Any

from tests.testbench.pipeline.recommendation_adapter import run_material_stage, run_source_stage


def run_sequence_scenario(scenario: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    context = copy.deepcopy(scenario.get("base_context") or {})
    inputs = copy.deepcopy(scenario.get("base_inputs") or {})
    runner = run_material_stage if scenario.get("stage") == "material" else run_source_stage
    results, transitions, violations, quality_failures = [], [], [], []
    previous = None
    for index, step in enumerate(scenario.get("steps") or []):
        context.update(copy.deepcopy(step.get("context_patch") or {}))
        step_variant = {**variant, **copy.deepcopy(step.get("variant_patch") or {})}
        single = {"id": f"{scenario['id']}__{step['id']}", "stage": scenario["stage"],
                  "context": copy.deepcopy(context), "inputs": copy.deepcopy(inputs), "oracle": {}}
        first, second = runner(single, step_variant), runner(single, step_variant)
        deterministic = _stable(first) == _stable(second)
        if not deterministic:
            violations.append({"code": "non_deterministic", "step_id": step["id"]})
        failures = _evaluate_step(step.get("oracle") or {}, first, previous)
        quality_failures.extend({**item, "step_id": step["id"]} for item in failures)
        if previous is not None:
            transitions.append(_transition(results[-1]["step_id"], step["id"], previous, first))
        results.append({"step_id": step["id"], "snapshot": first, "quality_failures": failures})
        previous = first
    final = results[-1]["snapshot"]
    evaluation = {"violations": violations, "quality_failures": quality_failures,
                  "passed": not violations and not quality_failures,
                  "evaluation_mode": "sequence",
                  "hit1": None, "hit3": None, "acceptable_top1": not quality_failures,
                  "mrr": 0.0, "ndcg3": None, "top1_source": final.get("top1_source_type"),
                  "top1_candidate_id": final.get("top1_candidate_id"), "score_gap": final.get("score_gap")}
    return {"scenario_id": scenario["id"], "snapshot": final, "sequence_steps": results,
            "transitions": transitions, "evaluation": evaluation, "violations": violations}


def _stable(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(snapshot.get(key) for key in ("candidate_count", "filtered_reasons", "ranked_candidates",
                                                "score_breakdown", "active_bias", "phase1_topics_after"))


def _evaluate_step(oracle: dict[str, Any], current: dict[str, Any], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    failures = []
    expected_source = oracle.get("expected_top1_source")
    if expected_source is not None and current.get("top1_source_type") != expected_source:
        failures.append({"code": "top1_source_mismatch", "expected": expected_source,
                         "actual": current.get("top1_source_type")})
    expected_candidate = oracle.get("expected_top1_candidate_id")
    if expected_candidate is not None and current.get("top1_candidate_id") != expected_candidate:
        failures.append({"code": "top1_candidate_mismatch", "expected": expected_candidate,
                         "actual": current.get("top1_candidate_id")})
    bias_expected = oracle.get("active_bias_expected")
    bias = current.get("active_bias") or {}
    if bias_expected is not None and bool(bias.get("applied")) != bool(bias_expected):
        failures.append({"code": "active_bias_mismatch", "expected": bias_expected, "actual": bias.get("applied")})
    reason = oracle.get("expected_active_bias_reason")
    if reason is not None and bias.get("fallback_reason") != reason:
        failures.append({"code": "active_bias_reason_mismatch", "expected": reason,
                         "actual": bias.get("fallback_reason")})
    if previous is not None:
        before, after = _source_scores(previous), _source_scores(current)
        for source, direction in (oracle.get("score_directions_from_previous") or {}).items():
            delta = after.get(source, 0.0) - before.get(source, 0.0)
            ok = delta < -1e-6 if direction == "down" else delta > 1e-6 if direction == "up" else math.isclose(delta, 0.0, abs_tol=1e-6)
            if not ok:
                failures.append({"code": "score_direction_mismatch", "source": source,
                                 "expected": direction, "delta": round(delta, 6)})
    return failures


def _source_scores(snapshot: dict[str, Any]) -> dict[str, float]:
    return {source: float(info.get("score") or 0.0) for source, info in (snapshot.get("source_scores") or {}).items()}


def _transition(before_id: str, after_id: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    left, right = _source_scores(before), _source_scores(after)
    return {"from": before_id, "to": after_id,
            "source_score_deltas": {source: round(right.get(source, 0.0) - left.get(source, 0.0), 6)
                                    for source in sorted(set(left) | set(right))},
            "top1_changed": before.get("top1_source_type") != after.get("top1_source_type")}
