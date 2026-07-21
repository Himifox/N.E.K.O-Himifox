"""P44-F2 pure timing/fatigue association analysis smoke."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_timing_analysis import (
    analyze_timing_fatigue_baseline,
)


def _dataset() -> dict:
    observations, feedback, annotations = [], [], []
    sources = ("music", "news", "vision", "meme")
    activities = ("idle", "chatting", "focused_work")
    for index in range(100):
        delivered = index < 70
        high_fatigue = index % 2 == 1
        should_recommend = not high_fatigue
        turn_id = f"timing-{index}"
        observations.append({
            "turn_id": turn_id,
            "ts": 10_000.0 + index,
            "algorithm_version": "0.8.3:proactive-recommendation-observation-v3",
            "activity_state": activities[index % len(activities)],
            "shadow_selected_source_type": sources[index % len(sources)],
            "delivered": delivered,
            "decision_context": {"timing": {
                "configured_interval_seconds": 10.0 + (index % 3) * 10.0,
                "elapsed_since_last_delivery_seconds": float(index),
                "recent_delivery_count_30m": index % 8,
                "recent_delivery_count_2h": 10 + index % 8,
                "consecutive_unanswered_deliveries": 3 if high_fatigue else 0,
            }},
        })
        annotations.append({"turn_id": turn_id, "should_recommend": should_recommend})
        if delivered and should_recommend:
            feedback.append({
                "turn_id": turn_id,
                "report_score_v1": 0.35 if index % 4 == 0 else 0.25,
                "event_type": "user_reply_fast",
            })
    return {"observations": observations, "feedback": feedback, "annotations": annotations}


def main() -> int:
    dataset = _dataset()
    original = deepcopy(dataset)
    report = analyze_timing_fatigue_baseline(dataset, bootstrap_repetitions=200)
    assert dataset == original
    assert report["input"]["observation_count"] == 100
    assert report["method"]["elapsed_time"] == (
        "continuous seconds; no absolute elapsed-time bucket gate"
    )
    assert report["outcomes"]["false_interruption"]["available"]
    assert report["outcomes"]["missed_opportunity"]["available"]
    assert report["conclusion"]["status"] == "candidate_for_shadow"
    assert report["conclusion"]["candidate_simulation_required"]
    assert "consecutive_unanswered_deliveries" in report["conclusion"][
        "stable_explicit_feedback_features"
    ]
    assert report["production_config_modified"] is False
    assert report["tuning_modified"] is False

    unlabeled = _dataset()
    unlabeled.pop("annotations")
    no_candidate = analyze_timing_fatigue_baseline(
        unlabeled,
        bootstrap_repetitions=200,
    )
    assert no_candidate["conclusion"]["status"] == "no_candidate"
    assert "human_should_recommend_labels_unavailable" in no_candidate["conclusion"][
        "reason_codes"
    ]
    assert not no_candidate["outcomes"]["false_interruption"]["available"]
    assert not no_candidate["outcomes"]["missed_opportunity"]["available"]
    print("P48 RECOMMENDATION TIMING ANALYSIS SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
