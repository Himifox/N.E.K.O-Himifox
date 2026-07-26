from main_logic.proactive_recommendation import PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION
from main_logic.proactive_recommendation_feedback import (
    build_feedback_event,
)
from main_logic.proactive_recommendation_observer import (
    OBSERVATION_LOG_FILENAME,
    append_recommendation_observation_jsonl,
)
from main_logic.proactive_recommendation_tuning import (
    TUNING_FILENAME,
    apply_recommendation_tuning_score,
    evaluate_recommendation_tuning_health,
    extract_auto_safe_feedback_suggestions,
    load_recommendation_tuning,
    maybe_auto_apply_recommendation_tuning_from_logs,
    pause_recommendation_tuning,
    reset_recommendation_tuning,
    resume_recommendation_tuning,
    save_recommendation_tuning,
    sanitize_recommendation_tuning,
)


def _observation(turn_id, *, source_type="music", score=0.82, ts=10_000.0):
    return {
        "ts": ts,
        "lanlan_name": "neko",
        "turn_id": turn_id,
        "algorithm_version": PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
        "decision_stage": "phase1_material",
        "candidate_count": 1,
        "shadow_selected_source_type": source_type,
        "shadow_selected_candidate_id": f"{source_type}:{turn_id}",
        "shadow_selected_score": score,
        "top_candidates": [
            {
                "rank": 1,
                "id": f"{source_type}:{turn_id}",
                "source_type": source_type,
                "family": source_type,
                "topic": source_type,
                "score": score,
            }
        ],
        "actual_primary_channel": source_type,
        "actual_reason_code": "CHAT_DELIVERED",
        "delivered": True,
        "actual_rank": 1,
        "matched_actual_material": True,
        "matched_actual_source": True,
    }


def _append_observation(tmp_path, row):
    append_recommendation_observation_jsonl(
        row,
        log_mode="jsonl",
        path=tmp_path / OBSERVATION_LOG_FILENAME,
    )


def _append_feedback(tmp_path, event):
    path = tmp_path / "proactive_recommendation_feedback.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        import json

        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _seed_auto_apply_logs(tmp_path, *, now=10_000.0):
    for idx in range(25):
        turn_id = f"music-{idx}"
        _append_observation(
            tmp_path,
            _observation(turn_id, source_type="music", score=0.62, ts=now - idx),
        )
        _append_feedback(
            tmp_path,
            build_feedback_event(
                lanlan_name="neko",
                turn_id=turn_id,
                event_type="user_continue",
                source_type="music",
                ts=now,
            ),
        )
    for idx in range(5):
        turn_id = f"news-{idx}"
        _append_observation(
            tmp_path,
            _observation(turn_id, source_type="news", score=0.86, ts=now - 100 - idx),
        )
        _append_feedback(
            tmp_path,
            build_feedback_event(
                lanlan_name="neko",
                turn_id=turn_id,
                event_type="proactive_disabled_after",
                source_type="news",
                ts=now,
            ),
        )


def _seed_feedback_window(tmp_path, rows, *, now=10_000.0):
    for idx, row in enumerate(rows):
        source_type = row.get("source_type", "music")
        turn_id = row.get("turn_id", f"{source_type}-{idx}")
        score = row.get("score", 0.62)
        _append_observation(
            tmp_path,
            _observation(
                turn_id,
                source_type=source_type,
                score=score,
                ts=now - idx,
            ),
        )
        _append_feedback(
            tmp_path,
            build_feedback_event(
                lanlan_name="neko",
                turn_id=turn_id,
                event_type=row.get("event_type", "user_continue"),
                source_type=source_type,
                ts=now,
            ),
        )


def _calibration(*, average=0.2, negative=0.1, high=0.3, joined=30):
    return {
        "sample_count": joined,
        "feedback_joined_count": joined,
        "average_feedback_score": average,
        "top1_positive_rate": 0.6,
        "top1_negative_rate": negative,
        "score_bucket_feedback": {
            "high": {"count": 10, "average_feedback_score": high},
            "mid": {"count": 10, "average_feedback_score": 0.2},
            "low": {"count": 10, "average_feedback_score": 0.1},
        },
        "suggested_weight_adjustments": {},
    }


