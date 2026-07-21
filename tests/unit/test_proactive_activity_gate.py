"""Direct tests for framework-independent proactive-chat activity gates."""

from types import SimpleNamespace

import pytest

from main_logic.proactive_chat import contracts
from main_logic.proactive_chat import decisions


def _snapshot(**overrides):
    values = {
        "state": "casual_browsing",
        "propensity": "open",
        "skip_probability": 0.0,
        "unfinished_thread": None,
        "game_intensity": "low",
        "game_genre": "unknown",
        "anti_slack_pending": None,
        "work_break_pending": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("privacy_mode", "expected"),
    ((False, True), (True, False)),
)
def test_activity_snapshot_fetch_respects_privacy_mode(
    privacy_mode,
    expected,
) -> None:
    assert decisions._should_fetch_activity_snapshot(privacy_mode) is expected


def test_closed_activity_gate_preserves_privacy_pass_contract() -> None:
    snapshot = _snapshot(
        state="private",
        propensity="closed",
        unfinished_thread={"text": "still blocked"},
    )

    result = decisions._decide_closed_activity_gate(
        snapshot,
        debug_force_invite=False,
    )

    assert result is not None
    assert result.status_code == 200
    assert result.body == {
        "success": True,
        "reason_code": contracts.PROACTIVE_REASON_PASS_PRIVACY,
        "action": "pass",
        "stage": contracts.PROACTIVE_STAGE_ACTIVITY_GATE,
        "message": "user state=private → closed (privacy lockdown)",
    }


@pytest.mark.parametrize(
    ("snapshot", "debug_force_invite"),
    (
        (None, False),
        (_snapshot(), False),
        (_snapshot(state="private", propensity="closed"), True),
    ),
)
def test_closed_activity_gate_allows_open_or_debug_paths(
    snapshot,
    debug_force_invite,
) -> None:
    assert (
        decisions._decide_closed_activity_gate(
            snapshot,
            debug_force_invite=debug_force_invite,
        )
        is None
    )


def test_restricted_activity_uses_fixed_schedule_and_bounded_jitter() -> None:
    decision = decisions._decide_activity_schedule(
        _snapshot(propensity="restricted_screen_only"),
        base_interval_seconds="300",
    )

    assert decision == decisions.ActivityScheduleDecision(
        fixed_mode=True,
        base_interval=300.0,
        jitter_max=60.0,
        has_must_fire=False,
    )


@pytest.mark.parametrize("raw_interval", (None, "bad", -10, 0))
def test_restricted_activity_ignores_invalid_or_nonpositive_jitter(
    raw_interval,
) -> None:
    decision = decisions._decide_activity_schedule(
        _snapshot(propensity="restricted_screen_only"),
        base_interval_seconds=raw_interval,
    )

    assert decision.fixed_mode is True
    assert decision.jitter_max == 0.0


def test_must_fire_keeps_fixed_mode_without_delaying_reminder() -> None:
    decision = decisions._decide_activity_schedule(
        _snapshot(
            propensity="restricted_screen_only",
            anti_slack_pending={"reason": "resume work"},
        ),
        base_interval_seconds=20,
    )

    assert decision.fixed_mode is True
    assert decision.has_must_fire is True
    assert decision.base_interval == 0.0
    assert decision.jitter_max == 0.0


def test_open_activity_uses_default_schedule() -> None:
    assert decisions._decide_activity_schedule(
        _snapshot(propensity="open"),
        base_interval_seconds=20,
    ) == decisions.ActivityScheduleDecision()


def test_probabilistic_activity_gate_preserves_throttled_contract() -> None:
    snapshot = _snapshot(
        state="gaming",
        skip_probability=0.3,
        game_intensity="high",
        game_genre="immersive_horror",
    )

    result = decisions._decide_probabilistic_activity_gate(
        snapshot,
        debug_force_invite=False,
        random_value=0.1,
    )

    assert result is not None
    assert result.body == {
        "success": True,
        "reason_code": contracts.PROACTIVE_REASON_PASS_THROTTLED,
        "action": "pass",
        "stage": contracts.PROACTIVE_STAGE_ACTIVITY_GATE,
        "message": "probabilistic skip: state=gaming intensity=high skip_prob=0.30",
    }


def test_probabilistic_activity_gate_uses_strict_less_than_roll() -> None:
    snapshot = _snapshot(skip_probability=0.3)

    assert decisions._decide_probabilistic_activity_gate(
        snapshot,
        debug_force_invite=False,
        random_value=0.3,
    ) is None


@pytest.mark.parametrize(
    ("snapshot", "debug_force_invite"),
    (
        (None, False),
        (_snapshot(skip_probability=0.0), False),
        (_snapshot(skip_probability=1.0, unfinished_thread={"text": "follow up"}), False),
        (_snapshot(skip_probability=1.0), True),
    ),
)
def test_probabilistic_activity_gate_preserves_bypass_rules(
    monkeypatch,
    snapshot,
    debug_force_invite,
) -> None:
    monkeypatch.setattr(
        decisions.random,
        "random",
        lambda: pytest.fail("bypassed activity gate must not consume RNG"),
    )

    assert decisions._decide_probabilistic_activity_gate(
        snapshot,
        debug_force_invite=debug_force_invite,
    ) is None
