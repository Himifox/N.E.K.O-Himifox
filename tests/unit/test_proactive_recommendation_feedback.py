import json
from datetime import datetime
from pathlib import Path

import main_logic.proactive_recommendation.feedback.service as feedback_module
import main_logic.proactive_recommendation.feedback.learning as learning_module
import main_logic.proactive_recommendation.feedback.availability as availability_module

from main_logic.proactive_recommendation.feedback.service import (
    clear_pending_recommendation_feedback,
    flush_censored_availability,
    note_user_turn_for_feedback,
    record_feedback_event,
    record_feedback_event_with_status,
    register_pending_feedback,
    register_pending_feedback_from_observation,
)
from main_logic.proactive_recommendation.feedback.availability import (
    AVAILABILITY_FILENAME,
    get_availability_shadow,
    record_availability_outcome,
)
from main_logic.proactive_recommendation.feedback.event_processing import (
    build_feedback_event,
    music_feedback_event_type,
    quality_feedback_score,
    sanitize_feedback_metadata,
    sanitize_recommendation_feedback_event,
)
from main_logic.proactive_recommendation.feedback.learning import (
    build_reward_score_v2_preview,
    build_reward_score_v3_preview,
)
from main_logic.proactive_recommendation.feedback.analytics import (
    join_observations_with_feedback,
    join_observations_with_reward_score_v2_preview,
    summarize_feedback_calibration,
    summarize_recommendation_feedback,
    summarize_reward_score_v2_preview,
    summarize_reward_score_v3_preview,
)
from main_logic.proactive_recommendation.feedback.service import (
    FEEDBACK_LOG_FILENAME,
    append_recommendation_feedback_jsonl,
    load_recommendation_feedback_jsonl,
)
from main_logic.proactive_recommendation.state.feedback_preview import (
    FEEDBACK_STATE_PREVIEW_FILENAME,
    LEGACY_FEEDBACK_STATE_PREVIEW_FILENAME,
    TEMPORARY_INTEREST_TTL_SECONDS,
    clear_temporary_feedback_state_preview,
    get_feedback_state_preview,
)
from main_logic.proactive_recommendation.state.source_preferences import (
    get_recommendation_preference_state,
)
from main_logic.proactive_recommendation.state.bandit_posteriors import (
    get_recommendation_bandit_state,
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


def test_availability_off_is_read_only_and_does_not_create_state(tmp_path):
    snapshot = record_availability_outcome(
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="text",
        delivered_at=100.0,
        replied_at=130.0,
        mode="off",
    )

    assert snapshot["enabled"] is False
    assert snapshot["status"] == "insufficient"
    assert snapshot["counterfactual_interval_multiplier"] is None
    assert snapshot["scheduling_consumed"] is False
    assert not (tmp_path / AVAILABILITY_FILENAME).exists()


def test_availability_shadow_uses_exact_bucket_and_aggregate_only(tmp_path):
    delivered = datetime(2026, 1, 1, 9, 0).timestamp()
    outcome_at = delivered + 600
    for _ in range(15):
        record_availability_outcome(
            config_dir=tmp_path,
            activity_state="focused_work",
            input_mode="text",
            delivered_at=outcome_at - 60,
            replied_at=outcome_at,
            mode="shadow",
        )
    for _ in range(15):
        record_availability_outcome(
            config_dir=tmp_path,
            activity_state="focused_work",
            input_mode="text",
            delivered_at=delivered,
            censored=True,
            mode="shadow",
        )

    snapshot = get_availability_shadow(
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="text",
        now=outcome_at,
        mode="shadow",
    )
    persisted = json.loads(
        (tmp_path / AVAILABILITY_FILENAME).read_text(encoding="utf-8")
    )

    assert snapshot["selected_level"] == "exact"
    assert snapshot["status"] == "available"
    assert snapshot["counterfactual_interval_multiplier"] == "1x"
    assert snapshot["selected_bucket"] == {
        "exposure_count": 30.0,
        "reply_count": 15.0,
        "censored_count": 15.0,
        "response_rate": 0.5,
        "average_reply_latency_seconds": 60.0,
    }
    assert snapshot["interval_consumed"] is False
    assert set(persisted) == {"schema_version", "updated_at", "buckets"}
    for bucket in persisted["buckets"].values():
        assert set(bucket) == {
            "activity_state",
            "input_mode",
            "time_bucket",
            "exposure_weight",
            "reply_weight",
            "censored_weight",
            "reply_latency_weighted_seconds",
            "updated_at",
        }


def test_availability_shadow_falls_back_activity_then_input_then_global(tmp_path):
    morning = datetime(2026, 1, 1, 8, 0).timestamp()
    evening = datetime(2026, 1, 1, 20, 0).timestamp()
    for idx in range(16):
        record_availability_outcome(
            config_dir=tmp_path,
            activity_state="focused_work",
            input_mode="text",
            delivered_at=morning,
            replied_at=morning + 60 if idx < 5 else None,
            censored=idx >= 5,
            mode="shadow",
        )
    for idx in range(16):
        record_availability_outcome(
            config_dir=tmp_path,
            activity_state="focused_work",
            input_mode="audio",
            delivered_at=evening,
            replied_at=evening + 120 if idx < 6 else None,
            censored=idx >= 6,
            mode="shadow",
        )

    snapshot = get_availability_shadow(
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="audio",
        now=evening + 600,
        mode="shadow",
    )

    assert snapshot["selected_level"] == "activity_state"
    assert snapshot["status"] == "uncertain"
    assert snapshot["counterfactual_interval_multiplier"] == "2x"
    assert snapshot["fallback_trace"][0]["ready"] is False
    assert snapshot["fallback_trace"][1]["ready"] is True


def test_availability_pending_reply_and_censor_do_not_change_feedback_behavior(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        availability_module,
        "PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE",
        "shadow",
    )
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="reply",
        source_type="news",
        delivered_at=100.0,
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="text",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=130.0,
        had_text=True,
        input_mode="text",
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="censored",
        source_type="meme",
        delivered_at=200.0,
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="audio",
    )
    censored_count = flush_censored_availability(now=801.0)
    snapshot = get_availability_shadow(
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="text",
        now=801.0,
        mode="shadow",
    )

    assert event["event_type"] == "user_reply"
    assert event["metadata"]["reply_latency_seconds"] == 30.0
    assert censored_count == 1
    assert snapshot["fallback_trace"][-1]["exposure_count"] == 2.0
    assert snapshot["fallback_trace"][-1]["reply_count"] == 1.0
    assert snapshot["scheduling_consumed"] is False


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


