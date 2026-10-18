from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugin.plugins.yui_stardew import YuiStardewPlugin
from plugin.plugins.yui_stardew.bridge import BridgeError


@pytest.mark.parametrize("action", ["follow", "status", "view"])
@pytest.mark.parametrize("ok", [True, False])
async def test_compact_result_preserves_execution_evidence(action, ok):
    state = {
        "world_ready": True,
        "body_present": False,
        "work_kind": "Chop",
        "stamina": 0,
        "nearby": [{"reason_code": "GIANT-CROP-POTENTIAL", "count": 3}],
    }
    response = {
        "protocol_version": 2,
        "request_id": "a" * 32,
        "ok": ok,
        "code": "TARGET-PROTECTED",
        "message": "The target is protected.",
        "state": state,
        "planting": None,
        "combat": None,
    }
    plugin = YuiStardewPlugin.__new__(YuiStardewPlugin)
    plugin._request_bridge = AsyncMock(return_value=response)

    result = await plugin._request_for_llm(action)

    assert result["ok"] is ok
    assert result["code"] == response["code"]
    assert result["message"] == response["message"]
    assert result["state"] == state
    assert "execution_truth" in result
    if not ok:
        assert "failed" in result["execution_truth"]
    assert "protocol_version" not in result
    assert "request_id" not in result
    assert "planting" not in result
    assert "combat" not in result
    assert "execution_truth" not in response


async def test_replacing_work_still_stops_before_starting():
    plugin = YuiStardewPlugin.__new__(YuiStardewPlugin)
    plugin._request_bridge = AsyncMock(
        side_effect=[
            {"ok": True, "state": {"work_kind": "Chop"}},
            {"ok": True},
            {"ok": True, "code": "STARTED", "state": {"work_kind": "Water"}},
        ]
    )
    arguments = {"kind": "Water"}

    result = await plugin._request_for_llm("work", arguments)

    assert [call.args for call in plugin._request_bridge.await_args_list] == [
        ("status",),
        ("work_stop",),
        ("work", arguments),
    ]
    assert result["replaced_work_kind"] == "Chop"
    assert result["state"]["work_kind"] == "Water"
    assert result["code"] == "STARTED"


async def test_failed_stop_prevents_replacement_work():
    plugin = YuiStardewPlugin.__new__(YuiStardewPlugin)
    plugin._request_bridge = AsyncMock(
        side_effect=[
            {"ok": True, "state": {"work_kind": "Chop"}},
            {"ok": False, "code": "BRIDGE-TIMEOUT", "state": {"work_kind": "Chop"}},
        ]
    )

    result = await plugin._request_for_llm("work", {"kind": "Water"})

    assert plugin._request_bridge.await_count == 2
    assert result["ok"] is False
    assert result["code"] == "BRIDGE-TIMEOUT"
    assert result["state"]["work_kind"] == "Chop"


async def test_connection_error_remains_an_error_result():
    plugin = YuiStardewPlugin.__new__(YuiStardewPlugin)
    plugin.logger = SimpleNamespace(warning=Mock())
    plugin._request_bridge = AsyncMock(
        side_effect=BridgeError("AUTH-FAILED", "Invalid key.")
    )

    result = await plugin._request_for_llm("follow")

    assert result["is_error"] is True
    assert result["error"] == "AUTH-FAILED"
    assert result["output"]["ok"] is False
    assert result["output"]["message"] == "Invalid key."
    assert result["output"]["state"] is None
