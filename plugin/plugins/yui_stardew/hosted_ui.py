"""Hosted UI connection diagnostics for the HimiYui bridge."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, ui

from .bridge import PROTOCOL_VERSION, BridgeError
from .discovery import BridgeDiscoveryResult, discover_bridge_connection


class YuiStardewHostedUiMixin:
    """Expose connection management without creating a game-control surface."""

    _runtime_config: dict[str, Any]
    _selected_instance_id: str
    _hosted_ui_token: str
    config: Any

    def _init_hosted_ui(self) -> None:
        self._hosted_ui_token = secrets.token_urlsafe(32)

    def _valid_hosted_ui_token(self, candidate: object) -> bool:
        if not isinstance(candidate, str):
            return False
        try:
            return secrets.compare_digest(candidate, self._hosted_ui_token)
        except TypeError:
            return False

    async def _hosted_discovery(self) -> BridgeDiscoveryResult:
        return await discover_bridge_connection(
            self._runtime_config,
            self._selected_instance_id,
        )

    @ui.context(id="dashboard", title="Yui 星露谷驱动")
    async def get_dashboard_ui_context(self) -> dict[str, Any]:
        try:
            discovery = await self._hosted_discovery()
            discovery_state = discovery.ui_state(self._runtime_config)
        except BridgeError as error:
            discovery = None
            discovery_state = {
                "state": "invalid",
                "code": error.code,
                "message": error.message,
                "source": "compatibility_override",
                "port": None,
                "instance_id": "",
                "protocol_version": PROTOCOL_VERSION,
                "verified_at": "",
                "candidates": [],
                "compatibility_override": True,
                "open_config_directory": "",
            }

        avatar_state: dict[str, Any] = {}
        connection = {
            "reachable": discovery is not None and discovery.selected is not None,
            "ok": discovery is not None and discovery.selected is not None,
            "code": discovery_state["code"],
            "message": discovery_state["message"],
        }
        if discovery is not None and discovery.selected is not None:
            state = discovery.selected.response.get("state")
            if isinstance(state, Mapping):
                avatar_state = dict(state)

        return {
            "protocol_version": PROTOCOL_VERSION,
            "discovery": discovery_state,
            "connection": connection,
            "avatar": avatar_state,
            "ui_token": self._hosted_ui_token,
        }

    @ui.action(
        id="yui_stardew_select_instance",
        label="选择当前游戏实例",
        icon="🎮",
        tone="success",
        group="connection",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="yui_stardew_select_instance",
        name="选择当前 HimiYui 实例",
        description="仅供 Hosted UI 在多个已验证的本机实例中选择本次会话目标。",
        input_schema={
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
            },
            "required": ["instance_id", "ui_token"],
            "additionalProperties": False,
        },
        metadata={"agent_auto": False, "ui_only": True},
    )
    async def select_instance_entry(
        self,
        instance_id: str = "",
        ui_token: str = "",
        **_: object,
    ):
        if not self._valid_hosted_ui_token(ui_token):
            return Err(SdkError("该操作只能从插件 Hosted UI 调用。", code="ui_only"))
        try:
            result = await discover_bridge_connection(self._runtime_config, instance_id)
        except BridgeError as error:
            return Err(SdkError(error.message, code=error.code))
        if result.selected is None or result.selected.candidate.instance_id != instance_id:
            return Err(SdkError("该实例已离线或未通过验证，请刷新后重试。", code="instance_unavailable"))
        self._selected_instance_id = instance_id
        return Ok({"selected": True, "instance_id": instance_id})

    @ui.action(
        id="yui_stardew_clear_compatibility_override",
        label="清除兼容覆盖",
        icon="↺",
        tone="default",
        group="connection",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="yui_stardew_clear_compatibility_override",
        name="清除 Yui 桥兼容覆盖",
        description="仅供 Hosted UI 清除旧版路径与手动密钥，恢复自动发现。",
        input_schema={
            "type": "object",
            "properties": {
                "ui_token": {"type": "string", "writeOnly": True, "minLength": 1},
            },
            "required": ["ui_token"],
            "additionalProperties": False,
        },
        metadata={"agent_auto": False, "ui_only": True},
    )
    async def clear_compatibility_override_entry(
        self,
        ui_token: str = "",
        **_: object,
    ):
        if not self._valid_hosted_ui_token(ui_token):
            return Err(SdkError("该操作只能从插件 Hosted UI 调用。", code="ui_only"))
        try:
            await self.config.set("yui_stardew.himi_yui_config_path", "", timeout=5.0)
            await self.config.set("yui_stardew.token", "", timeout=5.0)
            await self._load_config()
        except Exception as error:  # noqa: BLE001
            return Err(SdkError(f"清除兼容覆盖失败：{error}", code="config_save_failed"))
        self._selected_instance_id = ""
        return Ok({"cleared": True})


__all__ = ["YuiStardewHostedUiMixin"]
