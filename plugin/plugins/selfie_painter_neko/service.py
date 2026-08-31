from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError


_STYLE_PROMPTS = {
    "standard": "front-facing handheld selfie, looking at viewer, natural arm-length framing",
    "mirror": "mirror selfie, reflected composition, natural pose, phone visible in reflection",
    "photo": "candid third-person photo, natural perspective, subject looking toward camera",
}
_VALID_REFERENCE_SOURCES = {"none", "active_character", "file"}
_MAX_ERROR_TEXT = 300


class SelfiePainterError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelfiePainterConfig:
    api_format: str
    base_url: str
    api_key: str
    model: str
    size: str
    timeout_seconds: float
    character_prompt: str
    prompt_suffix: str
    negative_prompt: str
    default_style: str
    reference_source: str
    reference_image_path: str
    reference_strength: float
    public_base_url: str
    jpeg_quality: int
    max_saved_images: int
    max_image_bytes: int
    guidance_scale: float
    num_inference_steps: int
    seed: int
    context_enabled: bool = True
    diary_enabled: bool = True
    diary_max_events: int = 100

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SelfiePainterConfig:
        api_format = str(raw.get("api_format") or "openai").strip().lower()
        if api_format not in {"openai", "modelscope", "dashscope"}:
            raise SelfiePainterError("api_format 仅支持 openai、modelscope 或 dashscope。")

        default_style = str(raw.get("default_style") or "standard").strip().lower()
        if default_style not in _STYLE_PROMPTS:
            default_style = "standard"

        reference_source = str(raw.get("reference_source") or "none").strip().lower()
        if reference_source not in _VALID_REFERENCE_SOURCES:
            reference_source = "none"

        return cls(
            api_format=api_format,
            base_url=str(raw.get("base_url") or "").strip(),
            api_key=str(raw.get("api_key") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            size=str(raw.get("size") or "1024x1024").strip(),
            timeout_seconds=_bounded_float(raw.get("timeout_seconds"), 180.0, 10.0, 300.0),
            character_prompt=str(raw.get("character_prompt") or "").strip(),
            prompt_suffix=str(raw.get("prompt_suffix") or "").strip(),
            negative_prompt=str(raw.get("negative_prompt") or "").strip(),
            default_style=default_style,
            reference_source=reference_source,
            reference_image_path=str(raw.get("reference_image_path") or "").strip(),
            reference_strength=_bounded_float(raw.get("reference_strength"), 0.65, 0.0, 1.0),
            public_base_url=str(raw.get("public_base_url") or "").strip().rstrip("/"),
            jpeg_quality=_bounded_int(raw.get("jpeg_quality"), 92, 60, 100),
            max_saved_images=_bounded_int(raw.get("max_saved_images"), 30, 1, 500),
            max_image_bytes=_bounded_int(raw.get("max_image_bytes"), 20 * 1024 * 1024, 1024, 100 * 1024 * 1024),
            guidance_scale=_bounded_float(raw.get("guidance_scale"), 5.0, 0.0, 30.0),
            num_inference_steps=_bounded_int(raw.get("num_inference_steps"), 28, 1, 150),
            seed=_bounded_int(raw.get("seed"), -1, -1, 2_147_483_647),
            context_enabled=_as_bool(raw.get("context_enabled"), True),
            diary_enabled=_as_bool(raw.get("diary_enabled"), True),
            diary_max_events=_bounded_int(raw.get("diary_max_events"), 100, 1, 500),
        )


@dataclass(frozen=True)
class GeneratedSelfie:
    filename: str
    public_url: str
    preview_url: str
    style: str
    character_name: str = ""


class SelfiePainterService:
    def __init__(
        self,
        *,
        plugin_id: str,
        config_dir: Path,
        settings: SelfiePainterConfig,
        logger: Any,
    ) -> None:
        self.plugin_id = plugin_id
        self.config_dir = config_dir
        self.settings = settings
        self.logger = logger
        self.generated_dir = config_dir / "static" / "generated"

    def prepare_static_directory(self) -> None:
        self.generated_dir.mkdir(parents=True, exist_ok=True)

    def has_api_key(self) -> bool:
        return bool(self._api_key())

    def recent_images(self, *, limit: int = 6) -> list[dict[str, Any]]:
        images = sorted(
            (
                item
                for item in self.generated_dir.glob("selfie_*.jpg")
                if not item.stem.endswith("_preview")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        recent: list[dict[str, Any]] = []
        for image in images[: max(0, limit)]:
            preview = image.with_name(f"{image.stem}_preview.jpg")
            if not preview.is_file():
                continue
            recent.append(
                {
                    "filename": image.name,
                    "public_url": self._public_url(image.name),
                    "preview_url": self._public_url(preview.name),
                    "created_at": datetime.fromtimestamp(image.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
        return recent

    def has_generated_image(self, filename: str) -> bool:
        return bool(filename) and Path(filename).name == filename and (self.generated_dir / filename).is_file()

    def public_image_url(self, filename: str) -> str:
        return self._public_url(filename) if self.has_generated_image(filename) else ""

    async def generate(self, *, scene: str, style: str) -> GeneratedSelfie:
        return await self.generate_with_context(scene=scene, style=style)

    async def active_character(self) -> tuple[str, str]:
        return await self._active_character()

    async def generate_with_context(
        self,
        *,
        scene: str,
        style: str,
        active_character: tuple[str, str] | None = None,
        recent_context: str = "",
        continuity: str = "",
    ) -> GeneratedSelfie:
        self._validate_required_config()
        active_name, active_reference = active_character or await self._active_character()
        selected_style = style.strip().lower() if isinstance(style, str) else ""
        if selected_style not in _STYLE_PROMPTS:
            selected_style = self.settings.default_style

        prompt = self._build_prompt(
            scene=_clean_prompt_part(scene),
            style=selected_style,
            active_name=active_name,
            recent_context=_clean_prompt_part(recent_context, limit=900),
            continuity=_clean_prompt_part(continuity, limit=320),
        )
        reference = await self._reference_image(active_reference)
        api_key = self._api_key()

        timeout = httpx.Timeout(self.settings.timeout_seconds, connect=min(15.0, self.settings.timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client:
            if self.settings.api_format == "modelscope":
                image_bytes = await self._generate_modelscope(client, api_key, prompt, reference)
            elif self.settings.api_format == "dashscope":
                image_bytes = await self._generate_dashscope(client, api_key, prompt, reference)
            else:
                image_bytes = await self._generate_openai(client, api_key, prompt, reference)

        filename, preview_filename = await asyncio.to_thread(self._save_as_jpeg, image_bytes)
        await asyncio.to_thread(self._cleanup_old_images)
        return GeneratedSelfie(
            filename=filename,
            public_url=self._public_url(filename),
            preview_url=self._public_url(preview_filename),
            style=selected_style,
            character_name=active_name,
        )

    def _validate_required_config(self) -> None:
        missing = [
            name
            for name, value in (("base_url", self.settings.base_url), ("model", self.settings.model))
            if not value
        ]
        if not self._api_key():
            missing.append("api_key / NEKO_SELFIE_PAINTER_API_KEY")
        if missing:
            raise SelfiePainterError(f"自拍插件缺少配置：{', '.join(missing)}。")

    def _api_key(self) -> str:
        neko_key = os.getenv("NEKO_SELFIE_PAINTER_API_KEY", "").strip()
        if neko_key:
            return neko_key
        if self.settings.api_format == "dashscope":
            dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
            if dashscope_key:
                return dashscope_key
        return self.settings.api_key

    def _authorization(self, api_key: str) -> str:
        return api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"

    def _endpoint(self, suffix: str) -> str:
        base_url = self.settings.base_url.rstrip("/")
        if base_url.endswith(suffix):
            return base_url
        return f"{base_url}/{suffix.lstrip('/')}"

    def _build_prompt(
        self,
        *,
        scene: str,
        style: str,
        active_name: str,
        recent_context: str = "",
        continuity: str = "",
    ) -> str:
        parts = [self.settings.character_prompt]
        if active_name:
            parts.append(f"character name: {active_name}")
        parts.append(_STYLE_PROMPTS[style])
        if scene:
            parts.append(f"Current request (highest priority): {scene}")
        if recent_context:
            parts.append(recent_context)
        if continuity:
            parts.append(continuity)
        parts.append(self.settings.prompt_suffix)
        return ", ".join(part for part in parts if part)

    async def _active_character(self) -> tuple[str, str]:
        main_port = os.getenv("NEKO_MAIN_SERVER_PORT", "48911").strip() or "48911"
        url = f"http://127.0.0.1:{main_port}/api/card-drop/active-character?include_avatar=true"
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return "", ""
        if not isinstance(payload, Mapping):
            return "", ""
        name = str(payload.get("name") or "").strip()
        reference = str(
            payload.get("characterReferenceDataUrl") or payload.get("dataUrl") or ""
        ).strip()
        return name, reference

    async def _reference_image(self, active_reference: str) -> str:
        source = self.settings.reference_source
        if source == "none":
            return ""
        if source == "active_character":
            if active_reference.startswith("data:image/"):
                return active_reference
            raise SelfiePainterError("当前 NEKO 角色没有可用参考图，请改用 file 或 none。")

        path = Path(self.settings.reference_image_path)
        if not path.is_absolute():
            path = self.config_dir / path
        if not path.is_file():
            raise SelfiePainterError(f"参考图不存在：{path}")
        raw = await asyncio.to_thread(path.read_bytes)
        if len(raw) > self.settings.max_image_bytes:
            raise SelfiePainterError("参考图超过允许大小。")
        mime = _detect_mime(raw)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    async def _generate_openai(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        prompt: str,
        reference: str,
    ) -> bytes:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "prompt": prompt,
            "size": self.settings.size,
            "n": 1,
        }
        is_openai_official = urlparse(self.settings.base_url).hostname == "api.openai.com"
        base_lower = self.settings.base_url.lower()
        if "siliconflow" in base_lower:
            payload["image_size"] = payload.pop("size")
            payload["batch_size"] = payload.pop("n")
            payload["guidance_scale"] = self.settings.guidance_scale
            payload["num_inference_steps"] = self.settings.num_inference_steps
        if self.settings.negative_prompt and not is_openai_official:
            payload["negative_prompt"] = self.settings.negative_prompt
        if self.settings.seed >= 0 and not is_openai_official:
            payload["seed"] = self.settings.seed
        if reference:
            if is_openai_official:
                raise SelfiePainterError(
                    "官方 OpenAI 的参考图需要 images/edits 接口；当前请将 reference_source 设为 none。"
                )
            payload["image"] = reference
            payload["strength"] = self.settings.reference_strength

        response = await client.post(
            self._endpoint("images/generations"),
            headers={
                "Authorization": self._authorization(api_key),
                "Content-Type": "application/json",
            },
            json=payload,
        )
        await self._raise_for_status(response)
        return await self._image_from_payload(client, response.json())

    async def _generate_modelscope(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        prompt: str,
        reference: str,
    ) -> bytes:
        payload: dict[str, Any] = {"model": self.settings.model, "prompt": prompt}
        if reference:
            payload["image_url"] = [reference]
        else:
            payload.update(
                {
                    "size": self.settings.size,
                    "steps": self.settings.num_inference_steps,
                    "guidance": self.settings.guidance_scale,
                }
            )
            if self.settings.negative_prompt:
                payload["negative_prompt"] = self.settings.negative_prompt
            if self.settings.seed >= 0:
                payload["seed"] = self.settings.seed

        authorization = self._authorization(api_key)
        response = await client.post(
            self._endpoint("images/generations"),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "X-ModelScope-Async-Mode": "true",
            },
            json=payload,
        )
        await self._raise_for_status(response)
        task_payload = response.json()
        task_id = task_payload.get("task_id") if isinstance(task_payload, Mapping) else None
        if not isinstance(task_id, str) or not task_id:
            return await self._image_from_payload(client, task_payload)

        status_url = self._endpoint(f"tasks/{task_id}")
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(3.0)
            status_response = await client.get(
                status_url,
                headers={
                    "Authorization": authorization,
                    "X-ModelScope-Task-Type": "image_generation",
                },
            )
            await self._raise_for_status(status_response)
            status_payload = status_response.json()
            status = str(status_payload.get("task_status") or "").upper()
            if status == "SUCCEED":
                return await self._image_from_payload(client, status_payload)
            if status == "FAILED":
                message = str(status_payload.get("error_message") or "魔搭图片生成任务失败。")
                raise SelfiePainterError(message[:_MAX_ERROR_TEXT])
        raise SelfiePainterError("魔搭图片生成任务超时。")

    async def _generate_dashscope(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        prompt: str,
        reference: str,
    ) -> bytes:
        content: list[dict[str, str]] = []
        if reference:
            content.append({"image": reference})
        content.append({"text": prompt})

        parameters: dict[str, Any] = {
            "n": 1,
            "prompt_extend": True,
            "watermark": False,
            "size": self.settings.size.replace("x", "*"),
        }
        if self.settings.negative_prompt:
            parameters["negative_prompt"] = self.settings.negative_prompt[:500]
        if self.settings.seed >= 0:
            parameters["seed"] = self.settings.seed

        response = await client.post(
            self._endpoint("services/aigc/multimodal-generation/generation"),
            headers={
                "Authorization": self._authorization(api_key),
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.model,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": parameters,
            },
        )
        await self._raise_for_status(response)
        payload = response.json()
        if isinstance(payload, Mapping) and payload.get("code"):
            message = str(payload.get("message") or payload.get("code"))
            raise SelfiePainterError(f"百炼图片生成失败：{message[:_MAX_ERROR_TEXT]}")
        return await self._image_from_payload(client, payload)

    async def _image_from_payload(self, client: httpx.AsyncClient, payload: Any) -> bytes:
        value = _first_image_value(payload)
        if not value:
            raise SelfiePainterError("生图接口返回成功，但没有找到图片数据。")
        if value.startswith("data:image/"):
            return _decode_base64(value.split(",", 1)[1], self.settings.max_image_bytes)
        if value.startswith(("http://", "https://")):
            return await self._download_image(client, value)
        return _decode_base64(value, self.settings.max_image_bytes)

    async def _download_image(self, client: httpx.AsyncClient, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SelfiePainterError("生图接口返回了无效图片地址。")
        response = await client.get(url)
        await self._raise_for_status(response)
        content = response.content
        if len(content) > self.settings.max_image_bytes:
            raise SelfiePainterError("生成图片超过允许大小。")
        return content

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        text = response.text.strip().replace("\n", " ")[:_MAX_ERROR_TEXT]
        raise SelfiePainterError(f"生图接口请求失败（HTTP {response.status_code}）：{text}")

    def _save_as_jpeg(self, image_bytes: bytes) -> tuple[str, str]:
        if not image_bytes or len(image_bytes) > self.settings.max_image_bytes:
            raise SelfiePainterError("生成图片为空或超过允许大小。")
        image_id = uuid.uuid4().hex
        filename = f"selfie_{image_id}.jpg"
        preview_filename = f"selfie_{image_id}_preview.jpg"
        target = self.generated_dir / filename
        preview_target = self.generated_dir / preview_filename
        temporary = target.with_suffix(".tmp")
        preview_temporary = preview_target.with_suffix(".tmp")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                normalized.save(
                    temporary,
                    format="JPEG",
                    quality=self.settings.jpeg_quality,
                    optimize=True,
                )
                normalized.thumbnail((280, 420), Image.Resampling.LANCZOS)
                normalized.save(
                    preview_temporary,
                    format="JPEG",
                    quality=min(self.settings.jpeg_quality, 88),
                    optimize=True,
                )
            temporary.replace(target)
            preview_temporary.replace(preview_target)
        except (UnidentifiedImageError, OSError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            preview_temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise SelfiePainterError("生图接口返回的内容不是有效图片。") from error
        return filename, preview_filename

    def _cleanup_old_images(self) -> None:
        images = sorted(
            (
                item
                for item in self.generated_dir.glob("selfie_*.jpg")
                if not item.stem.endswith("_preview")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in images[self.settings.max_saved_images :]:
            try:
                stale.unlink()
                stale.with_name(f"{stale.stem}_preview.jpg").unlink(missing_ok=True)
            except OSError as error:
                self.logger.warning("Failed to remove old selfie %s: %s", stale, error)

    def _public_url(self, filename: str) -> str:
        base_url = (
            os.getenv("NEKO_SELFIE_PAINTER_PUBLIC_BASE_URL", "").strip().rstrip("/")
            or self.settings.public_base_url
        )
        if not base_url:
            port = os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "48916").strip() or "48916"
            base_url = f"http://127.0.0.1:{port}"
        return f"{base_url}/plugin/{self.plugin_id}/ui/generated/{filename}"


def _first_image_value(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, Mapping):
            for key in ("b64_json", "url"):
                value = first.get(key)
                if isinstance(value, str) and value:
                    return value
    for key in ("images", "output_images"):
        images = payload.get(key)
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str):
                return first
            if isinstance(first, Mapping):
                value = first.get("url") or first.get("b64_json")
                if isinstance(value, str):
                    return value
    value = payload.get("url") or payload.get("b64_json")
    if isinstance(value, str):
        return value

    output = payload.get("output")
    if isinstance(output, Mapping):
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, Mapping):
                message = first_choice.get("message")
                if isinstance(message, Mapping):
                    content = message.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, Mapping):
                                image = item.get("image")
                                if isinstance(image, str) and image:
                                    return image
        results = output.get("results")
        if isinstance(results, list) and results:
            first_result = results[0]
            if isinstance(first_result, Mapping):
                result_url = first_result.get("url")
                if isinstance(result_url, str):
                    return result_url
    return ""


def _decode_base64(value: str, max_bytes: int) -> bytes:
    compact = "".join(value.split())
    if len(compact) > ((max_bytes + 2) // 3) * 4 + 8:
        raise SelfiePainterError("生成图片超过允许大小。")
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SelfiePainterError("生图接口返回了无效的 Base64 图片。") from error
    if len(decoded) > max_bytes:
        raise SelfiePainterError("生成图片超过允许大小。")
    return decoded


def _detect_mime(value: bytes) -> str:
    if value.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if value.startswith(b"RIFF") and value[8:12] == b"WEBP":
        return "image/webp"
    if value.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/jpeg"


def _clean_prompt_part(value: Any, limit: int = 1200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


__all__ = [
    "GeneratedSelfie",
    "SelfiePainterConfig",
    "SelfiePainterError",
    "SelfiePainterService",
]
