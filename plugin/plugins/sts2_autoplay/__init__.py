from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Optional
import math
import os

from plugin.sdk.plugin import Err, NekoPluginBase, Ok, SdkError, lifecycle, llm_tool, neko_plugin, plugin_entry

from .service import STS2AutoplayService

_CONFIG_FILE = Path(__file__).with_name("plugin.toml")
_SOURCE_ID = "sts2_autoplay"
_DEFAULT_PRIORITY = 5
_DEFAULT_MESSAGE_TYPE = "neko_observation"
_DEFAULT_SCOPE = "auto"
_DEFAULT_STOP_CONDITION = "current_floor"
_DEFAULT_HISTORY_LIMIT = 20
_DEFAULT_GUIDANCE_TYPE = "soft_guidance"
_DEFAULT_MODE = "half-program"
_DEFAULT_CHARACTER_STRATEGY = "defect"
_MAX_HISTORY_LIMIT = 100

JsonObject = dict[str, Any]
AsyncPayloadFactory = Callable[[], Awaitable[JsonObject]]


def _as_mapping(value: Any) -> JsonObject:
    return dict(value) if isinstance(value, Mapping) else {}


def _summary_from(payload: Mapping[str, Any]) -> str:
    return str(payload.get("summary") or payload.get("message") or payload.get("content") or "")


