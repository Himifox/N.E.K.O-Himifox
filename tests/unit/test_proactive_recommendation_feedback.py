import json
from pathlib import Path

from main_logic.proactive_recommendation_feedback import (
    FEEDBACK_LOG_FILENAME,
    append_recommendation_feedback_jsonl,
    build_feedback_event,
    build_reward_score_v2_preview,
    clear_pending_recommendation_feedback,
    join_observations_with_reward_score_v2_preview,
    load_recommendation_feedback_jsonl,
    music_feedback_event_type,
    note_user_turn_for_feedback,
    record_feedback_event,
    record_feedback_event_with_status,
    register_pending_feedback,
    register_pending_feedback_from_observation,
    sanitize_feedback_metadata,
    sanitize_recommendation_feedback_event,
    join_observations_with_feedback,
    summarize_feedback_calibration,
    summarize_recommendation_feedback,
    summarize_reward_score_v2_preview,
)
from main_logic.proactive_recommendation_feedback_state import (
    FEEDBACK_STATE_PREVIEW_FILENAME,
    LEGACY_FEEDBACK_STATE_PREVIEW_FILENAME,
    TEMPORARY_INTEREST_TTL_SECONDS,
    clear_temporary_feedback_state_preview,
    get_feedback_state_preview,
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


def test_reward_score_v2_preview_keeps_reply_speed_neutral_and_deduplicated():
    fast_reply = build_feedback_event(
        lanlan_name="neko",
        turn_id="turn-1",
        event_type="user_reply_fast",
        source_type="music",
    )
    slow_reply = build_feedback_event(
        lanlan_name="neko",
        turn_id="turn-2",
        event_type="user_reply",
        source_type="music",
    )
    continued = build_feedback_event(
        lanlan_name="neko",
        turn_id="turn-1",
        event_type="user_continue",
        source_type="music",
    )

    fast = build_reward_score_v2_preview([fast_reply])
    slow = build_reward_score_v2_preview([slow_reply])
    combined = build_reward_score_v2_preview([fast_reply, fast_reply, continued])

    assert fast["reward_score_v2_preview"] == 0.2
    assert slow["reward_score_v2_preview"] == 0.2
    assert fast["components"]["relative_speed"] == 0.0
    assert slow["components"]["relative_speed"] == 0.0
    assert fast["relative_speed_status"] == "pending_personal_baseline"
    assert combined["reward_score_v2_preview"] == 0.55
    assert combined["event_types"] == ["user_reply_fast", "user_continue"]
    assert combined["ranking_consumed"] is False
    assert combined["tuning_consumed"] is False


def test_reward_score_v2_preview_treats_technical_failures_as_zero():
    for event_type in ("music_error", "autoplay_blocked"):
        event = build_feedback_event(
            lanlan_name="neko",
            turn_id=f"turn-{event_type}",
            event_type=event_type,
            source_type="music",
        )

        preview = build_reward_score_v2_preview([event])

        assert preview["reward_score_v2_preview"] == 0.0
        assert preview["components"]["consumption"] == 0.0
        assert preview["technical_zero_event_types"] == [event_type]


def test_reward_score_v2_preview_does_not_score_unknown_events():
    event = build_feedback_event(
        lanlan_name="neko",
        turn_id="turn-unknown",
        event_type="future_unknown_event",
        source_type="music",
    )

    preview = build_reward_score_v2_preview([event])

    assert preview["reward_score_v2_preview"] is None
    assert preview["recognized_event_types"] == []
    assert preview["unknown_event_types"] == ["future_unknown_event"]


def test_reward_score_v2_preview_requires_valid_delivery_attribution():
    observations = [
        _observation(
            turn_id="good",
            ts=9_999.0,
            shadow_selected_candidate_id="music:1",
        ),
        _observation(
            turn_id="technical",
            ts=9_998.0,
            shadow_selected_candidate_id="music:1",
        ),
        _observation(
            turn_id="source-mismatch",
            ts=9_997.0,
            shadow_selected_source_type="news",
            actual_primary_channel="news",
            shadow_selected_candidate_id="news:1",
        ),
        _observation(
            turn_id="inferred",
            ts=9_000.0,
            shadow_selected_source_type="meme",
            actual_primary_channel="meme",
            shadow_selected_candidate_id="meme:1",
        ),
    ]
    events = [
        build_feedback_event(
            lanlan_name="neko",
            turn_id="good",
            event_type="user_reply",
            source_type="music",
            candidate_id="music:1",
            ts=10_000.0,
        ),
        build_feedback_event(
            lanlan_name="neko",
            turn_id="good",
            event_type="user_continue",
            source_type="music",
            candidate_id="music:1",
            ts=10_001.0,
        ),
        build_feedback_event(
            lanlan_name="neko",
            turn_id="technical",
            event_type="music_error",
            source_type="music",
            candidate_id="music:1",
            ts=10_000.0,
        ),
        build_feedback_event(
            lanlan_name="neko",
            turn_id="source-mismatch",
            event_type="user_reply",
            source_type="music",
            ts=10_000.0,
        ),
    ]

    joined = join_observations_with_reward_score_v2_preview(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )
    summary = summarize_reward_score_v2_preview(
        observations,
        events,
        now=10_000.0,
        window_seconds=3600,
        sample_limit=50,
    )

    by_turn = {row["turn_id"]: row for row in joined}
    assert by_turn["good"]["reward_score_v2_preview"] == 0.55
    assert by_turn["technical"]["reward_score_v2_preview"] == 0.0
    assert by_turn["source-mismatch"]["reward_score_v2_preview"] is None
    assert by_turn["source-mismatch"]["attribution_issue"] == "source_mismatch"
    assert by_turn["inferred"]["reward_score_v2_preview"] == -0.05
    assert by_turn["inferred"]["feedback_inferred"] is True
    assert summary["reward_scored_count"] == 3
    assert summary["explicit_reward_scored_count"] == 2
    assert summary["inferred_reward_scored_count"] == 1
    assert summary["feedback_joined_count"] == 2
    assert summary["feedback_inferred_count"] == 1
    assert summary["attribution_issue_distribution"] == {"source_mismatch": 1}
    assert summary["average_reward_score_v2_preview"] == 0.275
    assert summary["average_all_reward_score_v2_preview"] == 0.167
    assert summary["average_inferred_reward_score_v2_preview"] == -0.05
    assert summary["inferred_ignored_reported_separately"] is True
    assert summary["relative_speed_neutral_count"] == 1
    assert summary["technical_zero_event_count"] == 1
    assert summary["ranking_consumed"] is False


def test_reward_score_v2_preview_uses_point_in_time_personal_reply_speed():
    latencies = [100.0, 105.0, 110.0, 115.0, 120.0, 70.0, 130.0]
    observations = [
        _observation(turn_id=f"reply-{index}", ts=9_000.0 + index)
        for index in range(len(latencies))
    ]
    events = [
        build_feedback_event(
            lanlan_name="neko",
            turn_id=f"reply-{index}",
            event_type="user_reply",
            source_type="music",
            metadata={"reply_latency_seconds": latency},
            ts=9_100.0 + index,
        )
        for index, latency in enumerate(latencies)
    ]

    joined = join_observations_with_reward_score_v2_preview(
        observations,
        events,
        now=10_000.0,
    )
    summary = summarize_reward_score_v2_preview(
        observations,
        events,
        now=10_000.0,
    )
    by_turn = {row["turn_id"]: row for row in joined}

    assert by_turn["reply-4"]["relative_speed_status"] == (
        "insufficient_personal_baseline"
    )
    assert by_turn["reply-4"]["relative_speed_baseline_sample_count"] == 4
    assert by_turn["reply-5"]["relative_speed_status"] == "baseline_ready_bonus"
    assert 0.2 < by_turn["reply-5"]["reward_score_v2_preview"] <= 0.25
    assert by_turn["reply-6"]["relative_speed_status"] == (
        "baseline_ready_no_bonus"
    )
    assert by_turn["reply-6"]["reward_score_v2_preview"] == 0.2
    assert summary["personal_reply_speed_baseline_ready_count"] == 2
    assert summary["relative_speed_bonus_count"] == 1
    assert summary["ranking_consumed"] is False
    assert summary["personalization_state_consumed"] is False


def test_reward_score_v2_preview_is_not_consumed_by_runtime_policy():
    project_root = Path(__file__).parents[2]
    policy_sources = (
        project_root / "main_logic" / "proactive_recommendation.py",
        project_root / "main_logic" / "proactive_recommendation_tuning.py",
        project_root / "main_routers" / "system_router" / "proactive_chat_flow.py",
    )

    for source_path in policy_sources:
        assert "reward_score_v2_preview" not in source_path.read_text(encoding="utf-8")


def test_shadow_feedback_state_separates_conversation_from_source_affinity(tmp_path):
    clear_temporary_feedback_state_preview()

    for index in range(3):
        register_pending_feedback(
            lanlan_name="neko",
            turn_id=f"state-{index}",
            source_type="music",
            candidate_id=f"music:{index}",
            delivered_at=1_000.0 + index,
            log_mode="jsonl",
            config_dir=tmp_path,
            recommendation_mode="shadow",
        )
        record_feedback_event(
            lanlan_name="neko",
            turn_id=f"state-{index}",
            event_type="user_reply",
            ts=1_010.0 + index,
        )
        if index == 0:
            record_feedback_event(
                lanlan_name="neko",
                turn_id="state-0",
                event_type="user_continue",
                ts=1_011.0,
            )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=1_020.0)
    temporary = preview["conversation_acceptance"]["temporary"]
    persistent = preview["conversation_acceptance"]["persistent"]
    stored = json.loads(
        (tmp_path / FEEDBACK_STATE_PREVIEW_FILENAME).read_text(encoding="utf-8")
    )

    assert temporary["interest_preview"] == 0.95
    assert temporary["positive_evidence_count"] == 4
    assert persistent["positive_evidence_count"] == 3
    assert persistent["negative_evidence_count"] == 0
    assert persistent["acceptance_preview"] == 0.2
    assert preview["source_affinity"]["temporary"]["sources"] == {}
    assert preview["source_affinity"]["persistent"]["sources"] == {}
    assert stored["schema_version"] == 2
    assert set(stored["conversation_acceptance"]) == {
        "positive_evidence_count",
        "negative_evidence_count",
        "updated_at",
    }
    dumped = json.dumps(stored, ensure_ascii=False).lower()
    for forbidden in ("turn_id", "reply_latency_seconds", "title", "url"):
        assert forbidden not in dumped

    expired = get_feedback_state_preview(
        config_dir=tmp_path,
        now=1_020.0 + TEMPORARY_INTEREST_TTL_SECONDS,
    )
    assert expired["conversation_acceptance"]["temporary"]["interest_preview"] == 0.0
    assert expired["conversation_acceptance"]["persistent"]["acceptance_preview"] == 0.2


