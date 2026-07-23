import json

import pytest

from main_logic.proactive_recommendation import PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION

from main_logic.proactive_recommendation_observer import (
    CALIBRATION_SAMPLE_LIMIT,
    CALIBRATION_WINDOW_SECONDS,
    REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH,
    REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH,
    REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH,
    append_recommendation_observation_jsonl,
    get_recommendation_calibration_samples,
    load_recommendation_observations_jsonl,
    sanitize_recommendation_observation,
    sanitize_recommendation_decision_context,
    sanitize_recommendation_feedback_state_preview,
    sanitize_recommendation_review_context,
    select_recommendation_observation_examples,
    summarize_recommendation_calibration,
    summarize_recommendation_observations,
    summarize_recommendation_review_context,
    summarize_recommendation_validation,
    validate_recommendation_review_context,
)


def _observation(**overrides):
    base = {
        "ts": 123.0,
        "lanlan_name": "neko",
        "turn_id": "turn-1",
        "activity_state": "gaming",
        "activity_propensity": "restricted",
        "algorithm_version": PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
        "git_revision": None,
        "recommendation_mode": "active_source",
        "decision_stage": "phase1_material",
        "candidate_count": 2,
        "shadow_selected_source_type": "music",
        "shadow_selected_candidate_id": "music:1",
        "shadow_selected_score": 0.82,
        "top_candidates": [
            {
                "rank": 1,
                "id": "music:1",
                "source_type": "music",
                "family": "music",
                "topic": "Kitchen Song",
                "score": 0.82,
                "payload": {"url": "must-not-leak"},
            }
        ],
        "actual_primary_channel": "music",
        "actual_source_tag": "MUSIC",
        "actual_reason_code": "CHAT_DELIVERED",
        "actual_stage": "delivery",
        "active_channels": ["music"],
        "delivered": True,
        "actual_rank": 1,
        "actual_candidate_score": 0.82,
        "matched_actual_material": True,
        "matched_actual_source": True,
        "active_bias_applied": True,
        "active_preferred_source_type": "music",
        "active_preferred_source_tag": "MUSIC",
        "active_preferred_candidate_id": "music:1",
        "active_bias_fallback_reason": None,
        "active_model_followed_preference": True,
        "payload": {"raw": "must-not-leak"},
        "source_links": [{"url": "must-not-leak"}],
        "raw_data": {"secret": "must-not-leak"},
        "screenshot_b64": "must-not-leak",
    }
    base.update(overrides)
    return base


def test_feedback_state_preview_sanitizer_keeps_only_bounded_aggregates():
    safe = sanitize_recommendation_feedback_state_preview(
        {
            "version": "feedback_state_preview_v2",
            "conversation_acceptance": {
                "temporary": {
                    "ttl_seconds": 7_200,
                    "interest_preview": 0.6,
                    "positive_evidence_count": 3,
                    "negative_evidence_count": 0,
                    "expires_in_seconds": 6_000,
                    "messages": ["private"],
                },
                "persistent": {
                    "min_explicit_evidence": 3,
                    "positive_evidence_count": 3,
                    "negative_evidence_count": 0,
                    "updated_at": 100.0,
                    "acceptance_preview": 0.2,
                    "url": "https://private.example/token=secret",
                },
            },
            "source_affinity": {
                "temporary": {
                    "ttl_seconds": 7_200,
                    "sources": {
                        "music": {
                            "interest_preview": 0.4,
                            "positive_evidence_count": 2,
                            "negative_evidence_count": 0,
                            "expires_in_seconds": 6_000,
                            "reply_latency_seconds": 12.5,
                            "title": "private",
                        }
                    },
                },
                "persistent": {
                    "min_explicit_evidence": 3,
                    "sources": {
                        "music": {
                            "positive_evidence_count": 2,
                            "negative_evidence_count": 0,
                            "updated_at": 100.0,
                            "affinity_preview": 0.0,
                            "url": "https://private.example/token=secret",
                        }
                    },
                },
            },
            "messages": ["private"],
        }
    )

    dumped = json.dumps(safe, ensure_ascii=False)
    assert safe["ranking_consumed"] is False
    assert safe["conversation_acceptance"]["temporary"]["interest_preview"] == 0.6
    assert safe["conversation_acceptance"]["persistent"]["acceptance_preview"] == 0.2
    assert safe["source_affinity"]["temporary"]["sources"]["music"] == {
        "interest_preview": 0.4,
        "positive_evidence_count": 2,
        "negative_evidence_count": 0,
        "expires_in_seconds": 6_000,
    }
    assert "private" not in dumped
    assert "latency" not in dumped
    assert "url" not in dumped.lower()