def test_quality_v2_and_reward_v3_replay_fast_reply_without_speed_bonus():
    fast_reply = build_feedback_event(
        lanlan_name="neko",
        turn_id="fast",
        event_type="user_reply_fast",
        source_type="music",
    )
    reply = build_feedback_event(
        lanlan_name="neko",
        turn_id="reply",
        event_type="user_reply",
        source_type="music",
    )
    ignored = build_feedback_event(
        lanlan_name="neko",
        turn_id="ignored",
        event_type="ignored",
        source_type="music",
    )

    assert fast_reply["report_score_v1"] == 0.25
    assert quality_feedback_score("user_reply_fast") == 0.15
    assert quality_feedback_score("user_reply") == 0.15
    assert quality_feedback_score("ignored") is None
    assert build_reward_score_v3_preview([fast_reply])["reward_score_v3_preview"] == 0.15
    assert build_reward_score_v3_preview([reply])["reward_score_v3_preview"] == 0.15
    ignored_preview = build_reward_score_v3_preview([ignored])
    assert ignored_preview["reward_score_v3_preview"] is None
    assert ignored_preview["excluded_event_types"] == ["ignored"]


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
    v3_summary = summarize_reward_score_v3_preview(
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
    assert v3_summary["version"] == "reward_score_v3_preview_v1"
    assert v3_summary["reward_scored_count"] == 2
    assert v3_summary["feedback_censored_count"] == 1
    assert v3_summary["average_reward_score_v3_preview"] == 0.25
    assert v3_summary["feedback_score_population"] == "explicit_only"
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
        project_root
        / "main_logic"
        / "proactive_recommendation"
        / "engine"
        / "scoring.py",
        project_root / "main_logic" / "proactive_recommendation" / "tuning" / "service.py",
        project_root / "main_routers" / "system_router" / "proactive_chat_flow.py",
    )

    for source_path in policy_sources:
        assert "reward_score_v2_preview" not in source_path.read_text(encoding="utf-8")