def test_shadow_music_material_events_update_only_verified_source_affinity(tmp_path):
    clear_temporary_feedback_state_preview()
    for index in range(3):
        register_pending_feedback(
            lanlan_name="neko",
            turn_id=f"music-state-{index}",
            source_type="music",
            candidate_id=f"music:{index}",
            delivered_at=1_000.0 + index,
            log_mode="jsonl",
            config_dir=tmp_path,
            recommendation_mode="shadow",
        )
        record_feedback_event(
            lanlan_name="neko",
            turn_id=f"music-state-{index}",
            event_type="music_played_through",
            ts=1_010.0 + index,
        )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=1_020.0)
    temporary = preview["source_affinity"]["temporary"]["sources"]["music"]
    persistent = preview["source_affinity"]["persistent"]["sources"]["music"]

    assert temporary["interest_preview"] == 1.0
    assert temporary["positive_evidence_count"] == 3
    assert persistent["positive_evidence_count"] == 3
    assert persistent["affinity_preview"] == 0.2
    assert preview["conversation_acceptance"]["temporary"]["interest_preview"] == 0.0
    assert preview["conversation_acceptance"]["persistent"]["acceptance_preview"] == 0.0


def test_shadow_turn_can_update_conversation_and_source_groups_independently(tmp_path):
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="conversation-and-music",
        source_type="music",
        candidate_id="music:both",
        delivered_at=1_000.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="conversation-and-music",
        event_type="user_reply",
        ts=1_010.0,
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="conversation-and-music",
        event_type="music_played_through",
        ts=1_011.0,
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=1_020.0)
    assert preview["conversation_acceptance"]["persistent"]["positive_evidence_count"] == 1
    assert (
        preview["source_affinity"]["persistent"]["sources"]["music"]
        ["positive_evidence_count"]
        == 1
    )