def test_feedback_state_preview_sanitizer_keeps_v1_read_only():
    safe = sanitize_recommendation_feedback_state_preview(
        {
            "version": "feedback_state_preview_v1",
            "temporary": {
                "ttl_seconds": 7_200,
                "sources": {
                    "music": {
                        "interest_preview": 0.5,
                        "positive_evidence_count": 2,
                        "negative_evidence_count": 0,
                        "expires_in_seconds": 60,
                    }
                },
            },
            "persistent": {
                "min_explicit_evidence": 3,
                "sources": {},
            },
        }
    )

    assert safe["version"] == "feedback_state_preview_v1"
    assert safe["ranking_consumed"] is False
    assert safe["temporary"]["sources"]["music"]["interest_preview"] == 0.5


def test_writer_off_does_not_create_file(tmp_path):
    path = tmp_path / "observations.jsonl"

    wrote = append_recommendation_observation_jsonl(
        _observation(),
        log_mode="off",
        path=path,
    )

    assert wrote is False
    assert not path.exists()


def test_writer_jsonl_appends_and_creates_parent(tmp_path):
    path = tmp_path / "nested" / "observations.jsonl"

    wrote = append_recommendation_observation_jsonl(
        _observation(),
        log_mode="jsonl",
        path=path,
    )

    assert wrote is True
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert payload["lanlan_name"] == "neko"
    assert payload["active_bias_applied"] is True
    assert payload["active_model_followed_preference"] is True
    assert payload["top_candidates"][0]["source_type"] == "music"
    assert payload["algorithm_version"] == PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION


def test_writer_rejects_observation_without_turn_id(tmp_path):
    path = tmp_path / "observations.jsonl"

    wrote = append_recommendation_observation_jsonl(
        _observation(turn_id=None),
        log_mode="jsonl",
        path=path,
    )

    assert wrote is False
    assert not path.exists()


@pytest.mark.parametrize(
    "activity_state",
    ["gaming", "busy", "away", "focused_work", "unknown"],
)
def test_activity_and_algorithm_fields_survive_sanitize_and_jsonl_round_trip(
    tmp_path,
    activity_state,
):
    path = tmp_path / f"{activity_state}.jsonl"
    observation = _observation(
        activity_state=activity_state,
        activity_propensity="restricted" if activity_state != "unknown" else "unknown",
    )

    safe = sanitize_recommendation_observation(observation)
    wrote = append_recommendation_observation_jsonl(
        observation,
        log_mode="jsonl",
        path=path,
    )
    rows = load_recommendation_observations_jsonl(path)

    assert wrote is True
    assert safe["activity_state"] == activity_state
    assert rows[0]["activity_state"] == activity_state
    assert rows[0]["activity_propensity"] == observation["activity_propensity"]
    assert rows[0]["algorithm_version"] == PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION


def test_sanitize_observation_drops_payload_source_links_and_raw_fields():
    safe = sanitize_recommendation_observation(_observation())
    dumped = json.dumps(safe, ensure_ascii=False)

    assert "payload" not in safe
    assert "source_links" not in safe
    assert "raw_data" not in safe
    assert "screenshot_b64" not in safe
    assert "must-not-leak" not in dumped
    assert set(safe["top_candidates"][0]) == {
        "rank",
        "id",
        "source_type",
        "family",
        "topic_usable",
        "score",
    }
    assert safe["top_candidates"][0]["topic_usable"] is True


