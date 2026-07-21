"""Focused compatibility tests for the proactive Phase 1 decision boundary."""

from main_logic.proactive_chat import contracts, generation


def test_empty_phase1_passes_when_no_unfinished_thread() -> None:
    decision = generation._decide_phase1_channels(
        [],
        None,
        has_unfinished_thread=False,
    )

    assert isinstance(decision, generation.Phase1Decision)
    assert decision.result is not None
    assert decision.result.body == {
        "success": True,
        "reason_code": contracts.PROACTIVE_REASON_PASS_MODEL_PASS,
        "action": "pass",
        "stage": contracts.PROACTIVE_STAGE_MODEL_DECISION,
        "message": "所有信息源筛选后均不值得搭话",
    }
    assert decision.active_channels == []
    assert decision.primary_channel == "unknown"
    assert decision.web_topic is None
    assert decision.music_topic is None


def test_empty_phase1_continues_text_only_for_unfinished_thread() -> None:
    decision = generation._decide_phase1_channels(
        [],
        None,
        has_unfinished_thread=True,
    )

    assert decision == generation.Phase1Decision(
        result=None,
        active_channels=[],
        primary_channel="unknown",
        web_topic=None,
        music_topic=None,
    )


def test_phase1_preserves_topic_order_duplicates_and_last_special_topic() -> None:
    topics = [
        ("music", "first track"),
        ("web", "first article"),
        ("music", "second track"),
        ("meme", "reaction image"),
        ("web", "second article"),
    ]

    decision = generation._decide_phase1_channels(
        topics,
        None,
        has_unfinished_thread=False,
    )

    assert decision.result is None
    assert decision.active_channels == ["music", "web", "music", "meme", "web"]
    assert decision.primary_channel == "music"
    assert decision.web_topic == "second article"
    assert decision.music_topic == "second track"
    assert topics == [
        ("music", "first track"),
        ("web", "first article"),
        ("music", "second track"),
        ("meme", "reaction image"),
        ("web", "second article"),
    ]


def test_vision_is_appended_and_takes_primary_channel() -> None:
    decision = generation._decide_phase1_channels(
        [("web", "article"), ("meme", "reaction image")],
        "screen description",
        has_unfinished_thread=False,
    )

    assert decision.result is None
    assert decision.active_channels == ["web", "meme", "vision"]
    assert decision.primary_channel == "vision"
    assert decision.web_topic == "article"
    assert decision.music_topic is None


def test_vision_only_phase1_is_not_treated_as_model_pass() -> None:
    decision = generation._decide_phase1_channels(
        [],
        "screen description",
        has_unfinished_thread=False,
    )

    assert decision.result is None
    assert decision.active_channels == ["vision"]
    assert decision.primary_channel == "vision"
    assert decision.web_topic is None
    assert decision.music_topic is None