def test_scoped_explicit_feedback_separates_timing_from_source_preference(tmp_path):
    clear_temporary_feedback_state_preview()
    pending = register_pending_feedback_from_observation(
        _observation(
            turn_id="scoped-news",
            shadow_selected_source_type="news",
            shadow_selected_candidate_id="news:verified",
            actual_primary_channel="chat",
            recommendation_mode="shadow",
        ),
        log_mode="jsonl",
        config_dir=tmp_path,
    )
    assert pending is not None
    assert pending.source_type == "news"
    assert pending.candidate_id == "news:verified"

    not_now = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="scoped-news",
        event_type="proactive_not_now",
        metadata={"ui_generation": "dual_scope_v1"},
        ts=10_001.0,
    )
    preview = get_feedback_state_preview(config_dir=tmp_path, now=10_001.0)
    assert not_now.state_updated is True
    assert not_now.feedback_scope == "conversation_acceptance"
    assert not_now.state_reason == "temporary_only"
    assert preview["conversation_acceptance"]["temporary"]["negative_evidence_count"] == 1
    assert preview["conversation_acceptance"]["persistent"]["negative_evidence_count"] == 0
    assert preview["source_affinity"]["persistent"]["sources"] == {}

    source_negative = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="scoped-news",
        event_type="source_not_interested",
        metadata={"ui_generation": "dual_scope_v1"},
        ts=10_002.0,
    )
    duplicate = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="scoped-news",
        event_type="source_not_interested",
        metadata={"ui_generation": "not-registered"},
        ts=10_003.0,
    )
    preview = get_feedback_state_preview(config_dir=tmp_path, now=10_003.0)
    source = preview["source_affinity"]["persistent"]["sources"]["news"]
    assert source_negative.state_updated is True
    assert source_negative.feedback_scope == "source_affinity"
    assert source_negative.state_reason == "exact_pending_match"
    assert source_negative.event["metadata"] == {"ui_generation": "dual_scope_v1"}
    assert duplicate.state_updated is False
    assert duplicate.state_reason == "duplicate_event"
    assert duplicate.event["metadata"] == {}
    assert source["negative_evidence_count"] == 1
    assert preview["conversation_acceptance"]["persistent"]["negative_evidence_count"] == 0


