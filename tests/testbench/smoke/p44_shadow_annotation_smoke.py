"""P44 Shadow quality, annotation, review, and readiness smoke."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.recommendation_shadow import audit_shadow_dataset, p44_readiness, validate_annotations
from tests.testbench.pipeline.recommendation_annotation_report import build_annotation_report


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
        second_review = {
            "required": index < 20,
            "status": "completed" if index < 20 else "not_required",
            "reviewer_id": "b" if index < 20 else "",
            "reviewed_at": "2026-07-16T00:00:00Z" if index < 20 else "",
            "should_recommend": True if index < 20 else None,
            "relevance": {candidate_id: 3} if index < 20 else {},
            "comment": "",
        }
        annotations.append({"turn_id": turn_id, "should_recommend": True,
                            "acceptable_top1_sources": ["music"], "relevance": {candidate_id: 3},
                            "must_filter_candidate_ids": [], "expected_filter_reasons": {},
                            "interruption_level": "acceptable", "privacy_risk": "none",
                            "score_diagnosis": "reasonable", "issue_layer": "none",
                            "annotator_id": "codex-first-pass-v1",
                            "primary_review_status": "accepted", "primary_reviewer_id": "a",
                            "primary_reviewed_at": "2026-07-16T00:00:00Z",
                            "second_review": second_review,
                            "context_for_review": {
                                "activity": "idle", "delivered": True,
                                "candidates": [{"id": candidate_id, "source_type": "music",
                                                "safe_title": f"track-{index}", "score": 0.6}],
                            },
                            "reviewed": index < 20, "reviewer_id": "b" if index < 20 else ""})
    dataset = {"observations": observations, "feedback": feedback}
    audit = audit_shadow_dataset(dataset)
    assert audit["observation_count"] == 100 and audit["feedback_joined_count"] == 30
    stamped = audit_shadow_dataset({"observations": [
        dict(observations[0], algorithm_version="v2", git_revision="rev-a"),
        dict(observations[1], algorithm_version="v2", git_revision="rev-b"),
    ], "feedback": []})
    assert stamped["algorithm_versions"] == {"v2": 2}
    assert not stamped["mixed_algorithm_versions"]
    assert stamped["git_revisions"] == {"rev-a": 1, "rev-b": 1}
    result = validate_annotations(dataset, annotations)
    assert result["ok"], result["errors"]
    legacy = dict(annotations[0], interruption_level="severe", privacy_risk="suspected",
                  score_diagnosis="not_applicable")
    legacy_result = validate_annotations(dataset, [legacy])
    assert legacy_result["ok"], legacy_result["errors"]
    assert legacy_result["normalized"][0]["interruption_level"] == "interruptive"
    assert legacy_result["normalized"][0]["privacy_risk"] == "medium"
    assert legacy_result["normalized"][0]["score_diagnosis"] == "not_enough_context"
    missing_primary_time = dict(annotations[0], primary_reviewed_at="")
    missing_primary_result = validate_annotations(dataset, [missing_primary_time])
    assert not missing_primary_result["ok"]
    assert any(error["path"].endswith("primary_reviewed_at")
               for error in missing_primary_result["errors"])
    naive_primary_time = dict(annotations[0], primary_reviewed_at="2026-07-16T00:00:00")
    naive_primary_result = validate_annotations(dataset, [naive_primary_time])
    assert not naive_primary_result["ok"]
    assert any("with timezone" in error["message"] for error in naive_primary_result["errors"])
    missing_second_time = dict(annotations[0])
    missing_second_time["second_review"] = dict(annotations[0]["second_review"], reviewed_at="")
    missing_second_result = validate_annotations(dataset, [missing_second_time])
    assert not missing_second_result["ok"]
    assert any(error["path"].endswith("second_review.reviewed_at")
               for error in missing_second_result["errors"])
    ready = p44_readiness(dataset, result["normalized"])
    assert ready["ready_for_weight_candidates"], ready["blockers"]
    report = build_annotation_report({
        "annotations": annotations,
        "quality_preview": {"feedback_joined_count": 30},
    })
    assert report["summary"]["feedback_joined_count"] == 30
    assert report["summary"]["human_confirmed_count"] == 100
    assert report["summary"]["second_review_completed_count"] == 20
    assert report["summary"]["positive_case_hit_at_1"] == {
        "numerator": 100, "denominator": 100, "value": 1.0,
    }
    assert "feedback_joined_count_not_available_or_below_30" not in report["weight_candidate_gate"]["blockers"]
    negative = dict(annotations[0], should_recommend=False,
                    relevance={"music:0": 0})
    negative["context_for_review"] = {
        "activity": "idle", "delivered": True,
        "candidates": [{"id": "music:0", "source_type": "music", "score": 0.6}],
    }
    negative_report = build_annotation_report({
        "annotations": [negative], "quality_preview": {"feedback_joined_count": 30},
    })
    assert negative_report["summary"]["positive_case_hit_at_1"]["denominator"] == 0
    assert negative_report["summary"]["false_interruption_rate"] == {
        "numerator": 1, "denominator": 1, "value": 1.0,
    }
    broken = [dict(annotations[0], relevance={"unknown:1": 3})]
    invalid = validate_annotations(dataset, broken)
    assert not invalid["ok"] and invalid["errors"][0]["path"].endswith("unknown:1")
    incomplete = p44_readiness({"observations": observations[:10], "feedback": []}, [])
    assert not incomplete["ready_for_weight_candidates"]
    assert "observation_count_below_100" in incomplete["blockers"]
    print("P44 SHADOW ANNOTATION SMOKE OK")
    return 0


if __name__ == "__main__": raise SystemExit(main())