def test_sanitize_observation_never_persists_candidate_topic_or_nested_context():
    safe = sanitize_recommendation_observation(
        _observation(
            top_candidates=[
                {
                    "rank": 1,
                    "id": "personal:1",
                    "source_type": "personal",
                    "family": "personal",
                    "topic": "private personal dynamic must-not-leak",
                    "score": 0.88,
                    "window_title": "private window must-not-leak",
                    "raw_text": "private chat must-not-leak",
                    "url": "https://example.test/?token=must-not-leak",
                }
            ]
        )
    )
    dumped = json.dumps(safe, ensure_ascii=False)

    assert safe["top_candidates"][0]["topic_usable"] is True
    assert "topic" not in safe["top_candidates"][0]
    assert "must-not-leak" not in dumped
    assert "window_title" not in dumped
    assert "raw_text" not in dumped
    assert "url" not in dumped


def test_decision_context_sanitizer_keeps_only_bounded_timing_features():
    safe = sanitize_recommendation_decision_context(
        {
            "timing": {
                "configured_interval_seconds": "300",
                "elapsed_since_last_delivery_seconds": 12.34567,
                "recent_delivery_count_30m": "2",
                "recent_delivery_count_2h": -3,
                "consecutive_unanswered_deliveries": True,
                "private_text": "must-not-leak",
            },
            "raw_context": {"messages": ["must-not-leak"]},
        }
    )

    assert safe == {
        "timing": {
            "configured_interval_seconds": 300.0,
            "elapsed_since_last_delivery_seconds": 12.346,
            "recent_delivery_count_30m": 2,
            "recent_delivery_count_2h": 0,
            "consecutive_unanswered_deliveries": 0,
        }
    }
    assert "must-not-leak" not in json.dumps(safe, ensure_ascii=False)


def test_review_context_sanitizer_removes_forbidden_fields_urls_and_bounds_text():
    raw = {
        "schema_version": 99,
        "candidate_labels": [
            {
                "id": "music:1",
                "source_type": "music",
                "safe_title": "T" * 200 + " https://private.test/song?token=secret",
                "safe_summary": "S" * 400 + " token=secret",
                "score": 0.82,
                "url": "https://private.test/?cookie=secret",
                "payload": {"cookie": "secret"},
            }
        ],
        "activity_state": "focused_work",
        "delivered_excerpt": "D" * 300 + " https://private.test/?token=secret",
        "redaction_notes": ["screen_text_truncated"],
        "screenshot_b64": "must-not-leak",
        "chat_text": "must-not-leak",
    }

    safe = sanitize_recommendation_review_context(raw)
    dumped = json.dumps(safe, ensure_ascii=False)

    assert safe["schema_version"] == 1
    assert len(safe["candidate_labels"][0]["safe_title"]) <= REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH
    assert len(safe["candidate_labels"][0]["safe_summary"]) <= REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH
    assert len(safe["delivered_excerpt"]) <= REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH
    assert "url_removed" in safe["redaction_notes"]
    assert "text_truncated" in safe["redaction_notes"]
    assert "must-not-leak" not in dumped
    assert "private.test" not in dumped
    assert "token=secret" not in dumped


def test_review_context_validator_requires_context_and_candidate_alignment():
    observation = _observation()

    missing = validate_recommendation_review_context(observation)
    misaligned = validate_recommendation_review_context(
        {
            **observation,
            "review_context": {
                "schema_version": 1,
                "candidate_labels": [
                    {
                        "id": "news:wrong",
                        "source_type": "news",
                        "safe_title": "safe",
                        "safe_summary": "safe",
                        "score": 0.5,
                    }
                ],
                "activity_state": "idle",
                "delivered_excerpt": "safe",
                "redaction_notes": [],
            },
        }
    )

    assert missing == {
        "valid": False,
        "annotation_ready": False,
        "issues": ["missing_review_context"],
    }
    assert misaligned["annotation_ready"] is False
    assert "review_context_candidate_alignment_mismatch" in misaligned["issues"]