def _finite_float(value: Any, *, key: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise SdkError(f"非法配置值: {key}={value}")
    return number


def _optional_finite_float(value: Optional[float], *, key: str) -> Optional[float]:
    return None if value is None else _finite_float(value, key=key)


def _replace_toml_number(text: str, *, key: str, value: float) -> str:
    needle = f"{key} ="
    replacement = f"{key} = {value:g}"
    found = False
    updated_lines: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith(needle):
            found = True
            updated_lines.append(replacement)
        else:
            updated_lines.append(line)

    if not found:
        raise SdkError(f"plugin.toml 中未找到配置项: {key}")

    return "\n".join(updated_lines) + ("\n" if text.endswith("\n") else "")


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    with NamedTemporaryFile("w", encoding=encoding, dir=path.parent, delete=False) as temp_file:
        temp_file.write(text)
        temp_path = Path(temp_file.name)
    os.replace(temp_path, path)


@neko_plugin
class STS2AutoplayPlugin(NekoPluginBase):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._cfg: JsonObject = {}
        self._service = STS2AutoplayService(
            self.logger,
            self.report_status,
            self._push_frontend_notification,
        )

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        cfg = _as_mapping(await self.config.dump(timeout=5.0))
        self._cfg = _as_mapping(cfg.get("sts2"))
        await self._service.startup(self._cfg)
        return Ok({"status": "ready", "result": await self._service.get_status()})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        await self._service.shutdown()
        return Ok({"status": "shutdown"})

    # ---- LLM-callable tools (registered via @llm_tool, see PR #1055) -----
    # Only two tools are exposed to the LLM: a stop-only safety valve and
    # a read-only status probe. All advice/play/start/pause/resume/review/
    # guidance flows must go through agent-brain plugin entries above.

    @llm_tool(
        name="sts2_autoplay_control",
        description=(
            "仅用于停止已经运行或暂停中的杀戮尖塔自动游玩任务。"
            "本工具只接受 action=stop；不能启动、暂停、恢复、查询状态或执行游戏动作。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["stop"],
                    "description": "唯一允许的控制动作：stop",
                },
            },
            "required": ["action"],
        },
        timeout=10.0,
    )
    async def llm_autoplay_control(self, action: str = "", **_: Any) -> JsonObject:
        if action == "stop":
            return await self._service.stop_autoplay()
        return {
            "status": "rejected",
            "message": "sts2_autoplay_control 只允许 action=stop，已拒绝非停止类 tool-call。",
            "summary": "已拒绝非停止类 tool-call。",
            "executed": False,
            "allowed_actions": ["stop"],
        }

    @llm_tool(
        name="sts2_get_status",
        description=(
            "只读获取杀戮尖塔连接状态、自动游玩状态、当前界面和最近错误。"
            "本工具不会启动、停止、暂停、恢复或执行任何游戏动作。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    async def llm_get_status(self, **_: Any) -> JsonObject:
        return await self._service.get_status()

    async def _run_entry(self, action: AsyncPayloadFactory, *, finish: bool = False):
        try:
            payload = await action()
            if finish:
                return await self.finish(data=payload, reply=False, message=_summary_from(payload))
            return Ok(payload)
        except SdkError as error:
            self.logger.warning(f"STS2 plugin entry failed: {error}")
            return Err(str(error))
        except Exception as error:
            self.logger.exception("Unexpected STS2 plugin entry failure")
            return Err(f"尖塔插件内部错误: {error}")

    def _push_frontend_notification(
        self,
        *,
        content: str,
        description: str,
        metadata: JsonObject,
        priority: int = _DEFAULT_PRIORITY,
        message_type: str = _DEFAULT_MESSAGE_TYPE,
    ) -> None:
        self.push_message(
            source=_SOURCE_ID,
            message_type=message_type,
            description=description,
            priority=priority,
            content=content,
            metadata=dict(metadata),
        )

    def _save_speed_overrides(
        self,
        *,
        post_action_delay_seconds: Optional[float] = None,
        poll_interval_active_seconds: Optional[float] = None,
        action_interval_seconds: Optional[float] = None,
    ) -> None:
        updates = {
            key: value
            for key, value in {
                "post_action_delay_seconds": post_action_delay_seconds,
                "poll_interval_active_seconds": poll_interval_active_seconds,
                "action_interval_seconds": action_interval_seconds,
            }.items()
            if value is not None
        }
        if not updates:
            return

        normalized_updates = {
            key: _finite_float(value, key=key)
            for key, value in updates.items()
        }
        text = _CONFIG_FILE.read_text(encoding="utf-8")
        updated_text = text
        for key, value in normalized_updates.items():
            updated_text = _replace_toml_number(updated_text, key=key, value=value)

        _atomic_write_text(_CONFIG_FILE, updated_text)
        self._cfg = {**self._cfg, **normalized_updates}

    @plugin_entry(id="sts2_status", name="获取尖塔状态", description="获取尖塔连接状态、自动游玩状态、当前快照，并可选刷新或附带历史。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"refresh": {"type": "boolean", "default": False}, "include_snapshot": {"type": "boolean", "default": True}, "include_history": {"type": "boolean", "default": False}, "history_limit": {"type": "integer", "default": _DEFAULT_HISTORY_LIMIT}}, "required": []}, metadata={"agent_auto": False})
    async def sts2_status(
        self,
        refresh: bool = False,
        include_snapshot: bool = True,
        include_history: bool = False,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
        **_: Any,
    ):
        async def action() -> JsonObject:
            try:
                raw_limit = int(history_limit)
            except (TypeError, ValueError):
                raw_limit = _DEFAULT_HISTORY_LIMIT
            safe_limit = max(1, min(raw_limit, _MAX_HISTORY_LIMIT))
            payload = await self._service.get_status_bundle(
                refresh=bool(refresh),
                include_snapshot=bool(include_snapshot),
                include_history=bool(include_history),
                history_limit=safe_limit,
            )
            server_state = str(_as_mapping(payload.get("server")).get("state") or "unknown")
            autoplay_state = str(_as_mapping(payload.get("autoplay")).get("state") or "unknown")
            return {**payload, "message": f"{server_state} | autoplay={autoplay_state}"}

        return await self._run_entry(action)

    @plugin_entry(id="sts2_step_once", name="执行一步", description="根据当前策略执行一步尖塔合法动作。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {}})
    async def sts2_step_once(self, **_: Any):
        return await self._run_entry(self._service.step_once, finish=True)

    @plugin_entry(id="sts2_neko_command", name="尖塔猫娘指令", description="杀戮尖塔普通用户自然语言总入口。用户没有明确指定底层工具时优先调用本入口；它会根据用户原话自动判断查看状态、给建议、打一张牌、执行一步、开启自动游玩、暂停、恢复、停止或发送软指导。默认咨询不操作，只有用户明确授权时才执行游戏动作。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"command": {"type": "string", "description": "用户原话，例如：这回合怎么打、帮我打一张牌、先防一下、暂停一下"}, "scope": {"type": "string", "default": _DEFAULT_SCOPE, "description": "可选意图提示：auto/status/advice/one_card/one_action/autoplay/control/guidance"}, "confirm": {"type": "boolean", "default": False, "description": "是否已确认允许持续托管等高风险操作"}}, "required": ["command"]})
    async def sts2_neko_command(self, command: str, scope: str = _DEFAULT_SCOPE, confirm: bool = False, **_: Any):
        return await self._run_entry(
            lambda: self._service.neko_command(command=command.strip(), scope=scope, confirm=confirm),
            finish=True,
        )

    @plugin_entry(id="sts2_recommend_one_card_by_neko", name="猫娘推荐一张牌", description="当用户询问杀戮尖塔当前打哪张牌好、帮忙看看出牌建议时调用：插件只会读取玩家/手牌/敌人状态并推荐一张 play_card，说明理由，不会自动打出卡牌。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"objective": {"type": "string", "description": "用户咨询目标，例如：帮我看看当前打哪张牌好"}}})
    async def sts2_recommend_one_card_by_neko(self, objective: Optional[str] = None, **_: Any):
        return await self._run_entry(lambda: self._service.recommend_one_card_by_neko(objective=objective), finish=True)

    @plugin_entry(id="sts2_play_one_card_by_neko", name="猫娘选择并打出一张牌", description="仅当用户明确授权插件实际操作、自动打出、代打或说帮我选一张牌打出去时调用：插件会读取玩家/手牌/敌人状态，选择一张 play_card，先通知将要打出的卡牌和理由，然后执行出牌。用户只是问打哪张牌好、想要建议时不要调用本入口，应调用 sts2_recommend_one_card_by_neko。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"objective": {"type": "string", "description": "用户授权目标，例如：帮我选一张牌打出去"}}})
    async def sts2_play_one_card_by_neko(self, objective: Optional[str] = None, **_: Any):
        return await self._run_entry(lambda: self._service.play_one_card_by_neko(objective=objective), finish=True)

    @plugin_entry(id="sts2_control", name="控制尖塔游玩", description="控制尖塔自动游玩生命周期：开始、暂停、恢复或停止。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["start", "pause", "resume", "stop"]}, "objective": {"type": "string", "description": "仅 start 时使用的用户目标"}, "stop_condition": {"type": "string", "default": _DEFAULT_STOP_CONDITION, "description": "仅 start 时使用：current_floor/current_combat/manual"}}, "required": ["action"]})
    async def sts2_control(
        self,
        action: Literal["start", "pause", "resume", "stop"],
        objective: Optional[str] = None,
        stop_condition: str = _DEFAULT_STOP_CONDITION,
        **_: Any,
    ):
        normalized_action = action.strip().lower()
        if normalized_action == "start":
            return await self._run_entry(lambda: self._service.start_autoplay(objective=objective, stop_condition=stop_condition), finish=True)
        if normalized_action == "pause":
            return await self._run_entry(self._service.pause_autoplay, finish=True)
        if normalized_action == "resume":
            return await self._run_entry(self._service.resume_autoplay, finish=True)
        if normalized_action == "stop":
            return await self._run_entry(self._service.stop_autoplay, finish=True)
        return Err(f"不支持的 control action: {action}")

    @plugin_entry(id="sts2_send_neko_guidance", name="发送Neko指导", description="向后台 autoplay 发送猫娘的软指导，会在下一轮决策时被 LLM 参考。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"content": {"type": "string", "description": "猫娘的指导内容，自然语言"}, "step": {"type": "integer", "description": "对应的步数（可选）"}, "type": {"type": "string", "default": _DEFAULT_GUIDANCE_TYPE}}, "required": ["content"]})
    async def sts2_send_neko_guidance(self, content: str, step: Optional[int] = None, type: str = _DEFAULT_GUIDANCE_TYPE, **_: Any):
        guidance = {"content": content.strip(), "step": step, "type": type}
        return await self._run_entry(lambda: self._service.send_neko_guidance(guidance), finish=True)

    @plugin_entry(id="sts2_configure", name="配置尖塔游玩", description="设置尖塔模式、角色策略或运行速度。", llm_result_fields=["summary"], input_schema={"type": "object", "properties": {"mode": {"type": "string", "default": _DEFAULT_MODE}, "character_strategy": {"type": "string", "default": _DEFAULT_CHARACTER_STRATEGY}, "action_interval_seconds": {"type": "number"}, "post_action_delay_seconds": {"type": "number"}, "poll_interval_active_seconds": {"type": "number"}}})
    async def sts2_configure(
        self,
        mode: Optional[str] = None,
        character_strategy: Optional[str] = None,
        action_interval_seconds: Optional[float] = None,
        post_action_delay_seconds: Optional[float] = None,
        poll_interval_active_seconds: Optional[float] = None,
        **_: Any,
    ):
        async def action() -> JsonObject:
            if (
                mode is None
                and character_strategy is None
                and action_interval_seconds is None
                and post_action_delay_seconds is None
                and poll_interval_active_seconds is None
            ):
                raise SdkError("至少提供一个配置字段")
            result: JsonObject = {}
            if mode is not None:
                result["mode"] = await self._service.set_mode(mode.strip())
            if character_strategy is not None:
                result["character_strategy"] = await self._service.set_character_strategy(character_strategy.strip())
            if (
                action_interval_seconds is not None
                or post_action_delay_seconds is not None
                or poll_interval_active_seconds is not None
            ):
                speed_payload = await self._service.set_speed(
                    action_interval_seconds=_optional_finite_float(action_interval_seconds, key="action_interval_seconds"),
                    post_action_delay_seconds=_optional_finite_float(post_action_delay_seconds, key="post_action_delay_seconds"),
                    poll_interval_active_seconds=_optional_finite_float(poll_interval_active_seconds, key="poll_interval_active_seconds"),
                )
                try:
                    self._save_speed_overrides(
                        action_interval_seconds=speed_payload.get("action_interval_seconds"),
                        post_action_delay_seconds=speed_payload.get("post_action_delay_seconds"),
                        poll_interval_active_seconds=speed_payload.get("poll_interval_active_seconds"),
                    )
                except Exception as exc:
                    self.logger.warning(f"STS2 speed override persistence failed: {exc}")
                    speed_payload = {
                        **speed_payload,
                        "local_save_failed": True,
                        "warning": f"运行时速度已生效，但写回 plugin.toml 失败: {exc}",
                    }
                result["speed"] = speed_payload
            status = await self._service.get_status()
            summary = "尖塔配置已更新"
            return {**result, "status": "ok", "message": summary, "summary": summary, "current": status}

        return await self._run_entry(action, finish=True)