def test_active_personalization_keeps_verified_source_learning_enabled(
    tmp_path, monkeypatch
):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    monkeypatch.setattr(
            learning_module,
            "PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE",
        "active",
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="active-music",
        source_type="music",
        candidate_id="music:active",
        delivered_at=1_000.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="active_source",
    )

    result = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="active-music",
        event_type="music_played_through",
        source_type="music",
        candidate_id="music:active",
        ts=1_100.0,
    )
    preview = get_feedback_state_preview(config_dir=tmp_path, now=1_101.0)

    assert result.state_updated is True
    assert result.feedback_scope == "source_affinity"
    assert (
        preview["source_affinity"]["persistent"]["sources"]["music"][
            "positive_evidence_count"
        ]
        == 1
    )


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


def test_explicit_source_feedback_updates_only_verified_source_preference(tmp_path):
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

    source_negative = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="scoped-news",
        event_type="source_not_interested",
        ts=10_001.0,
    )
    duplicate = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="scoped-news",
        event_type="source_not_interested",
        ts=10_002.0,
    )
    preview = get_feedback_state_preview(config_dir=tmp_path, now=10_002.0)
    source = preview["source_affinity"]["persistent"]["sources"]["news"]
    assert source_negative.state_updated is True
    assert source_negative.feedback_scope == "source_affinity"
    assert source_negative.state_reason == "exact_pending_match"
    assert source_negative.event["metadata"] == {}
    assert duplicate.state_updated is False
    assert duplicate.state_reason == "duplicate_event"
    assert duplicate.event["metadata"] == {}
    assert source["negative_evidence_count"] == 1
    assert preview["conversation_acceptance"]["persistent"]["negative_evidence_count"] == 0


def test_v2_shadow_feedback_uses_actual_arm_not_proposed_arm(tmp_path):
    pending = register_pending_feedback_from_observation(
        _observation(
            turn_id="shadow-policy-v2",
            shadow_selected_source_type="news",
            shadow_selected_candidate_id="news:actual",
            actual_primary_channel="chat",
            policy_decision={
                "context_version": "source-context-v2",
                "mode": "shadow",
                "proposed_arm": "music",
                "proposed_candidate_id": "music:virtual",
                "actual_arm": "news",
                "actual_candidate_id": "news:actual",
                "policy_applied": False,
            },
        ),
        log_mode="jsonl",
        config_dir=tmp_path,
    )

    assert pending is not None
    assert pending.source_type == "news"
    assert pending.candidate_id == "news:actual"


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
        project_root
        / "main_logic"
        / "proactive_recommendation"
        / "engine"
        / "scoring.py",
        project_root / "main_logic" / "proactive_recommendation" / "tuning" / "service.py",
    ):
        assert "feedback_state_preview" not in source_path.read_text(encoding="utf-8")


def test_music_feedback_threshold_mapping():
    assert music_feedback_event_type(played_through=True) == "music_played_through"
    assert music_feedback_event_type(started=False) == "music_not_started"
    assert music_feedback_event_type(played_wall_ms=2500) == "music_hard_skip"
    assert music_feedback_event_type(played_wall_ms=10_000) == "music_hard_skip"
    assert music_feedback_event_type(played_wall_ms=10_001) == "music_early_close"
    assert (
        music_feedback_event_type(active_playback_ms=2500, played_wall_ms=24_000)
        == "music_hard_skip"
    )
    assert (
        music_feedback_event_type(active_playback_ms=8000, played_wall_ms=24_000)
        == "music_hard_skip"
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
        music_feedback_event_type(active_playback_ms=29_999, played_wall_ms=35_000)
        == "music_early_close"
    )
    assert music_feedback_event_type(played_wall_ms=30_000, completion_ratio=0.29) == "music_normal_close"
    assert music_feedback_event_type(played_wall_ms=30_000, completion_ratio=0.35) == "music_mid_completion"
    assert music_feedback_event_type(played_wall_ms=30_000, completion_ratio=0.72) == "music_high_completion"


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
    assert first["event_type"] == "user_reply"
    assert "reply_length" not in first["metadata"]
    assert second["event_type"] == "user_continue"
    assert second["metadata"]["reply_length"] == 5
    assert [row["event_type"] for row in rows] == ["user_reply", "user_continue"]
    assert "private text" not in dumped