def test_sanitized_review_context_is_annotation_ready_and_raw_forbidden_context_is_not():
    observation = _observation()
    raw_context = {
        "candidate_labels": [
            {
                "id": "music:1",
                "source_type": "music",
                "safe_title": "Kitchen Song",
                "safe_summary": "short summary",
                "score": 0.82,
                "url": "https://example.test/?token=secret",
            }
        ],
        "activity_state": "focused_work",
        "delivered_excerpt": "short excerpt",
        "redaction_notes": [],
    }
    raw_result = validate_recommendation_review_context(
        {**observation, "review_context": raw_context}
    )
    safe_observation = sanitize_recommendation_observation(
        {**observation, "review_context": raw_context}
    )
    safe_result = validate_recommendation_review_context(safe_observation)

    assert raw_result["annotation_ready"] is False
    assert "review_context_forbidden_fields" in raw_result["issues"]
    assert "review_context_url_present" in raw_result["issues"]
    assert safe_result == {"valid": True, "annotation_ready": True, "issues": []}


def test_review_context_summary_blocks_rows_without_safe_context():
    ready = sanitize_recommendation_observation(
        {
            **_observation(),
            "review_context": {
                "candidate_labels": [
                    {
                        "id": "music:1",
                        "source_type": "music",
                        "safe_title": "Kitchen Song",
                        "safe_summary": "short summary",
                        "score": 0.82,
                    }
                ],
                "activity_state": "idle",
                "delivered_excerpt": "short excerpt",
                "redaction_notes": [],
            },
        }
    )

    summary = summarize_recommendation_review_context([ready, _observation(turn_id="blocked")])

    assert summary == {
        "sample_count": 2,
        "review_context_present_count": 1,
        "annotation_ready_count": 1,
        "annotation_blocked_count": 1,
        "issue_distribution": {"missing_review_context": 1},
    }


def test_writer_rotates_when_file_exceeds_threshold(tmp_path):
    path = tmp_path / "observations.jsonl"
    path.write_text("old row\n" * 5, encoding="utf-8")

    append_recommendation_observation_jsonl(
        _observation(turn_id="turn-new"),
        log_mode="jsonl",
        path=path,
        rotate_bytes=1,
    )

    rotated = path.parent / (path.name + ".1")
    assert rotated.exists()
    assert "old row" in rotated.read_text(encoding="utf-8")
    rows = load_recommendation_observations_jsonl(path)
    assert rows[0]["turn_id"] == "turn-new"


def test_summary_computes_rates_ranks_and_high_score_passes():
    rows = [
        _observation(
            turn_id="delivered-match",
            matched_actual_source=True,
            matched_actual_material=True,
            actual_rank=1,
        ),
        _observation(
            turn_id="delivered-miss",
            shadow_selected_source_type="meme",
            shadow_selected_score=0.44,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "meme:1",
                    "source_type": "meme",
                    "family": "meme",
                    "topic": "meme",
                    "score": 0.44,
                }
            ],
            matched_actual_source=False,
            matched_actual_material=False,
            actual_rank=3,
        ),
        _observation(
            turn_id="pass-high-score",
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            actual_rank=None,
            shadow_selected_score=0.91,
        ),
    ]

    summary = summarize_recommendation_observations(rows, high_score_threshold=0.75)

    assert summary["total"] == 3
    assert summary["delivered_count"] == 2
    assert summary["pass_count"] == 1
    assert summary["source_match_rate"] == 0.5
    assert summary["material_match_rate"] == 0.5
    assert summary["average_actual_rank"] == 2.0
    assert summary["shadow_top1_by_source_type"] == {"meme": 1, "music": 2}
    assert summary["pass_high_score_count"] == 1


