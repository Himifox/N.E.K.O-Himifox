# -*- coding: utf-8 -*-

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.proactive_chat import contracts
from main_routers.system_router import proactive_chat_flow


@pytest.mark.parametrize(
    ("reason_code", "expected_stage"),
    tuple(contracts._PROACTIVE_REASON_STAGE.items()),
)
def test_every_registered_reason_maps_to_its_contract_stage(
    reason_code: str,
    expected_stage: str,
) -> None:
    assert contracts._proactive_stage_for_reason(reason_code) == expected_stage


def test_every_declared_reason_is_registered() -> None:
    declared_reasons = {
        value
        for name, value in vars(contracts).items()
        if name.startswith("PROACTIVE_REASON_") and isinstance(value, str)
    }

    assert set(contracts._PROACTIVE_REASON_STAGE) == declared_reasons


def test_unknown_reason_maps_to_unknown_stage() -> None:
    assert contracts._proactive_stage_for_reason(None) == contracts.PROACTIVE_STAGE_UNKNOWN
    assert (
        contracts._proactive_stage_for_reason("NOT_A_REASON")
        == contracts.PROACTIVE_STAGE_UNKNOWN
    )


def test_body_builders_preserve_explicit_contract_fields() -> None:
    body = contracts._proactive_pass_body(
        contracts.PROACTIVE_REASON_PASS_SOURCE_EMPTY,
        success=False,
        stage="custom-stage",
        message="none",
    )

    assert body == {
        "success": False,
        "reason_code": contracts.PROACTIVE_REASON_PASS_SOURCE_EMPTY,
        "action": "pass",
        "stage": "custom-stage",
        "message": "none",
    }


@pytest.mark.parametrize(
    ("body", "expected_reason", "expected_stage"),
    (
        (
            {"action": "chat"},
            contracts.PROACTIVE_REASON_CHAT_DELIVERED,
            contracts.PROACTIVE_STAGE_DELIVERY,
        ),
        (
            {"action": "pass"},
            contracts.PROACTIVE_REASON_PASS_UNSPECIFIED,
            contracts.PROACTIVE_STAGE_UNKNOWN,
        ),
        (
            {"success": False},
            contracts.PROACTIVE_REASON_ERROR_INTERNAL,
            contracts.PROACTIVE_STAGE_RUNTIME_ERROR,
        ),
    ),
)
def test_ensure_reason_code_preserves_legacy_defaults(
    body: dict,
    expected_reason: str,
    expected_stage: str,
) -> None:
    result = contracts._ensure_proactive_reason_code(body)

    assert result is body
    assert result["reason_code"] == expected_reason
    assert result["stage"] == expected_stage


def test_proactive_chat_command_preserves_legacy_defaults() -> None:
    command = contracts.ProactiveChatCommand.from_payload({})

    assert command.lanlan_name is None
    assert command.voice_mode is False
    assert command.is_playing_music is False
    assert command.current_track is None
    assert command.music_cooldown is False
    assert command.mini_game_invite_enabled is True
    assert command.enabled_modes is None
    assert command.enabled_modes_provided is False
    assert command.window_title == ""
    assert command.language_candidates == (None, None, None)


def test_proactive_chat_command_captures_request_fields() -> None:
    current_track = {"name": "Night Flight"}
    avatar_position = {"x": 0.25, "y": 0.75}
    command = contracts.ProactiveChatCommand.from_payload(
        {
            "lanlan_name": "Yui",
            "voice_mode": 1,
            "is_playing_music": True,
            "current_track": current_track,
            "music_cooldown": "active",
            "mini_game_invite_enabled": 0,
            "base_interval_seconds": "20",
            "enabled_modes": [],
            "content_type": "video",
            "screenshot_data": "data:image/jpeg;base64,abc",
            "use_window_search": 1,
            "use_personal_dynamic": "yes",
            "avatar_position": avatar_position,
            "window_title": "Editor",
            "language": "zh-TW",
            "lang": "zh",
            "i18n_language": "en",
        }
    )

    assert command.lanlan_name == "Yui"
    assert command.voice_mode is True
    assert command.is_playing_music is True
    assert command.current_track is current_track
    assert command.music_cooldown is True
    assert command.mini_game_invite_enabled is False
    assert command.base_interval_seconds == "20"
    assert command.enabled_modes == []
    assert command.enabled_modes_provided is True
    assert command.content_type == "video"
    assert command.screenshot_data == "data:image/jpeg;base64,abc"
    assert command.use_window_search is True
    assert command.use_personal_dynamic is True
    assert command.avatar_position is avatar_position
    assert command.window_title == "Editor"
    assert command.language_candidates == ("zh-TW", "zh", "en")


def test_proactive_chat_command_distinguishes_missing_and_explicit_modes() -> None:
    missing = contracts.ProactiveChatCommand.from_payload({})
    explicit_none = contracts.ProactiveChatCommand.from_payload(
        {"enabled_modes": None}
    )

    assert missing.enabled_modes_provided is False
    assert explicit_none.enabled_modes_provided is True


@pytest.mark.parametrize("payload", (None, [], "not-an-object", 1))
def test_proactive_chat_command_preserves_legacy_non_mapping_failure(payload) -> None:
    """Malformed JSON keeps the pre-refactor ``.get`` failure/wire detail."""
    with pytest.raises(AttributeError, match="has no attribute 'get'"):
        contracts.ProactiveChatCommand.from_payload(payload)


@pytest.mark.asyncio
async def test_non_mapping_request_preserves_legacy_error_detail(monkeypatch) -> None:
    config_manager = SimpleNamespace(
        aget_character_data=AsyncMock(
            return_value=("博士", "Yui", None, None, None, {}, None, None, None),
        ),
    )
    request = SimpleNamespace(json=AsyncMock(return_value=[]))

    monkeypatch.setattr(
        proactive_chat_flow,
        "_validate_local_mutation_request",
        lambda request: None,
    )
    monkeypatch.setattr(
        proactive_chat_flow,
        "get_config_manager",
        lambda: config_manager,
    )
    monkeypatch.setattr(
        proactive_chat_flow,
        "get_session_manager",
        lambda: SimpleNamespace(),
    )

    response = await proactive_chat_flow.proactive_chat(request)

    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["reason_code"] == contracts.PROACTIVE_REASON_ERROR_INTERNAL
    assert body["detail"] == "'list' object has no attribute 'get'"