def test_source_not_interested_without_verified_material_is_diagnostic_only(tmp_path):
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="unverified-source",
        source_type="chat",
        delivered_at=10_000.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    result = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="unverified-source",
        event_type="source_not_interested",
        ts=10_001.0,
    )
    preview = get_feedback_state_preview(config_dir=tmp_path, now=10_001.0)
    assert result.logged is True
    assert result.state_updated is False
    assert result.feedback_scope == "source_affinity"
    assert result.state_reason == "pending_material_mismatch"
    assert preview["source_affinity"]["persistent"]["sources"] == {}


def test_shadow_source_affinity_rejects_unverified_and_technical_events(tmp_path):
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="unverified-music",
        source_type="music",
        delivered_at=1_000.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="unverified-music",
        event_type="music_played_through",
        ts=1_010.0,
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="mismatched-music",
        source_type="music",
        candidate_id="music:expected",
        delivered_at=1_015.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="mismatched-music",
        event_type="music_played_through",
        source_type="news",
        candidate_id="music:other",
        ts=1_018.0,
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="technical-music",
        source_type="music",
        candidate_id="music:technical",
        delivered_at=1_020.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="technical-music",
        event_type="music_error",
        ts=1_030.0,
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="ignored-chat",
        source_type="chat",
        delivered_at=1_040.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="ignored-chat",
        event_type="ignored",
        ts=1_050.0,
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=1_060.0)
    assert preview["source_affinity"]["temporary"]["sources"] == {}
    assert preview["source_affinity"]["persistent"]["sources"] == {}
    assert preview["conversation_acceptance"]["temporary"]["interest_preview"] == 0.0