def test_tuning_load_missing_and_score_application_is_safe(tmp_path):
    tuning = load_recommendation_tuning(config_dir=tmp_path)
    assert tuning["enabled"] is False
    assert tuning["source_type_adjustment"] == {}
    assert tuning["health"]["status"] == "healthy"

    score, adjustment = apply_recommendation_tuning_score(
        0.7,
        "news",
        tuning={
            "enabled": True,
            "mode": "manual",
            "source_type_adjustment": {"news": -0.4, "topic_hook": 0.1},
        },
    )

    assert adjustment == -0.15
    assert score == 0.55
    clamped_score, clamped_adjustment = apply_recommendation_tuning_score(
        0.98,
        "music",
        adjustments={"music": 0.15},
    )
    assert clamped_adjustment == 0.15
    assert clamped_score == 1.0


def test_sanitize_tuning_drops_invalid_sources_and_clamps():
    tuning = sanitize_recommendation_tuning(
        {
            "enabled": True,
            "mode": "auto_safe",
            "source_type_adjustment": {
                "news": -0.4,
                "music": 0.4,
                "topic_hook": 0.1,
            },
            "last_auto_apply": {
                "applied": True,
                "adjustments": {"news": -0.4},
                "reasons": ["over_scored"],
            },
        }
    )

    assert tuning["source_type_adjustment"] == {"news": -0.15, "music": 0.15}
    assert tuning["last_auto_apply"]["adjustments"] == {"news": -0.15}
    assert tuning["health"]["status"] == "healthy"


def test_tuning_health_good_window_resets_bad_count():
    tuning = {
        "enabled": True,
        "mode": "auto_safe",
        "last_calibration": {
            "average_feedback_score": 0.2,
            "top1_negative_rate": 0.1,
        },
        "health": {
            "status": "watch",
            "bad_window_count": 1,
            "good_window_count": 0,
            "pause_reason": "average_feedback_score_drop",
        },
    }

    updated = evaluate_recommendation_tuning_health(
        tuning,
        _calibration(average=0.25, negative=0.08, high=0.3),
        now=10_000.0,
    )

    assert updated["health"]["status"] == "healthy"
    assert updated["health"]["bad_window_count"] == 0
    assert updated["health"]["good_window_count"] == 1
    assert updated["health"]["pause_reason"] is None
    assert updated["health"]["last_evaluation"]["decision"] == "keep"


def test_tuning_health_bad_windows_watch_then_pause():
    baseline = {
        "average_feedback_score": 0.4,
        "top1_negative_rate": 0.05,
    }
    tuning = {
        "enabled": True,
        "mode": "auto_safe",
        "last_calibration": baseline,
    }

    watch = evaluate_recommendation_tuning_health(
        tuning,
        _calibration(average=0.25, negative=0.05, high=0.2),
        now=10_000.0,
    )
    paused = evaluate_recommendation_tuning_health(
        {**watch, "last_calibration": baseline},
        _calibration(average=0.24, negative=0.05, high=0.2),
        now=13_700.0,
    )

    assert watch["health"]["status"] == "watch"
    assert watch["health"]["bad_window_count"] == 1
    assert watch["health"]["pause_reason"] == "average_feedback_score_drop"
    assert watch["health"]["last_evaluation"]["decision"] == "watch"
    assert paused["health"]["status"] == "paused"
    assert paused["health"]["bad_window_count"] == 2
    assert paused["health"]["pause_reason"] == "average_feedback_score_drop"
    assert paused["health"]["paused_until"] == 13_700.0 + 6 * 3600
    assert paused["health"]["last_evaluation"]["decision"] == "pause"


def test_tuning_health_insufficient_feedback_does_not_increment_bad_count():
    tuning = {
        "enabled": True,
        "mode": "auto_safe",
        "last_calibration": {
            "average_feedback_score": 0.4,
            "top1_negative_rate": 0.05,
        },
        "health": {"status": "healthy", "bad_window_count": 1},
    }

    updated = evaluate_recommendation_tuning_health(
        tuning,
        _calibration(average=0.1, negative=0.4, high=-0.1, joined=12),
        now=10_000.0,
    )

    assert updated["health"]["status"] == "healthy"
    assert updated["health"]["bad_window_count"] == 1
    assert updated["health"]["last_evaluation"]["decision"] == "insufficient_feedback_for_health"


def test_tuning_health_expired_pause_returns_to_watch_before_learning():
    tuning = {
        "enabled": True,
        "mode": "auto_safe",
        "last_calibration": {
            "average_feedback_score": 0.2,
            "top1_negative_rate": 0.1,
        },
        "health": {
            "status": "paused",
            "bad_window_count": 2,
            "paused_until": 9_999.0,
            "pause_reason": "average_feedback_score_drop",
        },
    }

    updated = evaluate_recommendation_tuning_health(
        tuning,
        _calibration(average=0.25, negative=0.08, high=0.3),
        now=10_000.0,
    )

    assert updated["health"]["status"] == "watch"
    assert updated["health"]["bad_window_count"] == 2
    assert updated["health"]["paused_until"] is None
    assert updated["health"]["pause_reason"] is None
    assert updated["health"]["last_evaluation"]["decision"] == "pause_expired_watch"


