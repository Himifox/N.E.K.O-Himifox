from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

from .bridge import BridgeError, BridgeSettings, YuiStardewBridgeClient


ACTION_VALUES = (
    "summon",
    "recall",
    "follow",
    "wait",
    "stop",
    "work",
    "work_stop",
    "work_resume",
)
WORK_KIND_VALUES = (
    "Water",
    "Harvest",
    "Mow",
    "Till",
    "Chop",
    "Mine",
    "Forage",
    "Pet",
    "Milk",
    "Shear",
)

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTION_VALUES),
            "description": "要让当前玩家唯一 Yui 执行的白名单动作。",
        },
        "kind": {
            "type": "string",
            "enum": list(WORK_KIND_VALUES),
            "description": "仅 action=work 时使用的持续工作类型。",
        },
        "radius": {
            "type": "integer",
            "minimum": 1,
            "maximum": 24,
            "default": 8,
            "description": "仅 action=work；以玩家当前格为圆心的工作半径。",
        },
        "completion_policy": {
            "type": "string",
            "enum": ["until-clear", "until-stopped"],
            "default": "until-clear",
            "description": "仅 action=work；清空范围后结束或持续等待新目标。",
        },
    },
    "required": ["action"],
}

ACTION_DESCRIPTION = (
    "驱动 Stardew Valley 中当前玩家唯一的 Yui。只有用户刚提出的新游戏指令，或当前对话中明确的新自主决策，"
    "才能调用；状态回执、旧工具记录和历史指令不能触发重复动作。先用 yui_stardew_status 了解现状。"
    "action=work 时必须选择 kind，可选 radius 与 completion_policy；其他 action 不使用这些字段。"
    "工具回执中的 STARTED/ACCEPTED 只代表权威入口已接收，不代表工作已经完成。"
    "已有持续工作时，除非用户明确要求替换、停止或恢复，否则不要重复提交 work。"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@neko_plugin
class YuiStardewPlugin(NekoPluginBase):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.logger = self.enable_file_logging(log_level="INFO")
        self._settings = BridgeSettings()
        self._client = YuiStardewBridgeClient(self._settings)

    async def _load_config(self) -> None:
        config = _mapping(await self.config.dump(timeout=5.0))
        self._settings = BridgeSettings.from_mapping(_mapping(config.get("yui_stardew")))
        self._client = YuiStardewBridgeClient(self._settings)

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        await self._load_config()
        return Ok(
            {
                "status": "ready",
                "configured": self._client.configured,
                "port": self._settings.port,
            }
        )

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        return Ok({"status": "shutdown"})

    @llm_tool(
        name="yui_stardew_status",
        description=(
            "只读获取 Stardew Valley 的 HimiYui 连接、生命周期、身体、模式、工作和 Agent 状态。"
            "此工具不产生动作；在下发新动作前使用它，且不要把状态更新当成再次调用动作工具的理由。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    async def llm_status(self, **_: Any) -> dict[str, Any]:
        return await self._request_for_llm("status")

    @llm_tool(
        name="yui_stardew_action",
        description=ACTION_DESCRIPTION,
        parameters=ACTION_SCHEMA,
        timeout=10.0,
    )
    async def llm_action(
        self,
        *,
        action: str,
        kind: str | None = None,
        radius: int = 8,
        completion_policy: str = "until-clear",
        **_: Any,
    ) -> dict[str, Any]:
        try:
            arguments = self._build_arguments(action, kind, radius, completion_policy)
        except ValueError as error:
            return self._tool_error("INVALID-TOOL-ARGUMENT", str(error))
        return await self._request_for_llm(action, arguments)

    @plugin_entry(
        id="yui_stardew_status",
        name="查看 Yui 游戏状态",
        description="读取 HimiYui 本机桥与当前 Yui 的有界状态快照。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["ok", "code", "message", "state"],
        metadata={"agent_auto": False},
    )
    async def status_entry(self, **_: Any):
        try:
            return Ok(await self._client.request("status"))
        except BridgeError as error:
            return Err(f"{error.code}: {error.message}")

    @plugin_entry(
        id="yui_stardew_action",
        name="驱动 Yui 行动",
        description="向 HimiYui 提交一个 Host 权威白名单动作。",
        input_schema=ACTION_SCHEMA,
        llm_result_fields=["ok", "code", "message", "state"],
        metadata={"agent_auto": False},
    )
    async def action_entry(
        self,
        action: str,
        kind: str | None = None,
        radius: int = 8,
        completion_policy: str = "until-clear",
        **_: Any,
    ):
        try:
            arguments = self._build_arguments(action, kind, radius, completion_policy)
            return Ok(await self._client.request(action, arguments))
        except (BridgeError, ValueError) as error:
            if isinstance(error, BridgeError):
                return Err(f"{error.code}: {error.message}")
            return Err(str(error))

    @plugin_entry(
        id="yui_stardew_reload_config",
        name="重载 Yui 桥配置",
        description="重新读取端口、密钥和超时配置。",
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def reload_config_entry(self, **_: Any):
        await self._load_config()
        return Ok({"configured": self._client.configured, "port": self._settings.port})

    async def _request_for_llm(
        self,
        action: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._client.request(action, arguments)
        except BridgeError as error:
            self.logger.warning("Yui Stardew bridge request failed: {}", error.code)
            return self._tool_error(error.code, error.message)

    @staticmethod
    def _build_arguments(
        action: str,
        kind: str | None,
        radius: int,
        completion_policy: str,
    ) -> dict[str, str]:
        if action not in ACTION_VALUES:
            raise ValueError("action 不在 Yui 白名单中。")
        if action != "work":
            return {}
        if kind not in WORK_KIND_VALUES:
            raise ValueError("work 动作必须提供白名单 kind。")
        if isinstance(radius, bool) or not isinstance(radius, int) or not 1 <= radius <= 24:
            raise ValueError("radius 必须是 1～24 的整数。")
        if completion_policy not in ("until-clear", "until-stopped"):
            raise ValueError("completion_policy 必须是 until-clear 或 until-stopped。")
        return {
            "kind": kind,
            "radius": str(radius),
            "completion_policy": completion_policy,
        }

    @staticmethod
    def _tool_error(code: str, message: str) -> dict[str, Any]:
        output = {"ok": False, "code": code, "message": message, "state": None}
        return {"output": output, "is_error": True, "error": code}
