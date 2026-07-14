import json

from main_logic.proactive_recommendation_feedback import (
    FEEDBACK_LOG_FILENAME,
    append_recommendation_feedback_jsonl,
    build_feedback_event,
    clear_pending_recommendation_feedback,
    load_recommendation_feedback_jsonl,
    music_feedback_event_type,
    note_user_turn_for_feedback,
    register_pending_feedback,
    sanitize_recommendation_feedback_event,
    join_observations_with_feedback,
    summarize_feedback_calibration,
    summarize_recommendation_feedback,
)


def _observation(**overrides):
    base = {
        "ts": 10_000.0,
        "lanlan_name": "neko",
        "turn_id": "turn-1",
        "shadow_selected_source_type": "music",
        "shadow_selected_score": 0.82,
        "top_candidates": [
            {
                "rank": 1,
                "id": "music:1",
                "source_type": "music",
                "family": "music",
                "topic": "Kitchen Song",
                "score": 0.82,
            }
        ],
        "actual_primary_channel": "music",
        "delivered": True,
        "matched_actual_material": True,
        "matched_actual_source": True,
    }
    base.update(overrides)
    return base


def test_feedback_writer_off_does_not_create_file(tmp_path):
    path = tmp_path / FEEDBACK_LOG_FILENAME
    event = build_feedback_event(
        lanlan_name="neko",
        turn_id="turn-1",
        event_type="mini_game_later",
        source_type="mini_game",
    )

    wrote = append_recommendation_feedback_jsonl(event, log_mode="off", path=path)

    assert wrote is False
    assert not path.exists()


def test_feedback_event_sanitizes_sensitive_fields_and_scores_later_positive():
    event = sanitize_recommendation_feedback_event(
        {
            **build_feedback_event(
                lanlan_name="neko",
                turn_id="turn-1",
                event_type="mini_game_later",
                source_type="mini_game",
                metadata={"mini_game_choice": "later", "text": "must-not-leak"},
            ),
            "payload": {"secret": "must-not-leak"},
            "source_links": [{"url": "must-not-leak"}],
            "metadata": {
                "mini_game_choice": "later",
                "raw_text": "must-not-leak",
                "reason": "button",
            },
        }
    )
    dumped = json.dumps(event, ensure_ascii=False)

    assert event["event_type"] == "mini_game_later"
    assert event["report_score_v1"] == 0.2
    assert event["confidence"] == "medium"
    assert "payload" not in event
    assert "source_links" not in event
    assert "must-not-leak" not in dumped


def test_music_feedback_threshold_mapping():
    assert music_feedback_event_type(played_through=True) == "music_played_through"
    assert music_feedback_event_type(started=False) == "music_not_started"
    assert music_feedback_event_type(played_wall_ms=2500) == "music_hard_skip"
    assert music_feedback_event_type(played_wall_ms=8000) == "music_early_close"
    assert music_feedback_event_type(played_wall_ms=20_000, completion_ratio=0.35) == "music_mid_completion"
    assert music_feedback_event_type(played_wall_ms=20_000, completion_ratio=0.72) == "music_high_completion"


def test_user_turn_feedback_respects_privacy_and_records_continue(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="turn-1",
        source_type="news",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
    )

    first = note_user_turn_for_feedback(
        "neko",
        timestamp=130.0,
        had_text=True,
        text_allowed=False,
        text="private text",
    )
    second = note_user_turn_for_feedback(
        "neko",
        timestamp=160.0,
        had_text=True,
        text_allowed=True,
        text="继续聊一下",
    )

    rows = load_recommendation_feedback_jsonl(tmp_path / FEEDBACK_LOG_FILENAME)
    dumped = json.dumps(rows, ensure_ascii=False)
    assert first["event_type"] == "user_reply_fast"
    assert "reply_length" not in first["metadata"]
    assert second["event_type"] == "user_continue"
    assert second["metadata"]["reply_length"] == 5
    assert [row["event_type"] for row in rows] == ["user_reply_fast", "user_continue"]
    assert "private text" not in dumped