def test_paused_health_blocks_auto_safe_writes_until_next_health_window(tmp_path):
    _seed_auto_apply_logs(tmp_path)
    save_recommendation_tuning(
        {
            "enabled": True,
            "mode": "auto_safe",
            "source_type_adjustment": {},
            "health": {
                "status": "paused",
                "bad_window_count": 2,
                "paused_until": 20_000.0,
                "pause_reason": "average_feedback_score_drop",
            },
        },
        config_dir=tmp_path,
    )

    blocked = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    expired = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=20_001.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert blocked["applied"] is False
    assert blocked["reason"] == "tuning_health_paused"
    assert expired["applied"] is False
    assert expired["reason"] == "tuning_health_watch"
    assert tuning["source_type_adjustment"] == {}
    assert tuning["auto_apply_count"] == 0


def test_pause_and_resume_tuning_are_idempotent(tmp_path):
    paused = pause_recommendation_tuning(
        config_dir=tmp_path,
        now=10_000.0,
        duration_seconds=60,
        reason="unit_test",
    )
    paused_again = pause_recommendation_tuning(
        config_dir=tmp_path,
        now=10_010.0,
        duration_seconds=60,
        reason="unit_test",
    )
    resumed = resume_recommendation_tuning(config_dir=tmp_path, now=10_020.0)
    resumed_again = resume_recommendation_tuning(config_dir=tmp_path, now=10_030.0)

    assert paused["health"]["status"] == "paused"
    assert paused["health"]["paused_until"] == 10_060.0
    assert paused["health"]["pause_reason"] == "unit_test"
    assert paused_again["health"]["status"] == "paused"
    assert resumed["health"]["status"] == "healthy"
    assert resumed["health"]["bad_window_count"] == 0
    assert resumed["health"]["paused_until"] is None
    assert resumed["health"]["pause_reason"] is None
    assert resumed["health"]["last_evaluation"]["decision"] == "manual_resume"
    assert resumed_again["health"]["status"] == "healthy"


def test_extract_auto_safe_feedback_suggestions_uses_strong_signals_only():
    calibration = {
        "feedback_joined_count": 30,
        "average_feedback_score": 0.2,
        "top1_negative_rate": 0.1,
        "score_by_source_type": {"music": 0.6, "meme": -0.05},
        "feedback_signal_summary": {
            "music": {
                "played_through_count": 3,
                "strong_positive_count": 3,
                "high_confidence_negative_count": 0,
            },
            "meme": {
                "ignored_count": 5,
                "weak_negative_count": 5,
                "high_confidence_negative_count": 0,
                "strong_positive_count": 0,
            },
        },
        "suggested_weight_adjustments": {},
        "feedback_actionable_suggestions": {
            "music": {
                "adjustment": 0.03,
                "reasons": ["strong_music_positive_feedback"],
            },
            "meme": {
                "adjustment": -0.05,
                "reasons": ["weak_ignored_pressure"],
            },
        },
        "manual_tuning_preview": {
            "news": {
                "preview_adjustment": -0.05,
                "reasons": ["must_not_be_read"],
            }
        },
    }

    suggestions = extract_auto_safe_feedback_suggestions(calibration)

    assert suggestions == {
        "music": {
            "adjustment": 0.03,
            "reasons": ["strong_music_positive_feedback"],
        }
    }


def test_auto_safe_uses_strong_music_feedback_actionable_suggestion(tmp_path):
    rows = [
        {"source_type": "music", "event_type": "music_played_through"}
        for _ in range(3)
    ] + [
        {"source_type": "music", "event_type": "music_high_completion"}
        for _ in range(27)
    ]
    _seed_feedback_window(tmp_path, rows)

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["applied"] is True
    assert result["adjustments"]["music"] == 0.02
    assert tuning["source_type_adjustment"]["music"] == 0.02
    assert "strong_music_positive_feedback" in tuning["last_auto_apply"]["reasons"]


def test_auto_safe_does_not_downweight_ignored_only_pressure(tmp_path):
    rows = [
        {"source_type": "music", "event_type": "user_continue", "score": 0.62}
        for _ in range(25)
    ] + [
        {"source_type": "meme", "event_type": "ignored", "score": 0.86}
        for _ in range(5)
    ]
    _seed_feedback_window(tmp_path, rows)

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["applied"] is True
    assert "meme" not in result["adjustments"]
    assert "meme" not in tuning["source_type_adjustment"]
    assert "weak_ignored_pressure" not in tuning["last_auto_apply"]["reasons"]


