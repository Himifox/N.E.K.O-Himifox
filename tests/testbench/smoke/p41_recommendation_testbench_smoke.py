"""P41 deterministic recommendation semantic-contract smoke."""
from __future__ import annotations

import inspect
import json
import math
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench import config as cfg
from tests.testbench.pipeline import recommendation_adapter as adapter
from tests.testbench.pipeline.recommendation_runner import check_reproducibility, preview_scenario, read_run, run_experiment
from tests.testbench.pipeline.recommendation_scenario import list_scenarios, read_scenario, save_user_scenario, validate_scenario_dict
from tests.testbench.pipeline.recommendation_suite import verify_builtin_manifest
from tests.testbench.pipeline.recommendation_baseline import signoff_canonical_baseline, validate_known_regression
from tests.testbench.pipeline.recommendation_personalization import run_personalization_trace
from tests.testbench.pipeline.recommendation_coverage import build_coverage_report
from tests.testbench.pipeline.recommendation_evaluator import aggregate_variant, evaluate_case
from main_logic.proactive_recommendation_feedback import build_feedback_event


def main() -> int:
    negative = evaluate_case(
        {"oracle": {"should_recommend": False, "relevance": {"news:x": 0}}},
        {"ranked_candidates": [{"id": "news:x", "source_type": "news"}]},
        deterministic=True,
    )
    assert negative["hit1"] is None and negative["ndcg3"] is None
    negative_metrics = aggregate_variant([{"evaluation": negative, "violations": []}])
    assert negative_metrics["transparent_metrics"]["positive_case_hit_at_1"]["denominator"] == 0
    assert negative_metrics["transparent_metrics"]["false_interruption_rate"] == {
        "numerator": 1, "denominator": 1, "value": 1.0,
    }
    scenarios = list_scenarios()
    builtins = [row for row in scenarios if row["source"] == "builtin"]
    assert len(builtins) >= 27, len(builtins)
    assert {row["stage"] for row in scenarios} == {"source", "material"}
    # The curated golden suite must not regain byte-for-byte duplicate inputs.
    coverage = build_coverage_report()
    assert verify_builtin_manifest()["ok"]
    assert coverage["unique_input_count"] == coverage["scenario_count"]
    assert not coverage["duplicate_groups"] and not coverage["oracle_conflicts"]
    assert {"source_repeat", "candidate_repeat", "active_bias_boundary", "active_bias_fallback",
            "risk", "context_preference"}.issubset(coverage["factor_coverage"])
    invalid = validate_scenario_dict({"id": "bad", "stage": "oops", "oracle": {"relevance": {"x": 9}}})
    assert not invalid["ok"] and len(invalid["errors"]) >= 2
    bad_sequence = validate_scenario_dict({"schema_version": 2, "id": "bad-sequence", "stage": "source",
                                           "kind": "sequence", "factor_under_test": "source_repeat",
                                           "base_context": {}, "base_inputs": {}, "oracle": {},
                                           "steps": [{"id": "same"}, {"id": "same"}]})
    assert not bad_sequence["ok"]
    non_finite = validate_scenario_dict({"schema_version": 1, "id": "non-finite", "stage": "source",
                                         "context": {"source_weights": {"news": math.inf}},
                                         "inputs": {}, "oracle": {}})
    assert not non_finite["ok"] and any("non-finite" in item["message"] for item in non_finite["errors"])

    source_sequence = preview_scenario(read_scenario("sequence_news_source_repeat"), {"id": "production_default"})
    assert not source_sequence["violations"] and not source_sequence["evaluation"]["quality_failures"]
    assert source_sequence["transitions"][-1]["top1_changed"] is True
    candidate_sequence = preview_scenario(read_scenario("sequence_news_candidate_repeat"), {"id": "production_default"})
    assert candidate_sequence["transitions"][0]["source_score_deltas"]["news"] < 0
    boundary = preview_scenario(read_scenario("sequence_active_bias_boundary"), {"id": "production_default"})
    assert [step["snapshot"]["active_bias"]["applied"] for step in boundary["sequence_steps"]] == [True, False]

    privacy = next(row for row in scenarios if row["id"].startswith("privacy_") and read_scenario(row["id"])["context"]["privacy_state"] == "closed")
    preview = preview_scenario(read_scenario(privacy["id"]), {"id": "production_default"})
    assert "vision" not in [row["source_type"] for row in preview["snapshot"]["ranked_candidates"]]
    assert not preview["violations"]
    assert "source_scores" in preview["snapshot"]
    assert all("score" in info and "candidates" in info for info in preview["snapshot"]["source_scores"].values())

    source = inspect.getsource(adapter)
    assert "_score_candidate" not in source
    assert "build_shadow_recommendation_decision" in source

    observations, feedback = [], []
    for index in range(30):
        turn_id, ts = f"music-{index}", 10_000.0 + index
        observations.append({"ts": ts, "lanlan_name": "trace-a", "turn_id": turn_id,
                             "shadow_selected_source_type": "music", "shadow_selected_score": 0.62,
                             "top_candidates": [{"rank": 1, "id": f"music:{index}", "source_type": "music",
                                                 "family": "music", "topic": "song", "score": 0.62}],
                             "actual_primary_channel": "music", "delivered": True,
                             "matched_actual_material": True, "matched_actual_source": True})
        feedback.append(build_feedback_event(lanlan_name="trace-a", turn_id=turn_id,
                                             event_type="music_played_through", source_type="music", ts=ts + 0.5))
    trace = run_personalization_trace({"scenario_id": "competition_15", "users": [
        {"user_id": "music-lover", "rounds": [{"observations": observations, "feedback": feedback}]},
        {"user_id": "isolated-control", "rounds": []},
        {"user_id": "insufficient", "rounds": [{"observations": observations[:1], "feedback": feedback[:1]}]},
    ]})
    assert trace["production_config_modified"] is False
    assert trace["users"][0]["final_adjustments"]["music"] == 0.02
    assert trace["users"][1]["final_adjustments"] == {}
    assert trace["users"][2]["rounds"][0]["blocked_reason"] == "feedback_sample_count_below_threshold"

    with tempfile.TemporaryDirectory() as td:
        cfg.RECOMMENDATION_RUNS_DIR = Path(td) / "runs"
        cfg.RECOMMENDATION_BASELINES_DIR = Path(td) / "baselines"
        cfg.USER_RECOMMENDATION_SCENARIOS_DIR = Path(td) / "user-scenarios"
        result = run_experiment({"name": "smoke", "scenario_filter": {"ids": [privacy["id"]]},
                                 "baseline_variant": "production_default",
                                 "variants": [{"id": "production_default"}, {"id": "candidate", "source_type_adjustments": {"news": -0.02}}]})
        assert result["scenario_count"] == 1
        assert result["metrics"]["production_default"]["hard_violation_count"] == 0
        assert (cfg.RECOMMENDATION_RUNS_DIR / f"{result['id']}.json").exists()

        # Unfiltered standard suites contain builtins only and produce usable
        # quality metrics from the frozen source oracle.
        standard = run_experiment({"name": "standard", "scenario_filter": {},
                                   "baseline_variant": "production_default",
                                   "variants": [{"id": "production_default"},
                                                {"id": "candidate", "source_type_adjustments": {"news": -0.02}}]})
        full = read_run(standard["id"])
        assert standard["scenario_count"] == len([row for row in scenarios if row["source"] == "builtin"])
        assert standard["metrics"]["production_default"]["hit1"] is not None
        assert standard["metrics"]["production_default"]["ndcg3"] is not None
        assert standard["production_config_modified"] is False
        news_change = next(row for row in standard["weight_changes"]["candidate"]
                           if row["source"] == "news")
        assert news_change["delta"]["tuning_adjustment"] == -0.02
        assert news_change["baseline"]["source_weight"] is not None
        assert news_change["current"]["score"] is not None
        assert standard["comparisons"]["candidate"]["losses"] >= 1
        assert any(change["scenario_id"] == "privacy_04"
                   for change in standard["comparisons"]["candidate"]["top1_changes"])
        privacy_case = next(row for row in full["cases_by_variant"]["candidate"]
                            if row["scenario_id"] == "privacy_04")
        assert privacy_case["evaluation"]["quality_failures"]
        assert standard["execution_status"] == "passed" and standard["contract_status"] == "passed"
        assert standard["data_quality_status"] == "passed" and standard["quality_gate"] == "rejected"
        transparent = standard["metrics"]["production_default"]["transparent_metrics"]
        assert transparent["hit_at_1"]["denominator"] == standard["selection"]["ranking_eligible"]
        assert transparent["positive_case_hit_at_1"] == transparent["hit_at_1"]
        assert transparent["positive_case_ndcg_at_3"] == transparent["ndcg_at_3"]
        assert transparent["decision_accuracy_with_noop"]["denominator"] == 23
        assert set(transparent["gate_confusion_matrix"]) == {"tp", "fp", "tn", "fn"}
        assert standard["selection"]["builtin_selected"] == len(builtins)
        assert standard["selection"]["user_selected"] == 0

        override = read_scenario("competition_15")
        for key in ("_source", "has_builtin", "has_user", "overriding_builtin"):
            override.pop(key, None)
        override["context"]["activity_state"] = "busy"
        save_user_scenario(override)
        isolated = run_experiment({"name": "canonical-isolation", "variants": [{"id": "production_default"}]})
        assert isolated["selection"]["user_selected"] == 0
        assert isolated["input_hash"] == run_experiment({"name": "canonical-isolation-2", "variants": [{"id": "production_default"}]})["input_hash"]
        reproducibility = check_reproducibility({"name": "repro", "variants": [{"id": "production_default"}]})
        assert reproducibility["reproducible"] and all(reproducibility["checks"].values())
        baseline = signoff_canonical_baseline("smoke-baseline")
        assert baseline["ranking_eligible_count"] == baseline["relevance_labeled_count"]
        assert baseline["production_release_approved"] is False
        known_bad = validate_known_regression("smoke-baseline")
        assert known_bad["passed"] and all(known_bad["checks"].values())

    print("P41 RECOMMENDATION TESTBENCH SMOKE OK")
    return 0


if __name__ == "__main__": raise SystemExit(main())