def test_explicit_text_feedback_updates_non_music_source_preference(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="explicit-news",
        source_type="news",
        candidate_id="news:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="我对这个新闻不感兴趣，以后少推荐一些",
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=120.0)
    source = preview["source_affinity"]["persistent"]["sources"]["news"]
    bandit = get_recommendation_bandit_state(config_dir=tmp_path, now=120.0)
    assert event["event_type"] == "source_not_interested"
    assert event["source_type"] == "news"
    assert event["candidate_id"] == "news:verified"
    assert event["metadata"]["reason"] == "explicit_source_rejection"
    assert source["negative_evidence_count"] == 1
    assert set(bandit["arms"]) == {"news"}
    assert bandit["finalized_outcome_count"] == 1
    assert "不感兴趣" not in json.dumps(event, ensure_ascii=False)


def test_explicit_text_feedback_supports_deictic_positive_meme(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="explicit-meme",
        source_type="meme",
        candidate_id="meme:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="我喜欢这类内容，可以多推荐这种",
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=120.0)
    source = preview["source_affinity"]["persistent"]["sources"]["meme"]
    assert event["event_type"] == "source_interested"
    assert source["positive_evidence_count"] == 1


def test_explicit_text_source_feedback_excludes_music_and_ambiguous_text(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="explicit-music",
        source_type="music",
        candidate_id="music:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    music = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="这首音乐不好听，换一首",
    )

    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="ambiguous-news",
        source_type="news",
        candidate_id="news:verified",
        delivered_at=200.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    ambiguous = note_user_turn_for_feedback(
        "neko",
        timestamp=220.0,
        had_text=True,
        text_allowed=True,
        text="我不喜欢你这种说法",
    )

    assert music["event_type"] == "user_reply"
    assert ambiguous["event_type"] == "user_reply"


def test_non_music_text_feedback_uses_shared_negative_gradient(tmp_path):
    cases = (
        ("news", "以后少推荐新闻", "source_not_interested", -0.35, 1.0),
        ("meme", "怎么又是表情包，已经看腻了", "source_fatigue", -0.20, 0.5),
        ("vision", "这个屏幕内容没意思", "candidate_not_interested", -0.10, 0.25),
        ("video", "换一个", "candidate_not_interested", -0.10, 0.25),
    )
    for index, (source, text, event_type, score, failure) in enumerate(cases):
        clear_pending_recommendation_feedback()
        turn_id = f"gradient-{source}"
        timestamp = 120.0 + index
        register_pending_feedback(
            lanlan_name="neko",
            turn_id=turn_id,
            source_type=source,
            candidate_id=f"{source}:verified",
            delivered_at=100.0,
            log_mode="jsonl",
            config_dir=tmp_path,
            recommendation_mode="shadow",
        )

        event = note_user_turn_for_feedback(
            "neko",
            timestamp=timestamp,
            had_text=True,
            text_allowed=True,
            text=text,
        )
        preference = get_recommendation_preference_state(
            config_dir=tmp_path,
            now=timestamp,
        )["sources"][source]

        assert event["event_type"] == event_type
        assert event["report_score_v1"] == score
        assert preference["effective_failure"] == failure


def test_text_feedback_fuzzy_matches_source_alias_but_not_polarity(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="fuzzy-meme",
        source_type="meme",
        candidate_id="meme:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    typo = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="表情苞不好看",
    )

    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="positive-meme",
        source_type="meme",
        candidate_id="meme:positive",
        delivered_at=200.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    positive = note_user_turn_for_feedback(
        "neko",
        timestamp=220.0,
        had_text=True,
        text_allowed=True,
        text="这个表情包可以多推荐",
    )

    assert typo["event_type"] == "candidate_not_interested"
    assert positive["event_type"] == "source_interested"


def test_text_feedback_ignores_negation_reversal(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="negation-news",
        source_type="news",
        candidate_id="news:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="这个新闻并不无聊",
    )

    assert event["event_type"] == "user_reply"


