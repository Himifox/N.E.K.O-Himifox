from __future__ import annotations

import asyncio

import pytest

from plugin.plugins.selfie_painter_neko import SelfiePainterPlugin
from plugin.plugins.selfie_painter_neko.service import GeneratedSelfie


class _Context:
    plugin_id = "selfie_painter_neko"

    def __init__(self) -> None:
        self.updated: list[dict[str, object]] = []

    async def update_own_config(self, values: dict[str, object]) -> dict[str, object]:
        self.updated.append(values)
        return values


class _I18n:
    @staticmethod
    def t(_key: str, *, default: str, **_kwargs: object) -> str:
        return default


class _Service:
    async def generate(self, *, scene: str, style: str) -> GeneratedSelfie:
        assert scene == "海边"
        assert style == "photo"
        return GeneratedSelfie(
            filename="selfie.jpg",
            public_url="http://127.0.0.1/full.jpg",
            preview_url="http://127.0.0.1/preview.jpg",
            style=style,
        )


@pytest.mark.asyncio
async def test_save_webui_config_updates_base_config_without_profile() -> None:
    plugin = SelfiePainterPlugin.__new__(SelfiePainterPlugin)
    plugin.ctx = _Context()
    plugin.i18n = _I18n()
    reloaded = False

    async def reload_service() -> None:
        nonlocal reloaded
        reloaded = True

    plugin._reload_service = reload_service

    result = await plugin.save_webui_config(reference_source="active_character")

    expected = {"selfie_painter": {"reference_source": "active_character"}}
    assert plugin.ctx.updated == [expected]
    assert reloaded is True
    assert result.is_ok()


@pytest.mark.asyncio
async def test_generate_publishes_native_image_part() -> None:
    plugin = SelfiePainterPlugin.__new__(SelfiePainterPlugin)
    plugin._service = _Service()
    plugin._generation_lock = asyncio.Lock()
    plugin.i18n = _I18n()
    plugin.ctx = _Context()
    pushed: list[dict[str, object]] = []
    plugin.push_message = lambda **kwargs: pushed.append(kwargs)

    await plugin._generate_and_publish(scene="海边", style="photo")

    assert pushed == [
        {
            "visibility": ["chat"],
            "ai_behavior": "blind",
            "parts": [
                {"type": "text", "text": "拍好啦！"},
                {
                    "type": "image",
                    "url": "http://127.0.0.1/preview.jpg",
                    "mime": "image/jpeg",
                    "alt": "NEKO 自拍",
                },
            ],
            "source": "selfie_painter_neko",
            "metadata": {
                "kind": "selfie",
                "style": "photo",
                "filename": "selfie.jpg",
                "delivery_semantics": "passive",
            },
        }
    ]