def test_feedback_state_preview_v1_starts_v2_cold_without_migration(tmp_path):
    clear_temporary_feedback_state_preview()
    legacy_path = tmp_path / LEGACY_FEEDBACK_STATE_PREVIEW_FILENAME
    legacy_bytes = json.dumps(
        {
            "schema_version": 1,
            "sources": {
                "music": {
                    "positive_evidence_count": 99,
                    "negative_evidence_count": 0,
                    "updated_at": 100.0,
                }
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    legacy_path.write_bytes(legacy_bytes)
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="v2-cold-start",
        source_type="chat",
        delivered_at=190.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="v2-cold-start",
        event_type="user_reply",
        ts=200.0,
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=200.0)

    assert preview["version"] == "feedback_state_preview_v2"
    assert (
        preview["conversation_acceptance"]["persistent"]["positive_evidence_count"]
        == 1
    )
    assert preview["source_affinity"]["persistent"]["sources"] == {}
    assert legacy_path.read_bytes() == legacy_bytes
    stored_v2 = json.loads(
        (tmp_path / FEEDBACK_STATE_PREVIEW_FILENAME).read_text(encoding="utf-8")
    )
    assert stored_v2["schema_version"] == 2
    assert stored_v2["conversation_acceptance"]["positive_evidence_count"] == 1


def test_feedback_state_preview_does_not_update_outside_shadow(tmp_path):
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="active-state",
        source_type="news",
        delivered_at=2_000.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="active_source",
    )
    record_feedback_event(
        lanlan_name="neko",
        turn_id="active-state",
        event_type="user_reply",
        ts=2_010.0,
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=2_020.0)
    assert preview["conversation_acceptance"]["temporary"]["interest_preview"] == 0.0
    assert preview["source_affinity"]["temporary"]["sources"] == {}
    assert preview["source_affinity"]["persistent"]["sources"] == {}
    assert not (tmp_path / FEEDBACK_STATE_PREVIEW_FILENAME).exists()


def test_feedback_state_preview_is_not_consumed_by_ranking_or_tuning():
    project_root = Path(__file__).parents[2]
    for source_path in (
        project_root / "main_logic" / "proactive_recommendation.py",
        project_root / "main_logic" / "proactive_recommendation_tuning.py",
    ):
        assert "feedback_state_preview" not in source_path.read_text(encoding="utf-8")


def test_music_feedback_threshold_mapping():
    assert music_feedback_event_type(played_through=True) == "music_played_through"
    assert music_feedback_event_type(started=False) == "music_not_started"
    assert music_feedback_event_type(played_wall_ms=2500) == "music_hard_skip"
    assert music_feedback_event_type(played_wall_ms=8000) == "music_early_close"
    assert (
        music_feedback_event_type(active_playback_ms=2500, played_wall_ms=24_000)
        == "music_hard_skip"
    )
    assert (
        music_feedback_event_type(active_playback_ms=8000, played_wall_ms=24_000)
        == "music_early_close"
    )
    assert (
        music_feedback_event_type(
            active_playback_ms=12_252,
            played_wall_ms=23_940,
            completion_ratio=0.037,
        )
        == "music_early_close"
    )
    assert (
        music_feedback_event_type(active_playback_ms=18_000, played_wall_ms=24_000)
        == "music_normal_close"
    )
    assert music_feedback_event_type(played_wall_ms=20_000, completion_ratio=0.35) == "music_mid_completion"
    assert music_feedback_event_type(played_wall_ms=20_000, completion_ratio=0.72) == "music_high_completion"


def test_active_playback_metadata_is_finite_nonnegative_and_round_trips():
    assert sanitize_feedback_metadata({"active_playback_ms": 12_252.5}) == {
        "active_playback_ms": 12_252.5
    }
    for invalid in (
        -1,
        float("nan"),
        float("inf"),
        "12252",
        True,
        86_400_001,
        10**1000,
    ):
        assert "active_playback_ms" not in sanitize_feedback_metadata(
            {"active_playback_ms": invalid}
        )

    event = build_feedback_event(
        lanlan_name="neko",
        turn_id="music-active-time",
        event_type="music_early_close",
        source_type="music",
        metadata={"active_playback_ms": 12_252, "played_wall_ms": 23_940},
    )
    assert json.loads(json.dumps(event))["metadata"]["active_playback_ms"] == 12_252


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


def test_feedback_calibration_separates_explicit_inferred_and_invalid_turn_ids():
    observations = [
        _observation(turn_id="explicit", ts=10_000.0),
        _observation(turn_id="inferred", ts=9_000.0),
        _observation(turn_id=None, ts=9_000.0),
    ]
    events = [
        build_feedback_event(
            lanlan_name="neko",
            turn_id="explicit",
            event_type="user_reply",
            source_type="music",
            ts=10_010.0,
        )
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

    assert joined[0]["feedback_inferred"] is False
    assert joined[1]["feedback_inferred"] is True
    assert joined[1]["feedback_event_types"] == ["ignored"]
    assert joined[2]["feedback_inferred"] is False
    assert joined[2]["feedback_missing"] is True
    assert calibration["feedback_joined_count"] == 1
    assert calibration["feedback_inferred_count"] == 1
    assert calibration["feedback_scored_count"] == 2
    assert calibration["feedback_missing_count"] == 1
    assert calibration["feedback_rate_denominator"] == "feedback_scored_count"
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