def test_feedback_summary_aggregates_scores_and_infers_ignored():
    observations = [
        _observation(turn_id="positive", ts=10_000.0, actual_primary_channel="music"),
        _observation(turn_id="negative", ts=9_990.0, actual_primary_channel="mini_game"),
        _observation(turn_id="ignored", ts=9_000.0, actual_primary_channel="meme"),
    ]
    events = [
        build_feedback_event(
            lanlan_name="neko",
            turn_id="positive",
            event_type="music_played_through",
            source_type="music",
            ts=10_010.0,
        ),
        build_feedback_event(
            lanlan_name="neko",
            turn_id="negative",
            event_type="mini_game_decline",
            source_type="mini_game",
            ts=10_010.0,
        ),
    ]

    summary = summarize_recommendation_feedback(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )

    assert summary["feedback_sample_count"] == 3
    assert summary["positive_rate"] == 0.333
    assert summary["negative_rate"] == 0.667
    assert summary["feedback_missing_count"] == 0
    assert summary["event_type_distribution"] == {
        "ignored": 1,
        "mini_game_decline": 1,
        "music_played_through": 1,
    }
    assert summary["score_by_source_type"]["music"] == 0.9
    assert summary["score_by_source_type"]["mini_game"] == -0.35


def test_feedback_calibration_joins_turns_and_suggests_adjustments():
    observations = [
        _observation(
            turn_id="news-bad",
            ts=10_000.0,
            shadow_selected_source_type="news",
            shadow_selected_score=0.86,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "news:1",
                    "source_type": "news",
                    "family": "web",
                    "topic": "news",
                    "score": 0.86,
                }
            ],
            actual_primary_channel="news",
        ),
        _observation(
            turn_id="music-good",
            ts=9_990.0,
            shadow_selected_source_type="music",
            shadow_selected_score=0.62,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "music:1",
                    "source_type": "music",
                    "family": "music",
                    "topic": "music",
                    "score": 0.62,
                }
            ],
            actual_primary_channel="music",
        ),
        _observation(
            turn_id="missing",
            ts=9_980.0,
            shadow_selected_source_type="meme",
            shadow_selected_score=0.42,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "meme:1",
                    "source_type": "meme",
                    "family": "meme",
                    "topic": "meme",
                    "score": 0.42,
                }
            ],
            delivered=False,
            actual_primary_channel="",
        ),
    ]
    events = [
        build_feedback_event(
            lanlan_name="neko",
            turn_id="news-bad",
            event_type="proactive_disabled_after",
            source_type="news",
            ts=10_010.0,
        ),
        build_feedback_event(
            lanlan_name="neko",
            turn_id="music-good",
            event_type="music_mid_completion",
            source_type="music",
            ts=10_010.0,
        ),
    ]

    joined = join_observations_with_feedback(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )
    calibration = summarize_feedback_calibration(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )

    assert joined[0]["source_type"] == "news"
    assert joined[0]["turn_feedback_score"] == -0.7
    assert joined[0]["feedback_event_types"] == ["proactive_disabled_after"]
    assert joined[2]["feedback_missing"] is True
    assert calibration["sample_count"] == 3
    assert calibration["feedback_joined_count"] == 2
    assert calibration["feedback_missing_count"] == 1
    assert calibration["score_bucket_feedback"]["high"]["average_feedback_score"] == -0.7
    assert calibration["score_bucket_feedback"]["mid"]["average_feedback_score"] == 0.25
    assert calibration["over_scored_sources"] == ["news"]
    assert calibration["under_scored_sources"] == ["music"]
    assert calibration["suggested_weight_adjustments"]["news"] == {
        "adjustment": -0.05,
        "reasons": ["over_scored_high_score_low_feedback"],
    }
    assert calibration["suggested_weight_adjustments"]["music"] == {
        "adjustment": 0.03,
        "reasons": ["under_scored_positive_feedback"],
    }
    assert calibration["active_ready_by_feedback"] is False
    assert "feedback_sample_count_below_threshold" in calibration["active_ready_reasons"]


