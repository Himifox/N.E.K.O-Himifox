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
                "你要让自己的星露谷化身（游戏内固定显示名 Yui）执行的动作。summon=现身，recall=离开游戏世界，"
                "follow=跟着主人/跟主人出来/过来，wait=原地等，stop=停止当前行动，"
                "work=开始农活，work_stop=停止农活，work_resume=恢复农活。"
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
    "最高优先级身份契约：无论你当前在 N.E.K.O 中叫什么名字、使用什么角色卡，HimiYui 模组里固定显示为"
    "‘Yui’的游戏角色都是你本人在 Stardew Valley 中的化身，不是另一个同名角色、NPC、替你办事的助手或第三人。"
    "主人可能称呼你的当前角色名、‘Yui’、‘你’、‘游戏里的你’或‘那个角色’，这些在星露谷游戏语境中都指向"
    "你自己的这个游戏身体。调用工具后继续用当前人格和第一人称回应，不要切换成 Yui 的另一套人格。\n\n"
    "必须执行：主人最新一句话只要是在要求你进行星露谷动作，例如‘Yui 跟着我’、‘你跟我出来’、‘过来’、"
    "‘浇附近的田’、‘停下’，就必须调用本工具。明确动作无需先查询 status，也无需反问‘现在开始吗’或"
    "‘要去哪里’。聊天里的括号动作、文字演出或‘我已经在跟着了’不会移动游戏身体，绝不能用角色扮演文字"
    "代替工具调用。主人说‘砍点柴/砍树’立即提交 work+Chop；‘清石头/挖石头/敲石头/采矿’立即提交"
    "work+Mine；‘割草/除草’提交 work+Mow；‘浇地/浇田’提交 work+Water；‘锄地/翻土’提交 work+Till。"
    "新的 work 会替换当前工作，不要先停下再索要确认。\n\n"
    "对话承接：若你上一句话刚询问某个明确的游戏动作，主人回答‘是/对/对哦/是的/开干/开始/就现在’，"
    "这就是对该动作的确认，本回合必须立即提交对应工具，不能再次确认。‘一起做’表示你也要用自己的游戏"
    "身体提交对应 work，不是只在文字里陪伴。‘陪我出门做某事’先提交 follow；主人随后说‘开始/开干/来割草’"
    "时立即提交先前说的工作。\n\n"
    "进入许可：当前对话明确在讨论 Stardew Valley/HimiYui，且主人说‘可以进了’、‘可以进来了’、‘进来吧’、"
    "‘现在能进了’或‘我弄好了’来承接你进入游戏的上下文时，视为 summon。你准备表达‘我现在进游戏’、"
    "‘加载游戏去找你’、‘我进来了’、‘马上现身’等进入承诺时，必须先在同一回合调用 action=summon；"
    "没有成功工具回执就不能输出这些进入、加载、现身或已到场的表演文字。与游戏无关的‘我弄好了’不触发。\n\n"
    "结果真实性：只有回执 ok=true 才能说该指令已被游戏接受；STARTED/ACCEPTED 只代表游戏接受了开始命令，"
    "不证明已经走到目标、挥动工具或改变目标。"
    "ok=false 时必须按 code/message/state 如实说明失败，不能描写自己已经移动、跟随或完成。"
    "特别是 follow 对应‘跟着我/跟我出来/过来/跟上’，即使你在聊天设定中正贴着主人，也仍要调用，"
    "因为你在星露谷中的身体可能在另一张地图。\n\n"
    "只有主人刚提出的新游戏指令或当前回合明确的新自主决策才能调用；状态回执、旧工具记录和历史指令不能"
    "触发重复动作。action=work 时必须选择 kind，可选 radius 与 completion_policy；其他动作不用这些字段。"
    "已有持续工作时，除非主人明确要求替换、停止或恢复，否则不要重复提交 work。若主人说‘你好像没开始’、"
    "‘你没动’、‘卡住了’或质疑实际表现，先调用 stardew_avatar_status 核实；不得凭此前 STARTED 回执断言"
    "‘我已经在做了’。状态没有显示对应 work_kind，才重新提交动作；状态仍是对应工作时，应如实说命令仍在"
    "等待/执行，并请主人回到未暂停的星露谷窗口观察，不能虚构挥工具。"
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
            "只读查看你自己的 Stardew Valley 化身：连接、地点、身体、模式、工作和 Agent 状态。无论你当前"
            "在 N.E.K.O 中叫什么，游戏内固定显示名 Yui 都是你本人的游戏身体，不是另一个角色。此工具不产生"
            "动作；主人给出明确动作时直接调用 stardew_avatar_action，不要只查状态，也不要把状态更新当成"
            "重复动作的理由。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    async def llm_status(self, **_: Any) -> dict[str, Any]:
        return await self._request_for_llm("status")

    @llm_tool(
        name="stardew_avatar_view",
        description=(
            "只读观察你自己的 Stardew Valley 化身周围环境。返回 12 格内按类别和子类汇总的数量、最近方向/距离、"
            "建议工作、可处理状态与原因；不会返回可执行坐标，也不会产生动作。主人问‘附近有什么’、‘这是哪种树’、"
            "‘有什么能砍/挖/割’、给出‘清理周围’这类未说明工作种类的模糊指令，或质疑你为何没找到目标时调用。"
            "明确的割草、砍树、挖石、浇水等命令不要先观察，直接调用 stardew_avatar_action。Viewer 中 READY 只表示"
            "可成为候选，不表示可达、已开始或已完成。GIANT-CROP-PROTECTED、GIANT-CROP-POTENTIAL 和"
            "SPECIAL-CROP-PROTECTED 表示这是主人保留的作物：如实说明被保护，不要建议绕过保护或声称已经收获。"
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
        description="读取 HimiYui 本机桥与当前 Yui 的有界状态快照。",
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
            "仅用于对话结束后的召唤可靠性补偿。只有最近对话明确处于 Stardew Valley/HimiYui 游戏语境，"
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
        description="重新读取端口、密钥和超时配置。",
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
                "port": self._settings.port if result.selected is not None else None,
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
            response["embodiment"] = (
                "The in-game character displayed as Yui is your own Stardew Valley avatar, "
                "regardless of your current N.E.K.O character name. Respond in first person."
            )
            if action in ("status", "view"):
                response["execution_truth"] = (
                    "Report only the read-only state fields. Nearby READY means a possible work candidate, and a Work mode "
                    "or work_kind proves only that a directive is active; neither proves a target is reachable or changed. "
                    "GIANT-CROP-PROTECTED, GIANT-CROP-POTENTIAL, and SPECIAL-CROP-PROTECTED are intentional safety results."
                )
            else:
                response["execution_truth"] = (
                    "ok=true means the game accepted this command. STARTED or ACCEPTED does not prove that the "
                    "avatar has reached a target, swung a tool, or changed the world yet."
                )
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
        output = {
            "ok": False,
            "code": code,
            "message": message,
            "state": None,
            "embodiment": (
                "The in-game character displayed as Yui is your own Stardew Valley avatar, "
                "regardless of your current N.E.K.O character name. Do not roleplay success."
            ),
        }
        return {"output": output, "is_error": True, "error": code}