def test_calibration_samples_filter_recent_hour_and_limit_to_latest_fifty():
    now = 10_000.0
    old_rows = [
        _observation(turn_id=f"old-{idx}", ts=now - CALIBRATION_WINDOW_SECONDS - idx - 1)
        for idx in range(5)
    ]
    recent_rows = [
        _observation(turn_id=f"recent-{idx}", ts=now - 100 + idx)
        for idx in range(60)
    ]

    samples = get_recommendation_calibration_samples(old_rows + recent_rows, now=now)

    assert len(samples) == CALIBRATION_SAMPLE_LIMIT
    assert samples[0]["turn_id"] == "recent-10"
    assert samples[-1]["turn_id"] == "recent-59"
    assert all(now - row["ts"] <= CALIBRATION_WINDOW_SECONDS for row in samples)


def test_calibration_reports_active_ready_when_fixed_window_thresholds_pass():
    rows = [
        _observation(turn_id=f"ready-{idx}", ts=10_000.0 - idx, actual_rank=1)
        for idx in range(30)
    ]

    calibration = summarize_recommendation_calibration(rows, now=10_000.0)

    assert calibration["sample_count"] == 30
    assert calibration["sample_window_seconds"] == CALIBRATION_WINDOW_SECONDS
    assert calibration["sample_limit"] == CALIBRATION_SAMPLE_LIMIT
    assert calibration["source_match_rate"] == 1.0
    assert calibration["material_match_rate"] == 1.0
    assert calibration["average_actual_rank"] == 1.0
    assert calibration["pass_high_score_rate"] == 0.0
    assert calibration["active_ready"] is True
    assert calibration["active_ready_reasons"] == []
    assert calibration["calibration_issues"] == []


def test_calibration_reports_drift_and_pass_gate_conflict():
    delivered_misses = [
        _observation(
            turn_id=f"miss-{idx}",
            ts=10_000.0 - idx,
            actual_rank=3,
            matched_actual_source=False,
            matched_actual_material=False,
        )
        for idx in range(25)
    ]
    pass_high_scores = [
        _observation(
            turn_id=f"pass-{idx}",
            ts=9_900.0 - idx,
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            actual_rank=None,
            shadow_selected_score=0.9,
        )
        for idx in range(5)
    ]

    calibration = summarize_recommendation_calibration(
        delivered_misses + pass_high_scores,
        now=10_000.0,
    )

    assert calibration["sample_count"] == 30
    assert calibration["active_ready"] is False
    assert calibration["pass_high_score_rate"] == 0.167
    assert calibration["calibration_issues"] == [
        "source_selection_drift",
        "material_ranking_drift",
        "ranking_order_drift",
        "pass_gate_conflict",
    ]


def test_calibration_requires_enough_recent_samples():
    rows = [
        _observation(turn_id=f"few-{idx}", ts=10_000.0 - idx)
        for idx in range(5)
    ]

    calibration = summarize_recommendation_calibration(rows, now=10_000.0)

    assert calibration["active_ready"] is False
    assert "low_sample_count" in calibration["calibration_issues"]


