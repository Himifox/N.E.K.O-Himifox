"""Direct tests for framework-independent proactive-chat entry guards."""

import pytest

from main_logic.proactive_chat import contracts
from main_logic.proactive_chat import decisions


def test_manager_guard_rejects_unknown_character() -> None:
    result = decisions._decide_manager_entry_guard(
        "Missing",
        manager_exists=False,
    )

    assert result is not None
    assert result.status_code == 404
    assert result.body == {
        "success": False,
        "reason_code": contracts.PROACTIVE_REASON_ERROR_CHARACTER_NOT_FOUND,
        "stage": contracts.PROACTIVE_STAGE_ENTRY_GUARD,
        "error": "角色 Missing 不存在",
    }


def test_manager_guard_rejects_goodbye_silent_session() -> None:
    result = decisions._decide_manager_entry_guard(
        "Yui",
        manager_exists=True,
        goodbye_silent=True,
    )

    assert result is not None
    assert result.status_code == 200
    assert result.body["action"] == "pass"
    assert result.body["reason_code"] == contracts.PROACTIVE_REASON_PASS_DISABLED
    assert result.body["message"] == "goodbye silent; proactive skipped"


def test_manager_guard_allows_available_session() -> None:
    assert (
        decisions._decide_manager_entry_guard(
            "Yui",
            manager_exists=True,
            goodbye_silent=False,
        )
        is None
    )


@pytest.mark.parametrize(
    ("route_state", "expected_message"),
    (
        (True, "game route active; ordinary proactive skipped"),
        (None, "game route guard unavailable; ordinary proactive skipped"),
    ),
)
def test_game_route_guard_fails_closed(route_state, expected_message) -> None:
    result = decisions._decide_game_route_entry_guard(route_state)

    assert result is not None
    assert result.status_code == 200
    assert result.body["reason_code"] == contracts.PROACTIVE_REASON_PASS_ROUTE_ACTIVE
    assert result.body["message"] == expected_message


def test_game_route_guard_allows_inactive_route() -> None:
    assert decisions._decide_game_route_entry_guard(False) is None


def test_busy_guard_preserves_409_contract_and_state_snapshot() -> None:
    snapshot = {"phase": "PHASE1", "owner": "PROACTIVE"}

    result = decisions._decide_busy_entry_guard(
        False,
        state_snapshot=snapshot,
    )

    assert result is not None
    assert result.status_code == 409
    assert result.body == {
        "success": False,
        "reason_code": contracts.PROACTIVE_REASON_PASS_BUSY,
        "stage": contracts.PROACTIVE_STAGE_ENTRY_GUARD,
        "error": "AI正在响应中，无法主动搭话",
        "message": "请等待当前响应完成",
        "state": snapshot,
    }


def test_busy_guard_allows_available_session() -> None:
    assert (
        decisions._decide_busy_entry_guard(True, state_snapshot=None)
        is None
    )


@pytest.mark.parametrize(
    ("voice_mode", "manager_active", "realtime_session", "expected"),
    (
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ),
)
def test_voice_fast_path_requires_all_entry_conditions(
    voice_mode,
    manager_active,
    realtime_session,
    expected,
) -> None:
    assert (
        decisions._should_use_voice_fast_path(
            voice_mode=voice_mode,
            manager_active=manager_active,
            realtime_session=realtime_session,
        )
        is expected
    )
