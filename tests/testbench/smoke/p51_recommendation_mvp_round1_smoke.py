"""Smoke for the bounded Recommendation MVP four-arm candidate round."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_mvp_round1 import (
    analyze_recommendation_mvp_round1,
)


def _dataset(*, already_correct: bool) -> tuple[dict, dict]:
    observations = []
    annotations = []
    source_pairs = (("news", "music"), ("vision", "meme"))
    for index in range(16):
        bad, good = source_pairs[index % 2]
        turn_id = f"round1-{index}"
        ts = float(index if index < 8 else 10_000 + index)
        bad_score = 0.49 if already_correct else 0.51
        good_score = 0.51 if already_correct else 0.50
        ranked = [
            {"id": f"{bad}:{index}", "source_type": bad, "score": bad_score},
            {"id": f"{good}:{index}", "source_type": good, "score": good_score},
        ]
        if already_correct:
            ranked.reverse()
        observations.append({
            "turn_id": turn_id,
            "ts": ts,
            "algorithm_version": "synthetic-v1",
            "lanlan_name": "smoke",
            "activity_state": "idle",
            "shadow_selected_candidate_id": ranked[0]["id"],
            "shadow_selected_source_type": ranked[0]["source_type"],
            "delivered": True,
        })
        relevance = {
            f"{bad}:{index}": 0,
            f"{good}:{index}": 3,
        }
        annotations.append({
            "turn_id": turn_id,
            "primary_review_status": "accepted",
            "should_recommend": True,
            "relevance": relevance,
            "adjudication_grade": "A",
            "context_for_review": {"candidates": ranked},
        })
    return ({
        "name": "synthetic-round1",
        "observations": observations,
        "quality_preview": {
            "algorithm_versions": {"synthetic-v1": 16},
            "duplicate_turn_ids": [],
            "invalid_observation_indexes": [],
            "mixed_algorithm_versions": False,
        },
    }, {"annotations": annotations})


def main() -> int:
    source, labels = _dataset(already_correct=False)
    original_source = deepcopy(source)
    original_labels = deepcopy(labels)
    candidate = analyze_recommendation_mvp_round1(
        source,
        labels,
        split_index=8,
        minimum_source_candidate_rows=2,
        minimum_relevance_levels=1,
        source_delta_cap=0.02,
    )
    assert source == original_source
    assert labels == original_labels
    assert candidate["input_contract"]["passed"]
    assert candidate["conclusion"]["status"] == "candidate_selected"
    assert candidate["conclusion"]["selected_arm"] == "source_calibration"
    assert candidate["metrics"]["holdout"]["source_calibration"]["all_eligible"][
        "hit_at_1"
    ]["value"] == 1.0
    assert candidate["production_config_modified"] is False
    assert candidate["mvp_modified"] is False
    assert candidate["tuning_modified"] is False

    source, labels = _dataset(already_correct=True)
    baseline = analyze_recommendation_mvp_round1(
        source,
        labels,
        split_index=8,
        minimum_source_candidate_rows=2,
        minimum_relevance_levels=1,
        source_delta_cap=0.02,
    )
    assert baseline["conclusion"]["status"] == "baseline_retained"
    assert baseline["conclusion"]["selected_arm"] == "baseline"

    broken = deepcopy(source)
    broken["quality_preview"]["mixed_algorithm_versions"] = True
    blocked = analyze_recommendation_mvp_round1(
        broken,
        labels,
        split_index=8,
        minimum_source_candidate_rows=2,
        minimum_relevance_levels=1,
    )
    assert blocked["conclusion"]["status"] == "blocked_input_contract"
    print("P51 RECOMMENDATION MVP ROUND1 SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
