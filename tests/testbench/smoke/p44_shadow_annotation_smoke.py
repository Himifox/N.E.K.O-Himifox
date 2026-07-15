"""P44 Shadow quality, annotation, review, and readiness smoke."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_shadow import audit_shadow_dataset, p44_readiness, validate_annotations


def main() -> int:
    observations = []
    feedback = []
    annotations = []
    for index in range(100):
        turn_id = f"shadow-{index}"
        candidate_id = f"music:{index}"
        observations.append({"turn_id": turn_id, "ts": 10_000.0 + index,
                             "git_revision": "frozen-revision", "activity_state": "idle",
                             "shadow_selected_source_type": "music",
                             "top_candidates": [{"rank": 1, "id": candidate_id,
                                                 "source_type": "music", "score": 0.6}]})
        if index < 30:
            feedback.append({"turn_id": turn_id, "ts": 10_001.0 + index,
                             "event_type": "music_played_through", "source_type": "music"})
        annotations.append({"turn_id": turn_id, "should_recommend": True,
                            "acceptable_top1_sources": ["music"], "relevance": {candidate_id: 3},
                            "must_filter_candidate_ids": [], "expected_filter_reasons": {},
                            "interruption_level": "acceptable", "privacy_risk": "none",
                            "score_diagnosis": "reasonable", "issue_layer": "none",
                            "annotator_id": "a", "reviewed": index < 20,
                            "reviewer_id": "b" if index < 20 else ""})
    dataset = {"observations": observations, "feedback": feedback}
    audit = audit_shadow_dataset(dataset)
    assert audit["observation_count"] == 100 and audit["feedback_joined_count"] == 30
    result = validate_annotations(dataset, annotations)
    assert result["ok"], result["errors"]
    ready = p44_readiness(dataset, result["normalized"])
    assert ready["ready_for_weight_candidates"], ready["blockers"]
    broken = [dict(annotations[0], relevance={"unknown:1": 3})]
    invalid = validate_annotations(dataset, broken)
    assert not invalid["ok"] and invalid["errors"][0]["path"].endswith("unknown:1")
    incomplete = p44_readiness({"observations": observations[:10], "feedback": []}, [])
    assert not incomplete["ready_for_weight_candidates"]
    assert "observation_count_below_100" in incomplete["blockers"]
    print("P44 SHADOW ANNOTATION SMOKE OK")
    return 0


if __name__ == "__main__": raise SystemExit(main())