def test_validation_classifies_drift_pass_overuse_and_low_quality_examples():
    now = 10_000.0
    rows = [
        _observation(
            turn_id="source-drift",
            ts=now - 1,
            shadow_selected_source_type="meme",
            matched_actual_source=False,
            matched_actual_material=False,
            actual_rank=3,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "meme:1",
                    "source_type": "meme",
                    "family": "meme",
                    "topic": "meme topic",
                    "score": 0.82,
                }
            ],
        ),
        _observation(
            turn_id="material-drift",
            ts=now - 2,
            matched_actual_source=True,
            matched_actual_material=False,
            actual_rank=2,
        ),
        _observation(
            turn_id="pass-conflict",
            ts=now - 3,
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            actual_rank=None,
            shadow_selected_score=0.91,
        ),
        _observation(
            turn_id="low-quality",
            ts=now - 4,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "music:bad",
                    "source_type": "music",
                    "family": "music",
                    "topic": "",
                    "score": 0.82,
                    "payload": {"secret": "must-not-leak"},
                }
            ],
        ),
        _observation(turn_id="music-overuse-1", ts=now - 5),
        _observation(turn_id="music-overuse-2", ts=now - 6),
    ]

    validation = summarize_recommendation_validation(rows, now=now)
    dumped = json.dumps(validation, ensure_ascii=False)

    assert validation["sample_count"] == 6
    assert validation["issues"] == [
        "source_drift",
        "material_drift",
        "pass_conflict",
        "source_overuse",
        "candidate_overuse",
        "low_quality_top1",
    ]
    assert validation["issue_counts"] == {
        "source_drift": 1,
        "material_drift": 1,
        "pass_conflict": 1,
        "source_overuse": 1,
        "candidate_overuse": 1,
        "low_quality_top1": 1,
    }
    assert validation["dominant_source_type"] == "music"
    assert validation["dominant_candidate_id"] == "music:1"
    assert validation["rates"]["pass_conflict"] == 0.167
    assert validation["rates"]["candidate_overuse"] == 0.667
    assert validation["examples"]["pass_conflict"][0]["turn_id"] == "pass-conflict"
    assert validation["examples"]["candidate_overuse"]
    assert validation["examples"]["low_quality_top1"][0]["turn_id"] == "low-quality"
    assert {
        item["target"]
        for item in validation["suggested_weight_adjustments"]
    } >= {
        "context_match",
        "source_quality",
        "interruption_cost",
        "diversity_penalty",
        "source_type.music",
    }
    assert "payload" not in dumped
    assert "must-not-leak" not in dumped


def test_validation_empty_and_stale_samples_are_stable():
    now = 10_000.0
    rows = [
        _observation(
            turn_id="stale",
            ts=now - CALIBRATION_WINDOW_SECONDS - 1,
            matched_actual_source=False,
        )
    ]

    validation = summarize_recommendation_validation(rows, now=now)

    assert validation["sample_count"] == 0
    assert validation["issues"] == []
    assert validation["issue_counts"] == {
        "source_drift": 0,
        "material_drift": 0,
        "pass_conflict": 0,
        "source_overuse": 0,
        "candidate_overuse": 0,
        "low_quality_top1": 0,
    }
    assert validation["suggested_weight_adjustments"] == []
    assert validation["examples"] == {
        "source_drift": [],
        "material_drift": [],
        "pass_conflict": [],
        "source_overuse": [],
        "candidate_overuse": [],
        "low_quality_top1": [],
    }


def test_examples_prioritize_mismatch_and_high_score_passes_without_sensitive_fields():
    rows = [
        _observation(turn_id="boring-match", ts=1.0),
        _observation(
            turn_id="pass-high-score",
            ts=2.0,
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            shadow_selected_score=0.91,
        ),
        _observation(
            turn_id="material-mismatch",
            ts=3.0,
            matched_actual_material=False,
            actual_rank=2,
        ),
    ]

    examples = select_recommendation_observation_examples(rows, high_score_threshold=0.75)
    dumped = json.dumps(examples, ensure_ascii=False)

    assert [item["turn_id"] for item in examples[:2]] == [
        "material-mismatch",
        "pass-high-score",
    ]
    assert examples[1]["reason_code"] == "PASS_MODEL_PASS"
    assert "actual_reason_code" not in examples[1]
    assert "payload" not in dumped
    assert "source_links" not in dumped
    assert "raw_data" not in dumped
    assert "screenshot_b64" not in dumped


def test_examples_clamp_to_internal_maximum():
    rows = [
        _observation(
            turn_id=f"row-{idx}",
            ts=float(idx),
            matched_actual_material=False,
            actual_rank=idx,
        )
        for idx in range(30)
    ]

    examples = select_recommendation_observation_examples(rows, limit=999)

    assert len(examples) == 20