def test_named_source_feedback_rebinds_recent_verified_pending(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="older-meme",
        source_type="meme",
        candidate_id="meme:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="newer-chat",
        source_type="chat",
        delivered_at=110.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="这个表情包不好看",
    )
    followup = note_user_turn_for_feedback(
        "neko",
        timestamp=125.0,
        had_text=True,
        text_allowed=True,
        text="知道了",
    )
    source = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=125.0,
    )["sources"]["meme"]

    assert event["turn_id"] == "older-meme"
    assert event["candidate_id"] == "meme:verified"
    assert event["event_type"] == "candidate_not_interested"
    assert source["effective_failure"] == 0.25
    assert followup["turn_id"] == "newer-chat"
    assert followup["event_type"] == "user_continue"


def test_deictic_feedback_does_not_rebind_past_newer_chat(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="older-news",
        source_type="news",
        candidate_id="news:verified",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="newer-chat",
        source_type="chat",
        delivered_at=110.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="换一个",
    )

    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=120.0,
    )
    assert event["turn_id"] == "newer-chat"
    assert event["event_type"] == "user_reply"
    assert preference["sources"] == {}


def test_named_source_feedback_without_candidate_updates_preference_not_bandit(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="newer-chat",
        source_type="chat",
        delivered_at=110.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="不喜欢这个新闻",
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=120.0)
    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=120.0,
    )
    bandit = get_recommendation_bandit_state(config_dir=tmp_path, now=120.0)
    rows = load_recommendation_feedback_jsonl(tmp_path / FEEDBACK_LOG_FILENAME)

    assert event["turn_id"] == "newer-chat"
    assert event["event_type"] == "source_not_interested"
    assert event["source_type"] == "news"
    assert event["candidate_id"] is None
    assert event["metadata"]["attribution_basis"] == "explicit_named_source"
    assert (
        preview["source_affinity"]["persistent"]["sources"]["news"]
        ["negative_evidence_count"]
        == 1
    )
    assert preference["sources"]["news"]["effective_failure"] == 1.0
    assert bandit["arms"] == {}
    assert bandit["finalized_outcome_count"] == 0
    assert "不喜欢这个新闻" not in json.dumps(rows, ensure_ascii=False)


def test_named_source_positive_feedback_without_candidate_updates_preference(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="positive-chat",
        source_type="chat",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="新闻可以多推荐",
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=120.0)
    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=120.0,
    )
    assert event["event_type"] == "source_interested"
    assert event["source_type"] == "news"
    assert event["candidate_id"] is None
    assert (
        preview["source_affinity"]["persistent"]["sources"]["news"]
        ["positive_evidence_count"]
        == 1
    )
    assert preference["sources"]["news"]["effective_success"] == 1.0


def test_named_source_fatigue_without_candidate_updates_preference(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="fatigue-chat",
        source_type="chat",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    event = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="怎么又是表情包，已经看腻了",
    )

    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=120.0,
    )
    bandit = get_recommendation_bandit_state(config_dir=tmp_path, now=120.0)
    assert event["event_type"] == "source_fatigue"
    assert event["source_type"] == "meme"
    assert event["candidate_id"] is None
    assert preference["sources"]["meme"]["effective_failure"] == 0.5
    assert bandit["arms"] == {}


def test_direct_named_source_feedback_deduplicates_same_turn(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="duplicate-chat",
        source_type="chat",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    first = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="以后少推荐新闻",
    )
    second = note_user_turn_for_feedback(
        "neko",
        timestamp=121.0,
        had_text=True,
        text_allowed=True,
        text="以后少推荐新闻",
    )

    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=121.0,
    )
    assert first["event_type"] == second["event_type"] == "source_not_interested"
    assert preference["sources"]["news"]["effective_failure"] == 1.0


def test_stronger_direct_named_source_feedback_replaces_same_turn_outcome(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="replacement-chat",
        source_type="chat",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    fatigue = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="怎么又是表情包，已经看腻了",
    )
    rejection = note_user_turn_for_feedback(
        "neko",
        timestamp=121.0,
        had_text=True,
        text_allowed=True,
        text="以后少推荐表情包",
    )

    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=121.0,
    )
    assert fatigue["event_type"] == "source_fatigue"
    assert rejection["event_type"] == "source_not_interested"
    assert preference["sources"]["meme"]["effective_failure"] == 1.0


