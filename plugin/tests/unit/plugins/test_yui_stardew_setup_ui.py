from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugin.plugins.yui_stardew import YuiStardewPlugin
from plugin.plugins.yui_stardew.installer import SetupError
from plugin.sdk.plugin import Err, Ok


@pytest.fixture
def plugin(tmp_path):
    game = str(tmp_path / "SteamLibrary" / "steamapps" / "common" / "Stardew Valley")
    instance = YuiStardewPlugin.__new__(YuiStardewPlugin)
    instance._init_hosted_ui()
    instance._runtime_config = {"game_path": game}
    instance.config = SimpleNamespace(set=AsyncMock())
    instance._load_config = AsyncMock()
    instance._installer = SimpleNamespace(
        diagnose=Mock(
            return_value={"selected_path": game, "code": "READY", "message": "Ready"}
        ),
        select_game=Mock(return_value=game),
        install=Mock(
            return_value={"ok": True, "message": "Installed", "backup_path": "backup"}
        ),
        enable_bridge=Mock(return_value={"ok": True, "message": "Enabled"}),
        launch=Mock(return_value={"ok": True, "message": "Launch requested"}),
    )
    return instance


@pytest.mark.parametrize(
    "entry",
    [
        "select_game_entry",
        "install_mod_entry",
        "enable_bridge_entry",
        "launch_game_entry",
    ],
)
@pytest.mark.parametrize("token", ["", "invalid-token", None])
async def test_setup_actions_require_hosted_ui_token_before_any_service_call(
    plugin, entry, token
):
    result = await getattr(plugin, entry)(ui_token=token)

    assert isinstance(result, Err)
    assert result.error.code == "ui_only"
    for operation in vars(plugin._installer).values():
        operation.assert_not_called()
    plugin.config.set.assert_not_awaited()
    plugin._load_config.assert_not_awaited()


@pytest.mark.parametrize("entry", ["select_game_entry", "install_mod_entry"])
async def test_path_persistence_failure_prevents_install_or_launch(plugin, entry):
    selected = plugin._runtime_config["game_path"]
    plugin._runtime_config = {}
    plugin.config.set.side_effect = OSError("simulated persistence failure")
    kwargs = {"game_path": selected} if entry == "select_game_entry" else {}

    result = await getattr(plugin, entry)(ui_token=plugin._hosted_ui_token, **kwargs)

    assert isinstance(result, Err)
    assert result.error.code == "setup_failed"
    plugin.config.set.assert_awaited_once_with(
        "yui_stardew.game_path", selected, timeout=5.0
    )
    plugin._load_config.assert_not_awaited()
    plugin._installer.install.assert_not_called()
    plugin._installer.enable_bridge.assert_not_called()
    plugin._installer.launch.assert_not_called()


async def test_valid_selection_is_saved_without_installing_mod(plugin):
    selected = plugin._runtime_config["game_path"]
    plugin._runtime_config = {}

    result = await plugin.select_game_entry(
        game_path=selected, ui_token=plugin._hosted_ui_token
    )

    assert isinstance(result, Ok)
    plugin._installer.select_game.assert_called_once_with(selected)
    plugin.config.set.assert_awaited_once_with(
        "yui_stardew.game_path", selected, timeout=5.0
    )
    plugin._load_config.assert_awaited_once()
    plugin._installer.install.assert_not_called()


@pytest.mark.parametrize(
    ("entry", "operation"),
    [
        ("install_mod_entry", "install"),
        ("enable_bridge_entry", "enable_bridge"),
        ("launch_game_entry", "launch"),
    ],
)
async def test_authorized_setup_actions_return_service_result(plugin, entry, operation):
    service = getattr(plugin._installer, operation)

    result = await getattr(plugin, entry)(ui_token=plugin._hosted_ui_token)

    assert isinstance(result, Ok)
    assert result.unwrap() == service.return_value
    service.assert_called_once_with(plugin._runtime_config["game_path"])
    plugin.config.set.assert_not_awaited()


async def test_installer_error_keeps_actionable_code_and_message(plugin):
    plugin._installer.install.side_effect = SetupError(
        "GAME-RUNNING", "Please close the game"
    )

    result = await plugin.install_mod_entry(ui_token=plugin._hosted_ui_token)

    assert isinstance(result, Err)
    assert result.error.code == "GAME-RUNNING"
    assert str(result.error) == "Please close the game"


async def test_setup_snapshot_only_diagnoses_without_persisting_or_installing(plugin):
    state = await plugin._setup_snapshot()

    assert state == plugin._installer.diagnose.return_value
    plugin._installer.diagnose.assert_called_once_with(
        plugin._runtime_config["game_path"]
    )
    plugin.config.set.assert_not_awaited()
    plugin._installer.install.assert_not_called()
    plugin._installer.enable_bridge.assert_not_called()
    plugin._installer.launch.assert_not_called()