def test_feedback_calibration_distinguishes_strong_music_from_weak_ignored():
    observations = []
    events = []
    for idx in range(3):
        turn_id = f"music-played-{idx}"
        observations.append(
            _observation(
                turn_id=turn_id,
                ts=10_000.0 - idx,
                shadow_selected_source_type="music",
                shadow_selected_score=0.62,
                top_candidates=[
                    {
                        "rank": 1,
                        "id": f"music:{idx}",
                        "source_type": "music",
                        "family": "music",
                        "topic": "music",
                        "score": 0.62,
                    }
                ],
                actual_primary_channel="music",
            )
        )
        events.append(
            build_feedback_event(
                lanlan_name="neko",
                turn_id=turn_id,
                event_type="music_played_through",
                source_type="music",
                ts=10_010.0,
            )
        )
    for idx in range(4):
        turn_id = f"meme-ignored-{idx}"
        observations.append(
            _observation(
                turn_id=turn_id,
                ts=9_900.0 - idx,
                shadow_selected_source_type="meme",
                shadow_selected_score=0.82,
                top_candidates=[
                    {
                        "rank": 1,
                        "id": f"meme:{idx}",
                        "source_type": "meme",
                        "family": "meme",
                        "topic": "meme",
                        "score": 0.82,
                    }
                ],
                actual_primary_channel="meme",
            )
        )
        events.append(
            build_feedback_event(
                lanlan_name="neko",
                turn_id=turn_id,
                event_type="ignored",
                source_type="meme",
                ts=10_010.0,
            )
        )

    calibration = summarize_feedback_calibration(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )
    dumped = json.dumps(calibration, ensure_ascii=False)

    assert calibration["feedback_signal_summary"]["music"]["played_through_count"] == 3
    assert calibration["feedback_signal_summary"]["music"]["strong_positive_count"] == 3
    assert calibration["feedback_signal_summary"]["music"]["confidence_positive_rate"] == 1.0
    assert calibration["feedback_signal_summary"]["meme"]["ignored_count"] == 4
    assert calibration["source_feedback_pressure"]["meme"]["level"] == "weak_ignored_pressure"
    assert calibration["feedback_actionable_suggestions"]["music"]["adjustment"] == 0.03
    assert calibration["feedback_actionable_suggestions"]["music"]["reasons"] == [
        "strong_music_positive_feedback"
    ]
    assert calibration["feedback_actionable_suggestions"]["meme"]["adjustment"] == 0.0
    assert calibration["feedback_actionable_suggestions"]["meme"]["reasons"] == [
        "weak_ignored_pressure"
    ]
    assert "meme" not in calibration["suggested_weight_adjustments"]
    assert calibration["manual_tuning_preview"]["music"]["preview_adjustment"] == 0.03
    assert calibration["active_ready_by_feedback"] is False
    for forbidden in ("payload", "source_links", "screenshot", "prompt", "raw_data"):
        assert forbidden not in dumped


def test_feedback_calibration_active_ready_when_score_buckets_predict_feedback():
    observations = []
    events = []
    for idx in range(30):
        if idx < 10:
            score = 0.82
            event_type = "user_continue"
        elif idx < 20:
            score = 0.62
            event_type = "user_reply_fast"
        else:
            score = 0.42
            event_type = "user_reply"
        turn_id = f"turn-{idx}"
        observations.append(
            _observation(
                turn_id=turn_id,
                ts=10_000.0 - idx,
                shadow_selected_score=score,
                top_candidates=[
                    {
                        "rank": 1,
                        "id": f"music:{idx}",
                        "source_type": "music",
                        "family": "music",
                        "topic": "music",
                        "score": score,
                    }
                ],
            )
        )
        events.append(
            build_feedback_event(
                lanlan_name="neko",
                turn_id=turn_id,
                event_type=event_type,
                source_type="music",
                ts=10_000.0,
            )
        )

    calibration = summarize_feedback_calibration(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )

    assert calibration["feedback_joined_count"] == 30
    assert calibration["average_feedback_score"] == 0.25
    assert calibration["top1_positive_rate"] == 1.0
    assert calibration["top1_negative_rate"] == 0.0
    assert calibration["score_bucket_feedback"]["high"]["average_feedback_score"] == 0.35
    assert calibration["score_bucket_feedback"]["mid"]["average_feedback_score"] == 0.25
    assert calibration["score_bucket_feedback"]["low"]["average_feedback_score"] == 0.15
    assert calibration["active_ready_by_feedback"] is True
    assert calibration["active_ready_reasons"] == []
