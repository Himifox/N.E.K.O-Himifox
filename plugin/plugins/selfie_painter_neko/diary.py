from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


_STORE_KEY = "selfie_diary_v1"
_DEFAULT_CHARACTER = "__default__"
_MAX_CONTEXT_ITEMS = 3
_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "www.",
    "api_key",
    "api key",
    "apikey",
    "token",
    "password",
    "密码",
    "密钥",
    "令牌",
)
_VISUAL_TERMS = (
    "穿",
    "衣",
    "裙",
    "裤",
    "鞋",
    "帽",
    "发型",
    "头发",
    "妆",
    "表情",
    "微笑",
    "害羞",
    "开心",
    "难过",
    "困",
    "海边",
    "沙滩",
    "卧室",
    "房间",
    "咖啡",
    "公园",
    "学校",
    "教室",
    "街",
    "窗边",
    "雨",
    "雪",
    "夕阳",
    "黄昏",
    "夜晚",
    "早晨",
    "灯光",
    "坐",
    "站着",
    "站在",
    "躺",
    "回头",
    "比心",
    "抱",
    "拿着",
    "wear",
    "dress",
    "outfit",
    "smile",
    "beach",
    "room",
    "cafe",
    "park",
    "sunset",
    "night",
    "rain",
    "snow",
    "pose",
)


def _clean_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(value or ""))
    text = re.sub(r"[`<>\[\]{}]+", " ", text)
    return " ".join(text.split())[:limit]


def _character_key(character_name: str) -> str:
    return _clean_text(character_name, limit=100) or _DEFAULT_CHARACTER


def _record_payload(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    payload = getattr(record, "payload", None)
    if isinstance(payload, Mapping):
        return dict(payload)
    dump = getattr(record, "dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def select_recent_visual_context(
    records: Iterable[Any],
    *,
    character_name: str,
    current_scene: str,
    limit: int = _MAX_CONTEXT_ITEMS,
) -> str:
    """Return a bounded, non-persistent visual hint from recent user utterances."""
    scene = _clean_text(current_scene, limit=1200)
    selected: list[str] = []
    seen: set[str] = set()
    for record in reversed(list(records)):
        payload = _record_payload(record)
        if str(payload.get("type") or "").lower() != "user_message":
            continue
        event_character = _clean_text(payload.get("lanlan"), limit=100)
        if character_name and event_character and event_character != character_name:
            continue
        raw_content = str(payload.get("content") or "")
        lowered_raw = raw_content.casefold()
        if "```" in raw_content or any(marker in lowered_raw for marker in _SENSITIVE_MARKERS):
            continue
        content = _clean_text(raw_content, limit=240)
        if not content or content == scene or content in seen:
            continue
        lowered = content.casefold()
        if not any(term.casefold() in lowered for term in _VISUAL_TERMS):
            continue
        seen.add(content)
        selected.append(content)
        if len(selected) >= max(0, limit):
            break
    if not selected:
        return ""
    selected.reverse()
    return (
        "Recent visual hints from conversation; treat them as optional context and never "
        "override the current request: " + " | ".join(selected)
    )


def continuity_hint(events: Iterable[Mapping[str, Any]], *, current_scene: str) -> str:
    """Use the last successful scene only when the current request omitted a scene."""
    if _clean_text(current_scene, limit=1200):
        return ""
    for event in events:
        scene = _clean_text(event.get("scene"), limit=240)
        if scene:
            return f"Optional continuity from the previous selfie: {scene}"
    return ""


class SelfieDiary:
    def __init__(self, *, store: Any, logger: Any, max_events: int = 100) -> None:
        self.store = store
        self.logger = logger
        self.max_events = max(1, min(500, int(max_events)))

    async def _read(self) -> dict[str, Any]:
        result = await self.store.get(_STORE_KEY, {})
        value = result.value if hasattr(result, "value") else result
        if not isinstance(value, Mapping):
            return {"version": 1, "characters": {}}
        document = dict(value)
        characters = document.get("characters")
        if not isinstance(characters, Mapping):
            characters = {}
        return {"version": 1, "characters": dict(characters)}

    async def _write(self, document: Mapping[str, Any]) -> None:
        result = await self.store.set(_STORE_KEY, dict(document))
        error = getattr(result, "error", None)
        if error is not None:
            raise RuntimeError(str(error))

    async def list_events(self, character_name: str, *, limit: int = 30) -> list[dict[str, Any]]:
        document = await self._read()
        raw_events = document["characters"].get(_character_key(character_name), [])
        if not isinstance(raw_events, list):
            return []
        events = [dict(item) for item in raw_events if isinstance(item, Mapping)]
        return events[: max(0, limit)]

    async def append_success(
        self,
        *,
        character_name: str,
        scene: str,
        style: str,
        filename: str,
    ) -> dict[str, Any]:
        clean_scene = _clean_text(scene, limit=1200)
        event = {
            "id": uuid.uuid4().hex,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "title": _clean_text(clean_scene, limit=60) or "一张新的自拍",
            "diary": clean_scene or f"留下了一张 {style} 风格的自拍。",
            "mood": "",
            "level": "notable" if clean_scene else "mundane",
            "scene": clean_scene,
            "style": _clean_text(style, limit=20),
            "filename": _clean_text(filename, limit=160),
            "source": "explicit_request" if clean_scene else "plugin_default",
        }
        document = await self._read()
        characters = document["characters"]
        key = _character_key(character_name)
        existing = characters.get(key, [])
        events = [dict(item) for item in existing if isinstance(item, Mapping)] if isinstance(existing, list) else []
        characters[key] = [event, *events][: self.max_events]
        await self._write(document)
        return event

    async def clear_character(self, character_name: str) -> int:
        document = await self._read()
        characters = document["characters"]
        key = _character_key(character_name)
        existing = characters.pop(key, [])
        await self._write(document)
        return len(existing) if isinstance(existing, list) else 0


__all__ = ["SelfieDiary", "continuity_hint", "select_recent_visual_context"]
