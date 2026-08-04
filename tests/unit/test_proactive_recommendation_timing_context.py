from main_logic.proactive_recommendation.feedback.service import (
    clear_pending_recommendation_feedback,
    consecutive_unanswered_recommendation_deliveries,
    note_user_turn_for_feedback,
    register_pending_feedback,
)
from main_logic.proactive_recommendation.feedback.availability import (
    record_availability_outcome,
)
from main_logic.proactive_recommendation.service import (
    clear_proactive_delivery_timing_history,
    proactive_delivery_timing_snapshot,
    record_proactive_delivery_for_timing,
)


def test_delivery_timing_snapshot_uses_real_delivery_history():
    clear_proactive_delivery_timing_history()
    name = "timing-neko"
    record_proactive_delivery_for_timing(name, delivered_at=10_000.0)
    record_proactive_delivery_for_timing(name, delivered_at=10_120.0)

    snapshot = proactive_delivery_timing_snapshot(
        name,
        configured_interval_seconds="300",
        now=10_420.0,
    )

    assert snapshot == {
        "configured_interval_seconds": 300.0,
        "elapsed_since_last_delivery_seconds": 300.0,
        "recent_delivery_count_30m": 2,
        "recent_delivery_count_2h": 2,
    }
    clear_proactive_delivery_timing_history()


def test_delivery_timing_snapshot_prunes_rows_outside_two_hours():
    clear_proactive_delivery_timing_history()
    name = "timing-prune-neko"
    for timestamp in (1_000.0, 8_300.0, 9_000.0, 9_700.0):
        record_proactive_delivery_for_timing(name, delivered_at=timestamp)

    snapshot = proactive_delivery_timing_snapshot(
        name,
        configured_interval_seconds=-1,
        now=10_000.0,
    )

    assert snapshot["configured_interval_seconds"] is None
    assert snapshot["elapsed_since_last_delivery_seconds"] == 300.0
    assert snapshot["recent_delivery_count_30m"] == 3
    assert snapshot["recent_delivery_count_2h"] == 3
    clear_proactive_delivery_timing_history()


def test_availability_shadow_does_not_change_delivery_timing(tmp_path):
    clear_proactive_delivery_timing_history()
    name = "availability-shadow-neko"
    record_proactive_delivery_for_timing(name, delivered_at=10_000.0)
    before = proactive_delivery_timing_snapshot(
        name,
        configured_interval_seconds=300,
        now=10_300.0,
    )

    shadow = record_availability_outcome(
        config_dir=tmp_path,
        activity_state="focused_work",
        input_mode="text",
        delivered_at=10_000.0,
        replied_at=10_030.0,
        mode="shadow",
    )
    after = proactive_delivery_timing_snapshot(
        name,
        configured_interval_seconds=300,
        now=10_300.0,
    )

    assert after == before
    assert shadow["scheduling_consumed"] is False
    assert shadow["interval_consumed"] is False
    clear_proactive_delivery_timing_history()


def test_consecutive_unanswered_delivery_count_stops_at_latest_reply():
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="turn-1",
        source_type="music",
        delivered_at=100.0,
    )
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="turn-2",
        source_type="meme",
        delivered_at=200.0,
    )

    assert consecutive_unanswered_recommendation_deliveries("neko", now=250.0) == 2

    note_user_turn_for_feedback(
        "neko",
        timestamp=260.0,
        had_text=True,
    )
    assert consecutive_unanswered_recommendation_deliveries("neko", now=270.0) == 0

    register_pending_feedback(
        lanlan_name="neko",
        turn_id="turn-3",
        source_type="vision",
        delivered_at=300.0,
    )
    assert consecutive_unanswered_recommendation_deliveries("neko", now=320.0) == 1
    clear_pending_recommendation_feedback()
