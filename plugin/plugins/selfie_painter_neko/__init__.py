from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    tr,
    ui,
)

from .service import SelfiePainterConfig, SelfiePainterError, SelfiePainterService


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@neko_plugin
class SelfiePainterPlugin(NekoPluginBase):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.logger = self.enable_file_logging(log_level="INFO")
        self._service: SelfiePainterService | None = None
        self._generation_lock = asyncio.Lock()

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        await self._reload_service()
        service = self._service
        if service is None:
            raise RuntimeError("selfie painter service could not be initialized")
        service.prepare_static_directory()
        if not self.register_static_ui("static", cache_control="no-store"):
            raise RuntimeError("selfie painter static directory is unavailable")
        return Ok({"status": "ready", "api_format": service.settings.api_format})

    async def _reload_service(self) -> None:
        config = _as_mapping(await self.config.dump(timeout=5.0))
        settings = SelfiePainterConfig.from_mapping(_as_mapping(config.get("selfie_painter")))
        self._service = SelfiePainterService(
            plugin_id=self.plugin_id,
            config_dir=self.config_dir,
            settings=settings,
            logger=self.logger,
        )
        self._service.prepare_static_directory()

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        self._service = None
        return Ok({"status": "shutdown"})

    @llm_tool(
        name="neko_take_selfie",
        description=tr(
            "tools.take_selfie.description",
            default=(
                "当用户要求 NEKO 自拍、拍照或生成一张自己的照片时调用。"
                "scene 描述地点、衣着、动作、表情或光线；style 可为 standard、mirror 或 photo。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {
                "scene": {
                    "type": "string",
                    "description": "用户希望出现的场景、服装、动作、表情或光线，可留空。",
                },
                "style": {
                    "type": "string",
                    "enum": ["standard", "mirror", "photo"],
                    "description": "standard=前置自拍，mirror=对镜自拍，photo=第三人称照片。",
                },
            },
        },
        timeout=300.0,
    )
    async def take_selfie(
        self,
        *,
        scene: str = "",
        style: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        try:
            result = await self._generate_and_publish(scene=scene, style=style)
            return {
                "ok": True,
                "message": self.i18n.t(
                    "messages.sent",
                    default="自拍已经直接显示在聊天中，不要重复输出图片地址。",
                ),
                "style": result.style,
            }
        except SelfiePainterError as error:
            self.logger.warning("Selfie generation failed: %s", error)
            return self._tool_error(str(error))
        except Exception as error:
            self.logger.exception("Unexpected selfie generation failure")
            return self._tool_error(
                self.i18n.t("errors.internal", default="生成自拍时出现内部错误：{error}", error=error)
            )

    async def _generate_and_publish(self, *, scene: str, style: str):
        service = self._service
        if service is None:
            raise SelfiePainterError(self.i18n.t("errors.not_ready", default="自拍插件尚未启动。"))
        async with self._generation_lock:
            result = await service.generate(scene=scene, style=style)
        caption = self.i18n.t("messages.ready", default="拍好啦！")
        self.push_message(
            visibility=["chat"],
            ai_behavior="blind",
            parts=[
                {"type": "text", "text": caption},
                {
                    "type": "image",
                    "url": result.preview_url,
                    "mime": "image/jpeg",
                    "alt": "NEKO 自拍",
                },
            ],
            source=self.plugin_id,
            metadata={
                "kind": "selfie",
                "style": result.style,
                "filename": result.filename,
                "delivery_semantics": "passive",
            },
        )
        return result

    @ui.context(id="dashboard", title=tr("panel.title", default="NEKO Selfie Painter"))
    async def get_dashboard_context(self) -> dict[str, Any]:
        service = self._service
        if service is None:
            return {"ready": False, "configured": False, "recent_images": []}
        settings = service.settings
        return {
            "ready": True,
            "configured": bool(settings.base_url and settings.model and service.has_api_key()),
            "api_key_configured": service.has_api_key(),
            "config": {
                "api_format": settings.api_format,
                "base_url": settings.base_url,
                "model": settings.model,
                "size": settings.size,
                "character_prompt": settings.character_prompt,
                "prompt_suffix": settings.prompt_suffix,
                "negative_prompt": settings.negative_prompt,
                "default_style": settings.default_style,
                "reference_source": settings.reference_source,
                "reference_image_path": settings.reference_image_path,
                "public_base_url": settings.public_base_url,
            },
            "recent_images": service.recent_images(limit=6),
        }

    @ui.action(
        label=tr("actions.generate.label", default="Generate selfie"),
        tone="primary",
        group="generation",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="selfie_generate_webui",
        name=tr("entries.generate.name", default="Generate selfie from WebUI"),
        description=tr("entries.generate.description", default="Generate a NEKO selfie from the plugin panel."),
        input_schema={
            "type": "object",
            "properties": {
                "scene": {"type": "string"},
                "style": {"type": "string", "enum": ["standard", "mirror", "photo"]},
            },
        },
    )
    async def generate_from_webui(self, scene: str = "", style: str = "", **_: Any):
        try:
            result = await self._generate_and_publish(scene=scene, style=style)
            return Ok(
                {
                    "message": self.i18n.t("messages.ready", default="拍好啦！"),
                    "public_url": result.public_url,
                    "preview_url": result.preview_url,
                    "style": result.style,
                }
            )
        except SelfiePainterError as error:
            return Err(SdkError(str(error)))
        except Exception as error:
            self.logger.exception("Unexpected WebUI selfie generation failure")
            return Err(SdkError(str(error)))

    @ui.action(
        label=tr("actions.saveConfig.label", default="Save config"),
        tone="success",
        group="config",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="selfie_save_config",
        name=tr("entries.saveConfig.name", default="Save selfie config"),
        description=tr("entries.saveConfig.description", default="Save settings from the selfie WebUI."),
        input_schema={
            "type": "object",
            "properties": {
                "api_format": {
                    "type": "string",
                    "enum": ["openai", "modelscope", "dashscope"],
                },
                "base_url": {"type": "string"},
                "api_key": {"type": "string"},
                "model": {"type": "string"},
                "size": {"type": "string"},
                "character_prompt": {"type": "string"},
                "prompt_suffix": {"type": "string"},
                "negative_prompt": {"type": "string"},
                "default_style": {"type": "string", "enum": ["standard", "mirror", "photo"]},
                "reference_source": {"type": "string", "enum": ["none", "active_character", "file"]},
                "reference_image_path": {"type": "string"},
                "public_base_url": {"type": "string"},
            },
        },
    )
    async def save_webui_config(self, **kwargs: Any):
        allowed = {
            "api_format",
            "base_url",
            "model",
            "size",
            "character_prompt",
            "prompt_suffix",
            "negative_prompt",
            "default_style",
            "reference_source",
            "reference_image_path",
            "public_base_url",
        }
        updates = {key: str(kwargs.get(key) or "").strip() for key in allowed if key in kwargs}
        api_key = str(kwargs.get("api_key") or "").strip()
        if api_key:
            updates["api_key"] = api_key
        try:
            SelfiePainterConfig.from_mapping(updates)
            await self.ctx.update_own_config({"selfie_painter": updates})
            await self._reload_service()
            return Ok({"message": self.i18n.t("messages.configSaved", default="配置已保存。")})
        except (SelfiePainterError, TypeError, ValueError) as error:
            return Err(SdkError(str(error)))
        except Exception as error:
            self.logger.exception("Failed to save selfie WebUI config")
            return Err(SdkError(str(error)))

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            "output": {"ok": False, "message": message},
            "is_error": True,
            "error": message,
        }


__all__ = ["SelfiePainterPlugin"]
