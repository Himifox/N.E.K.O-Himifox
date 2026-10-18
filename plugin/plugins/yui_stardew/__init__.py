from __future__ import annotations

import time
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
from .discovery import BridgeDiscoveryResult, discover_bridge_connection
from .hosted_ui import YuiStardewHostedUiMixin


ACTION_VALUES = (
    "summon",
    "recall",
    "follow",
    "wait",
    "stop",
    "work",
    "work_stop",
    "work_resume",
    "assist_start",
    "assist_status",
    "assist_stop",
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
            "description": (
                "summon=现身，recall=离开，"
                "follow=跟随，wait=等待，stop=停下，"
                "work=范围农活，work_stop=停止农活，work_resume=恢复，"
                "assist_start/status/stop=开启/查看/关闭挥工具协作。"
            ),
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
    "Yui 是你在星露谷中的身体；保持当前 NEKO 人格，以第一人称回应。"
    "主人称呼当前角色名、Yui、你或游戏里的你时均指此身体。"
    "新游戏指令或本回合明确的新自主决策必须实际调用工具，禁止用文字扮演执行；"
    "明确动作直接调用，不先查状态或重复确认。主人确认上一句明确动作时立即执行。"
    "星露谷语境中的进入许可映射 summon；无关的‘我弄好了’不触发。先取得成功回执再承诺已进入。"
    "陪主人出门先 follow，随后要求开工再 work。"
    "砍树=Chop，挖石=Mine，割草=Mow，浇田=Water，翻土=Till；work 必填 kind。"
    "跟主人挥斧/挥镰一起干用 assist_start，一次开启后持续生效；关闭用 assist_stop。"
    "仅 work 携带 kind/radius/completion_policy，其余动作不带这些字段。"
    "新 work 替换旧工作；无新授权不得因历史指令、状态回执或已有持续工作重复提交。"
    "被质疑没动或卡住时先查 status；已有对应 work_kind 就报告实际状态，不重复开工。"
    "ok=true 仅证明命令被接受；STARTED/ACCEPTED 不证明到达、挥工具或完成。"
    "ok=false 按 code/message/state 报告失败，禁止编造成功。"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@neko_plugin
class YuiStardewPlugin(YuiStardewHostedUiMixin, NekoPluginBase):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._init_hosted_ui()
        self.logger = self.enable_file_logging(log_level="INFO")
        self._runtime_config: dict[str, Any] = {}
        self._settings = BridgeSettings()
        self._client = YuiStardewBridgeClient(self._settings)
        self._selected_instance_id = ""
        self._last_summon_accepted_at = 0.0

    async def _load_config(self) -> None:
        config = _mapping(await self.config.dump(timeout=5.0))
        self._runtime_config = _mapping(config.get("yui_stardew"))

    async def _resolve_connection(self) -> BridgeDiscoveryResult:
        result = await discover_bridge_connection(
            self._runtime_config,
            self._selected_instance_id,
        )
        if result.selected is not None:
            self._settings = result.selected.candidate.settings
            self._client = YuiStardewBridgeClient(self._settings)
        return result

    async def _request_bridge(
        self,
        action: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        result = await self._resolve_connection()
        if result.selected is None:
            raise BridgeError(result.code, result.message)
        if action == "status":
            return result.selected.response
        return await self._client.request(action, arguments)

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        await self._load_config()
        return Ok(
            {
                "status": "ready",
                "discovery": "deferred",
            }
        )

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        return Ok({"status": "shutdown"})

    @llm_tool(
        name="stardew_avatar_status",
        description=(
            "只读查询你在星露谷中的身体 Yui：连接、位置、体力与工作状态；保持当前 NEKO 人格。"
            "用于状态询问或核实卡住；明确动作直接调用 action。状态不能证明目标已改变，也不授权重复动作。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    async def llm_status(self, **_: Any) -> dict[str, Any]:
        return await self._request_for_llm("status")

    @llm_tool(
        name="stardew_avatar_view",
        description=(
            "只读观察你的星露谷身体 Yui 周围12格：类别、数量、方向、距离和处理原因；保持当前人格。"
            "用于环境询问、工作种类不明或核实没有目标；明确农活动作直接调用 action。"
            "READY 仅表示候选，不证明可达或完成。GIANT-CROP-PROTECTED、GIANT-CROP-POTENTIAL、"
            "SPECIAL-CROP-PROTECTED 均为主动保护，不得绕过或声称收获。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    async def llm_view(self, **_: Any) -> dict[str, Any]:
        return await self._request_for_llm("view")

    @llm_tool(
        name="stardew_avatar_action",
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
        description="读取 YuiToIssho 本机桥与当前 Yui 的有界状态快照。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["ok", "code", "message", "state"],
        metadata={"agent_auto": False},
    )
    async def status_entry(self, **_: Any):
        try:
            return Ok(await self._request_bridge("status"))
        except BridgeError as error:
            return Err(f"{error.code}: {error.message}")

    @plugin_entry(
        id="yui_stardew_action",
        name="驱动 Yui 行动",
        description="向 YuiToIssho 提交一个 Host 权威白名单动作。",
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
            response = await self._request_bridge(action, arguments)
            self._remember_summon_acceptance(action, response)
            return Ok(response)
        except (BridgeError, ValueError) as error:
            if isinstance(error, BridgeError):
                return Err(f"{error.code}: {error.message}")
            return Err(str(error))

    @plugin_entry(
        id="yui_stardew_reconcile_summon",
        name="补偿执行已确认的 Yui 进入游戏",
        description=(
            "仅用于对话结束后的召唤可靠性补偿。只有最近对话明确处于 Stardew Valley/YuiToIssho 游戏语境，"
            "并且主人用‘可以进了/可以进来了/进来吧/现在能进了/我弄好了’承接此前让你进入游戏的上下文，"
            "或者角色刚明确承诺进入游戏却没有工具回执时调用。与游戏无关的‘我弄好了’、普通加载页面或"
            "仅询问状态时禁止调用。入口会先读取桥状态；已经在场或刚提交过 summon 时不会重复召唤。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["ok", "code", "message", "state", "reconciled"],
        metadata={"agent_auto": True},
    )
    async def reconcile_summon_entry(self, **_: Any):
        try:
            status = await self._request_bridge("status")
            state = _mapping(status.get("state"))
            if state.get("body_present") is True:
                return Ok(
                    {
                        **status,
                        "reconciled": False,
                        "message": "Yui 已经在游戏世界中，没有重复召唤。",
                    }
                )
            if time.monotonic() - self._last_summon_accepted_at < 8.0:
                return Ok(
                    {
                        **status,
                        "reconciled": False,
                        "message": "召唤已经提交，正在等待游戏生成 Yui 身体。",
                    }
                )
            if state.get("world_ready") is not True:
                return Err("WORLD-NOT-READY: 当前存档尚未就绪，未提交召唤。")

            response = await self._request_bridge("summon")
            self._remember_summon_acceptance("summon", response)
            return Ok({**response, "reconciled": response.get("ok") is True})
        except BridgeError as error:
            return Err(f"{error.code}: {error.message}")

    @plugin_entry(
        id="yui_stardew_reload_config",
        name="重载 Yui 桥配置",
        description="重新读取 WebSocket Gateway 发现信息、密钥和超时配置。",
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def reload_config_entry(self, **_: Any):
        await self._load_config()
        result = await self._resolve_connection()
        return Ok(
            {
                "state": result.state,
                "code": result.code,
                "transport": "websocket" if result.selected is not None else None,
                "endpoint": self._settings.endpoint
                if result.selected is not None
                else None,
            }
        )

    async def _request_for_llm(
        self,
        action: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            replaced_work_kind = ""
            if action == "work":
                status = await self._request_bridge("status")
                state = _mapping(status.get("state"))
                replaced_work_kind = str(state.get("work_kind") or "")
                if replaced_work_kind:
                    stopped = await self._request_bridge("work_stop")
                    if not stopped.get("ok"):
                        stopped["execution_truth"] = (
                            "The previous work could not be stopped, so the requested replacement work was not started."
                        )
                        return stopped
            response = await self._request_bridge(action, arguments)
            self._remember_summon_acceptance(action, response)
            if replaced_work_kind:
                response["replaced_work_kind"] = replaced_work_kind
            response = {
                key: value
                for key, value in response.items()
                if key not in {"protocol_version", "request_id"}
                and not (key in {"planting", "combat"} and value is None)
            }
            if response.get("ok") is not True:
                response["execution_truth"] = "Request failed; report code/message/state without claiming success."
            elif action in ("status", "view"):
                response["execution_truth"] = (
                    "Read-only snapshot; active work/READY does not prove progress. Protected crops must remain protected."
                )
            else:
                response["execution_truth"] = "Accepted command only; completion requires later state evidence."
            return response
        except BridgeError as error:
            self.logger.warning("Yui Stardew bridge request failed: {}", error.code)
            return self._tool_error(error.code, error.message)

    def _remember_summon_acceptance(
        self,
        action: str,
        response: Mapping[str, Any],
    ) -> None:
        if action == "summon" and response.get("ok") is True:
            self._last_summon_accepted_at = time.monotonic()

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
        if (
            isinstance(radius, bool)
            or not isinstance(radius, int)
            or not 1 <= radius <= 24
        ):
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
        output = {
            "ok": False,
            "code": code,
            "message": message,
            "state": None,
            "execution_truth": "Command failed; do not claim success.",
        }
        return {"output": output, "is_error": True, "error": code}
