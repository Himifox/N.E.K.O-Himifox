"""Tests for proactive-chat source mode and weight selection."""

from types import SimpleNamespace

import pytest

from main_logic.proactive_chat import contracts
from main_logic.proactive_chat import decisions


def _command(payload):
    return contracts.ProactiveChatCommand.from_payload(payload)


def _snapshot(*, propensity="open", unfinished_thread=None, state="idle"):
    return SimpleNamespace(
        propensity=propensity,
        unfinished_thread=unfinished_thread,
        state=state,
    )


def test_explicit_empty_modes_do_not_fall_back_to_home() -> None:
    explicit_modes = []
    selection = decisions._select_source_modes(
        _command({"enabled_modes": explicit_modes}),
        _snapshot(),
        debug_force_invite=False,
    )

    assert selection.enabled_modes == []
    assert selection.has_unfinished_thread is False
    assert selection.result is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"screenshot_data": "image"}, ["vision"]),
        ({"use_window_search": True}, ["window"]),
        ({"content_type": "news"}, ["news"]),
        ({"content_type": "video"}, ["video"]),
        ({"use_personal_dynamic": True}, ["personal"]),
        ({}, ["home"]),
    ),
)
def test_legacy_source_mode_inference_preserves_precedence(payload, expected) -> None:
    selection = decisions._select_source_modes(
        _command(payload),
        _snapshot(),
        debug_force_invite=False,
    )

    assert selection.enabled_modes == expected


def test_restricted_screen_policy_keeps_only_vision() -> None:
    selection = decisions._select_source_modes(
        _command({"enabled_modes": ["music", "vision", "news"]}),
        _snapshot(propensity="restricted_screen_only", state="gaming"),
        debug_force_invite=False,
    )

    assert selection.enabled_modes == ["vision"]
    assert selection.restricted_to_vision is True
    assert selection.result is None


def test_restricted_screen_policy_allows_text_only_unfinished_thread() -> None:
    selection = decisions._select_source_modes(
        _command({"enabled_modes": ["music"]}),
        _snapshot(
            propensity="restricted_screen_only",
            unfinished_thread={"text": "continue"},
            state="focused_work",
        ),
        debug_force_invite=False,
    )

    assert selection.enabled_modes == []
    assert selection.has_unfinished_thread is True
    assert selection.text_only_followup is True
    assert selection.result is None


def test_restricted_screen_policy_passes_without_vision_or_thread() -> None:
    selection = decisions._select_source_modes(
        _command({"enabled_modes": ["music"]}),
        _snapshot(propensity="restricted_screen_only", state="focused_work"),
        debug_force_invite=False,
    )

    assert selection.result is not None
    assert selection.result.body == {
        "success": True,
        "reason_code": contracts.PROACTIVE_REASON_PASS_RESTRICTED_SCREEN_ONLY,
        "action": "pass",
        "stage": contracts.PROACTIVE_STAGE_ACTIVITY_GATE,
        "message": (
            "user state=focused_work restricts proactive to screen-only, "
            "but vision not enabled this round"
        ),
    }


def test_debug_force_invite_bypasses_restricted_source_policy() -> None:
    selection = decisions._select_source_modes(
        _command({"enabled_modes": ["music"]}),
        _snapshot(propensity="restricted_screen_only"),
        debug_force_invite=True,
    )

    assert selection.enabled_modes == ["music"]
    assert selection.result is None
    assert selection.restricted_to_vision is False


def test_empty_source_gate_runs_only_without_unfinished_thread() -> None:
    result = decisions._decide_empty_source_gate(
        [],
        has_unfinished_thread=False,
    )

    assert result is not None
    assert result.body["reason_code"] == contracts.PROACTIVE_REASON_PASS_SOURCE_EMPTY
    assert decisions._decide_empty_source_gate(
        [],
        has_unfinished_thread=True,
    ) is None
    assert decisions._decide_empty_source_gate(
        ["home"],
        has_unfinished_thread=False,
    ) is None


def test_weight_selection_excludes_vision_and_unavailable_channels(monkeypatch) -> None:
    calls = []

    def fake_compute(lanlan_name, candidates):
        calls.append((lanlan_name, candidates))
        return {"music": 0.1, "reminiscence": 0.9}

    monkeypatch.setattr(decisions, "_compute_source_weights", fake_compute)
    monkeypatch.setattr(
        decisions,
        "_filter_sources_by_weight",
        lambda weights: {"music"},
    )
    available = {"vision": {}, "music": {}}

    selection = decisions._select_weighted_sources(
        "Yui",
        ["vision", "music", "news"],
        available,
        has_reminiscence=True,
    )

    assert calls == [("Yui", ["music", "reminiscence"])]
    assert selection.weights == {"music": 0.1, "reminiscence": 0.9}
    assert selection.suppressed == {"music"}
    assert available == {"vision": {}, "music": {}}


def test_weight_selection_skips_computation_without_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        decisions,
        "_compute_source_weights",
        lambda *args: pytest.fail("empty candidates must not compute weights"),
    )

    selection = decisions._select_weighted_sources(
        "Yui",
        ["vision", "music"],
        {"vision": {}},
        has_reminiscence=False,
    )

    assert selection == decisions.SourceWeightSelection(weights={}, suppressed=set())