def test_auto_safe_requires_two_high_confidence_negative_events(tmp_path):
    rows = [
        {"source_type": "music", "event_type": "user_continue", "score": 0.62}
        for _ in range(29)
    ] + [
        {
            "source_type": "news",
            "event_type": "proactive_disabled_after",
            "score": 0.86,
        }
    ]
    _seed_feedback_window(tmp_path, rows)

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["applied"] is True
    assert "news" not in result["adjustments"]
    assert "news" not in tuning["source_type_adjustment"]


def test_auto_safe_downweights_repeated_high_confidence_negative_feedback(tmp_path):
    rows = [
        {"source_type": "music", "event_type": "user_continue", "score": 0.62}
        for _ in range(28)
    ] + [
        {
            "source_type": "news",
            "event_type": "proactive_disabled_after",
            "score": 0.86,
        }
        for _ in range(2)
    ]
    _seed_feedback_window(tmp_path, rows)

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["applied"] is True
    assert result["adjustments"]["news"] == -0.02
    assert tuning["source_type_adjustment"]["news"] == -0.02
    assert "high_confidence_negative_feedback" in tuning["last_auto_apply"]["reasons"]


def test_auto_safe_writes_small_adjustment_and_respects_cooldown(tmp_path):
    _seed_auto_apply_logs(tmp_path)

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    second = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_100.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["applied"] is True
    assert result["adjustments"]["news"] == -0.02
    assert result["adjustments"]["music"] == 0.02
    assert second["applied"] is False
    assert second["reason"] == "no_applicable_adjustments"
    assert tuning["source_type_adjustment"]["news"] == -0.02
    assert tuning["source_type_adjustment"]["music"] == 0.02
    assert tuning["auto_apply_count"] == 1
    assert (tmp_path / TUNING_FILENAME).exists()


def test_auto_safe_clamps_cumulative_adjustment(tmp_path):
    _seed_auto_apply_logs(tmp_path)
    save_recommendation_tuning(
        {
            "enabled": True,
            "mode": "auto_safe",
            "source_type_adjustment": {"news": -0.14},
            "source_last_applied_at": {"news": 1.0},
            "rollback": {
                "previous_source_type_adjustment": {},
                "applied": False,
                "reason": None,
            },
        },
        config_dir=tmp_path,
    )

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["applied"] is True
    assert result["adjustments"]["news"] == -0.01
    assert result["adjustments"]["music"] == 0.02
    assert tuning["source_type_adjustment"]["news"] == -0.15


def test_auto_safe_rolls_back_when_feedback_drops(tmp_path):
    _seed_auto_apply_logs(tmp_path)
    save_recommendation_tuning(
        {
            "enabled": True,
            "mode": "auto_safe",
            "source_type_adjustment": {"news": -0.02},
            "last_calibration": {
                "average_feedback_score": 0.5,
                "top1_positive_rate": 0.8,
                "top1_negative_rate": 0.05,
            },
            "rollback": {
                "previous_source_type_adjustment": {},
                "applied": False,
                "reason": None,
            },
        },
        config_dir=tmp_path,
    )

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="auto_safe",
        config_dir=tmp_path,
        now=10_000.0,
    )
    tuning = load_recommendation_tuning(config_dir=tmp_path)

    assert result["rollback_applied"] is True
    assert result["reason"] == "average_feedback_score_drop"
    assert tuning["source_type_adjustment"] == {}
    assert tuning["rollback"]["applied"] is True
    assert tuning["auto_apply_count"] == 0
    assert tuning["health"]["status"] == "watch"
    assert tuning["health"]["bad_window_count"] == 1
    assert tuning["health"]["pause_reason"] == "average_feedback_score_drop"


def test_manual_mode_does_not_auto_write_and_reset_is_idempotent(tmp_path):
    _seed_auto_apply_logs(tmp_path)

    result = maybe_auto_apply_recommendation_tuning_from_logs(
        mode="manual",
        config_dir=tmp_path,
        now=10_000.0,
    )
    reset = reset_recommendation_tuning(config_dir=tmp_path)
    reset_again = reset_recommendation_tuning(config_dir=tmp_path)

    assert result == {"applied": False, "reason": "mode_not_auto_safe"}
    assert reset is True
    assert reset_again is True
    assert not (tmp_path / TUNING_FILENAME).exists()
