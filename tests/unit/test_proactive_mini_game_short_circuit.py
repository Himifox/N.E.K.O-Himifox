"""Tests for mini-game invite entry and short-circuit orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.proactive_chat import contracts
from main_logic.proactive_chat import mini_game_invite as invites
from main_routers.system_router import proactive_chat_flow


def test_last_user_message_at_is_derived_from_activity_snapshot() -> None:
    snapshot = SimpleNamespace(seconds_since_user_msg=12.5)

    assert invites._last_user_message_at_from_activity(
        snapshot,
        now=100.0,
    ) == 87.5
    assert invites._last_user_message_at_from_activity(None, now=100.0) is None
    assert invites._last_user_message_at_from_activity(
        SimpleNamespace(seconds_since_user_msg=None),
        now=100.0,
    ) is None


def test_entry_advance_normalizes_resolved_notification(monkeypatch) -> None:
    monkeypatch.setattr(
        invites,
        "_mini_game_invite_advance_response",
        lambda lanlan_name, last_user_msg_at: {
            "session_id": "invite-1",
            "action": "suppress",
        },
    )

    result = invites._advance_mini_game_invite_entry("Yui", 123.0)

    assert result == invites.MiniGameInviteEntryAdvance(
        session_id="invite-1",
        action="suppress",
    )


@pytest.mark.parametrize("outcome", (None, {}, {"action": "suppress"}))
def test_entry_advance_ignores_outcomes_without_session(monkeypatch, outcome) -> None:
    monkeypatch.setattr(
        invites,
        "_mini_game_invite_advance_response",
        lambda lanlan_name, last_user_msg_at: outcome,
    )

    assert invites._advance_mini_game_invite_entry("Yui", 123.0) is None


@pytest.mark.asyncio
async def test_short_circuit_builds_options_for_delivered_invite(monkeypatch) -> None:
    async def fake_deliver(**kwargs):
        return contracts._proactive_chat_body(
            message="mini-game invite delivered",
            channel="mini_game",
            game_type="soccer",
            invite_session_id="invite-1",
        )

    monkeypatch.setattr(invites, "_maybe_deliver_mini_game_invite", fake_deliver)

    short_circuit = await invites._run_mini_game_invite_short_circuit(
        lanlan_name="Yui",
        mgr=object(),
        activity_snapshot=object(),
        invite_lang="zh",
        master_name="博士",
    )

    assert short_circuit is not None
    assert short_circuit.result.status_code == 200
    assert short_circuit.result.body["action"] == "chat"
    assert short_circuit.options_payload is not None
    assert short_circuit.options_payload["type"] == "mini_game_invite_options"
    assert short_circuit.options_payload["session_id"] == "invite-1"
    assert short_circuit.options_payload["game_type"] == "soccer"


@pytest.mark.asyncio
async def test_short_circuit_preserves_pass_without_options(monkeypatch) -> None:
    body = contracts._proactive_pass_body(
        contracts.PROACTIVE_REASON_PASS_DELIVERY_BUSY,
        message="busy",
    )

    async def fake_deliver(**kwargs):
        return body

    monkeypatch.setattr(invites, "_maybe_deliver_mini_game_invite", fake_deliver)

    short_circuit = await invites._run_mini_game_invite_short_circuit(
        lanlan_name="Yui",
        mgr=object(),
        activity_snapshot=object(),
        invite_lang="zh",
        master_name="博士",
    )

    assert short_circuit is not None
    assert short_circuit.result.body is body
    assert short_circuit.options_payload is None


@pytest.mark.asyncio
async def test_short_circuit_returns_none_when_invite_does_not_fire(monkeypatch) -> None:
    async def fake_deliver(**kwargs):
        return None

    monkeypatch.setattr(invites, "_maybe_deliver_mini_game_invite", fake_deliver)

    assert await invites._run_mini_game_invite_short_circuit(
        lanlan_name="Yui",
        mgr=object(),
        activity_snapshot=object(),
        invite_lang="zh",
        master_name="博士",
    ) is None


@pytest.mark.asyncio
async def test_router_adapter_sends_options_payload() -> None:
    send_json = AsyncMock()
    mgr = SimpleNamespace(
        websocket=SimpleNamespace(
            send_json=send_json,
            client_state=None,
        )
    )
    payload = {"type": "mini_game_invite_options", "session_id": "invite-1"}

    await proactive_chat_flow._push_mini_game_invite_options(mgr, payload)

    send_json.assert_awaited_once_with(payload)