def test_direct_named_source_feedback_rejects_ambiguous_or_candidate_only_text(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="ambiguous-chat",
        source_type="chat",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    ambiguous = note_user_turn_for_feedback(
        "neko",
        timestamp=120.0,
        had_text=True,
        text_allowed=True,
        text="新闻和视频都少推荐",
    )
    candidate_only = note_user_turn_for_feedback(
        "neko",
        timestamp=121.0,
        had_text=True,
        text_allowed=True,
        text="这个新闻不好看",
    )

    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=121.0,
    )
    assert ambiguous["event_type"] == "user_reply"
    assert candidate_only["event_type"] == "user_continue"
    assert preference["sources"] == {}


def test_untrusted_attribution_metadata_cannot_bypass_candidate_gate(tmp_path):
    clear_pending_recommendation_feedback()
    clear_temporary_feedback_state_preview()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="forged-chat",
        source_type="chat",
        delivered_at=100.0,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    result = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="forged-chat",
        event_type="source_not_interested",
        source_type="news",
        metadata={"attribution_basis": "explicit_named_source"},
        ts=120.0,
    )
    candidate_result = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="forged-chat",
        event_type="candidate_not_interested",
        source_type="news",
        ts=121.0,
    )

    preview = get_feedback_state_preview(config_dir=tmp_path, now=120.0)
    preference = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=120.0,
    )
    assert result.state_updated is False
    assert result.preference_state_updated is False
    assert result.bandit_state_updated is False
    assert result.state_reason == "pending_material_mismatch"
    assert candidate_result.state_updated is False
    assert candidate_result.preference_state_updated is False
    assert candidate_result.state_reason == "pending_material_mismatch"
    assert preview["source_affinity"]["persistent"]["sources"] == {}
    assert preference["sources"] == {}


def test_feedback_summary_scores_explicit_events_and_censors_unanswered():
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

    assert summary["feedback_sample_count"] == 2
    assert summary["quality_feedback_scored_count"] == 2
    assert summary["positive_rate"] == 0.5
    assert summary["negative_rate"] == 0.5
    assert summary["feedback_missing_count"] == 1
    assert summary["feedback_censored_count"] == 1
    assert summary["feedback_score_population"] == "explicit_only"
    assert summary["score_version"] == "report_score_v2"
    assert summary["event_type_distribution"] == {
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


def test_feedback_calibration_separates_explicit_censored_and_invalid_turn_ids():
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
    assert joined[1]["feedback_inferred"] is False
    assert joined[1]["feedback_censored"] is True
    assert joined[1]["feedback_event_types"] == []
    assert joined[2]["feedback_inferred"] is False
    assert joined[2]["feedback_missing"] is True
    assert calibration["feedback_joined_count"] == 1
    assert calibration["feedback_inferred_count"] == 0
    assert calibration["feedback_scored_count"] == 1
    assert calibration["quality_feedback_scored_count"] == 1
    assert calibration["feedback_censored_count"] == 2
    assert calibration["feedback_missing_count"] == 2
    assert calibration["feedback_score_population"] == "explicit_only"
    assert calibration["feedback_rate_denominator"] == "quality_feedback_scored_count"
    assert "feedback_sample_count_below_threshold" in calibration["active_ready_reasons"]


def test_feedback_calibration_excludes_ignored_from_quality_and_tuning():
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
    assert calibration["feedback_censored_count"] == 4
    assert calibration["quality_feedback_scored_count"] == 3
    assert "meme" not in calibration["feedback_signal_summary"]
    assert "meme" not in calibration["source_feedback_pressure"]
    assert calibration["feedback_actionable_suggestions"]["music"]["adjustment"] == 0.03
    assert calibration["feedback_actionable_suggestions"]["music"]["reasons"] == [
        "strong_music_positive_feedback"
    ]
    assert "meme" not in calibration["feedback_actionable_suggestions"]
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
            event_type = "music_mid_completion"
        else:
            score = 0.42
            event_type = "music_normal_close"
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
    assert calibration["average_feedback_score"] == 0.217
    assert calibration["top1_positive_rate"] == 1.0
    assert calibration["top1_negative_rate"] == 0.0
    assert calibration["score_bucket_feedback"]["high"]["average_feedback_score"] == 0.35
    assert calibration["score_bucket_feedback"]["mid"]["average_feedback_score"] == 0.25
    assert calibration["score_bucket_feedback"]["low"]["average_feedback_score"] == 0.05
    assert calibration["active_ready_by_feedback"] is True
    assert calibration["active_ready_reasons"] == []
