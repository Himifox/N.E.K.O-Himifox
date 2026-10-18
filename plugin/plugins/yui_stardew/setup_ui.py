"""UI-only installation actions; never exposed as dialogue tools."""

from __future__ import annotations

import asyncio
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, ui

from .installer import Installer, SetupError


def _action_schema(*, select: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
    }
    if select:
        properties["game_path"] = {"type": "string", "minLength": 1, "maxLength": 4096}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class YuiStardewSetupUiMixin:
    def _init_setup_ui(self) -> None:
        self._installer = Installer()
        self._setup_lock = asyncio.Lock()

    async def _setup_snapshot(self) -> dict[str, Any]:
        selected = self._runtime_config.get("game_path", "")
        return await asyncio.to_thread(
            self._installer.diagnose, selected if isinstance(selected, str) else ""
        )

    async def _setup_action(self, action: str, ui_token: str, game_path: str = ""):
        if not self._valid_hosted_ui_token(ui_token):
            return Err(SdkError("该操作只能从插件面板调用。", code="ui_only"))
        async with self._setup_lock:
            try:
                if action == "select":
                    selected = await asyncio.to_thread(
                        self._installer.select_game, game_path
                    )
                else:
                    snapshot = await self._setup_snapshot()
                    selected = snapshot["selected_path"]
                    if not selected:
                        raise SetupError(snapshot["code"], snapshot["message"])
                if self._runtime_config.get("game_path") != selected:
                    await self.config.set(
                        "yui_stardew.game_path", selected, timeout=5.0
                    )
                    await self._load_config()
                if action == "select":
                    return Ok(
                        {"ok": True, "message": "已保存游戏位置，请检查安装状态。"}
                    )
                operation = {
                    "install": self._installer.install,
                    "enable": self._installer.enable_bridge,
                    "launch": self._installer.launch,
                }[action]
                return Ok(await asyncio.to_thread(operation, selected))
            except SetupError as error:
                return Err(SdkError(error.message, code=error.code))
            # Configuration persistence can fail independently of game access.
            except Exception:
                return Err(
                    SdkError(
                        "操作未完成，请刷新面板并检查配置目录权限。",
                        code="setup_failed",
                    )
                )

    @ui.action(
        id="yui_stardew_select_game",
        label="选择游戏位置",
        group="setup",
        order=1,
        refresh_context=True,
    )
    @plugin_entry(
        id="yui_stardew_select_game",
        name="选择 Steam 星露谷位置",
        input_schema=_action_schema(select=True),
        metadata={"agent_auto": False, "ui_only": True},
    )
    async def select_game_entry(
        self, game_path: str = "", ui_token: str = "", **_: object
    ):
        return await self._setup_action("select", ui_token, game_path)

    @ui.action(
        id="yui_stardew_install_mod",
        label="安装并连接星露谷",
        group="setup",
        order=2,
        refresh_context=True,
    )
    @plugin_entry(
        id="yui_stardew_install_mod",
        name="安装配套 Yui 模组并开启桥接",
        input_schema=_action_schema(),
        timeout=60.0,
        metadata={"agent_auto": False, "ui_only": True},
    )
    async def install_mod_entry(self, ui_token: str = "", **_: object):
        return await self._setup_action("install", ui_token)

    @ui.action(
        id="yui_stardew_enable_bridge",
        label="开启桥接",
        group="setup",
        order=3,
        refresh_context=True,
    )
    @plugin_entry(
        id="yui_stardew_enable_bridge",
        name="备份配置并开启 Yui 桥接",
        input_schema=_action_schema(),
        timeout=60.0,
        metadata={"agent_auto": False, "ui_only": True},
    )
    async def enable_bridge_entry(self, ui_token: str = "", **_: object):
        return await self._setup_action("enable", ui_token)

    @ui.action(
        id="yui_stardew_launch_game",
        label="启动游戏",
        group="setup",
        order=4,
        refresh_context=True,
    )
    @plugin_entry(
        id="yui_stardew_launch_game",
        name="通过 SMAPI 启动星露谷",
        input_schema=_action_schema(),
        metadata={"agent_auto": False, "ui_only": True},
    )
    async def launch_game_entry(self, ui_token: str = "", **_: object):
        return await self._setup_action("launch", ui_token)
