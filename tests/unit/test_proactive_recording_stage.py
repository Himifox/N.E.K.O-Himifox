"""Focused tests for post-commit proactive-chat recording."""

from __future__ import annotations

from typing import Any

import pytest

from main_logic.proactive_chat import contracts
from main_logic.proactive_chat import delivery as service


class _ActivityTracker:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self._events = events

    def mark_unfinished_thread_used(self) -> None:
        self._events.append(("unfinished",))


class _Manager:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.current_speech_id = "committed-turn"
        self._activity_tracker = _ActivityTracker(events)


class _MemoryClient:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self._events = events

    async def post(self, url: str, **kwargs: Any) -> None:
        self._events.append(("memory surfaced", url, kwargs))


def _web_link() -> dict[str, str]:
    return {
        "title": "一场夏夜流星雨",
        "url": "https://example.test/meteor",
        "source": "示例新闻",
    }


def _music_link() -> dict[str, str]:
    return {
        "title": "夜曲",
        "artist": "周杰伦",
        "url": "https://example.test/nocturne",
        "source": "音乐推荐",
        "type": "music",
    }


def _meme_link() -> dict[str, str]:
    return {
        "title": "猫猫震惊",
        "url": "https://example.test/cat.png",
        "source": "表情包",
    }


def _install_recorders(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[Any, ...]],
) -> None:
    monkeypatch.setattr(
        service,
        "_record_proactive_chat",
        lambda *args: events.append(("chat", *args)),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_proactive_material_key",
        lambda *args: "night-track-key",
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_record_proactive_material",
        lambda *args: events.append(("material", *args)),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_mini_game_invite_count_post_response_chat",
        lambda *args: events.append(("mini counter", *args)),
        raising=False,
    )

    async def increment_total(*args: Any) -> int:
        events.append(("total", *args))
        return 1

    monkeypatch.setattr(
        service,
        "_increment_proactive_chat_total",
        increment_total,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_record_reminiscence_usage",
        lambda *args: events.append(("reminiscence", *args)),
        raising=False,
    )

    async def record_source_used(*, url: str, kind: str, title: str = "") -> None:
        events.append((f"{kind} source", url, title))

    monkeypatch.setattr(
        service,
        "_record_source_used",
        record_source_used,
        raising=False,
    )
    memory_client = _MemoryClient(events)
    monkeypatch.setattr(
        service,
        "_get_internal_http_client",
        lambda: memory_client,
        raising=False,
    )


async def _record(
    delivery: service.CommittedDelivery,
    mgr: _Manager,
    **overrides: Any,
) -> contracts.ProactiveChatResult:
    values: dict[str, Any] = {
        "delivery": delivery,
        "mgr": mgr,
        "lanlan_name": "兰兰",
        "response_text": "今晚有流星雨，也给你配一首《夜曲》。",
        "source_tag": "CHAT",
        "active_channels": ["web", "music", "meme"],
        "has_unfinished_thread": True,
        "surfaced_reflection_ids": ["reflection-1", "reflection-2"],
        "selected_web_link": _web_link(),
        "selected_web_topic_key": "meteor-topic",
        "web_parsed": {"title": "一场夏夜流星雨"},
        "selected_music_link": _music_link(),
        "selected_music_topic_key": "night-music-topic",
        "selected_meme_link": _meme_link(),
        "selected_meme_topic_key": "cat-meme-topic",
        "meme_content": {"keyword": "猫猫震惊"},
        "memory_server_port": 4321,
    }
    values.update(overrides)
    return await service._record_committed_delivery(**values)


@pytest.mark.asyncio
async def test_committed_delivery_records_every_side_effect_in_legacy_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_recorders(monkeypatch, events)
    mgr = _Manager(events)
    web = _web_link()
    music = _music_link()
    meme = _meme_link()
    delivery = service.CommittedDelivery(
        primary_channel="chat",
        source_links=[web, music, meme],
        delivered_tag="MUSIC",
        delivered_music_link=music,
        is_music_used=True,
        action_note="",
        vision_screenshot_b64=None,
    )

    # Merely constructing the commit fact has no persistence side effect.  The
    # caller must explicitly hand a successful CommittedDelivery to this stage.
    assert events == []
    result = await _record(
        delivery,
        mgr,
        selected_web_link=web,
        selected_music_link=music,
        selected_meme_link=meme,
    )

    assert [event[0] for event in events] == [
        "chat",
        "material",
        "mini counter",
        "total",
        "reminiscence",
        "unfinished",
        "memory surfaced",
        "web source",
        "music source",
        "image source",
    ]
    assert events[0] == ("chat", "兰兰", "今晚有流星雨，也给你配一首《夜曲》。", "chat")
    assert events[1] == ("material", "兰兰", "MUSIC", "night-track-key")
    assert events[6] == (
        "memory surfaced",
        "http://127.0.0.1:4321/record_surfaced/兰兰",
        {
            "json": {"reflection_ids": ["reflection-1", "reflection-2"]},
            "timeout": 5.0,
        },
    )
    assert events[7:] == [
        ("web source", web["url"], web["title"]),
        ("music source", music["url"], "夜曲 - 周杰伦"),
        ("image source", meme["url"], meme["title"]),
    ]
    assert result == contracts.ProactiveChatResult(
        body={
            "success": True,
            "reason_code": contracts.PROACTIVE_REASON_CHAT_DELIVERED,
            "action": "chat",
            "message": "主动搭话已发送",
            "lanlan_name": "兰兰",
            "source_mode": "chat",
            "source_tag": "CHAT",
            "active_channels": ["web", "music", "meme"],
            "source_links": [web, music, meme],
            "turn_id": "committed-turn",
            "stage": contracts.PROACTIVE_STAGE_DELIVERY,
        }
    )


@pytest.mark.asyncio
async def test_optional_and_unselected_side_effects_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_recorders(monkeypatch, events)
    mgr = _Manager(events)
    delivery = service.CommittedDelivery(
        primary_channel="vision",
        source_links=[{"title": "屏幕", "source": "屏幕内容"}],
        delivered_tag="CHAT",
        delivered_music_link=None,
        is_music_used=False,
        action_note="",
        vision_screenshot_b64=None,
    )

    result = await _record(
        delivery,
        mgr,
        source_tag="VISION",
        active_channels=["vision"],
        has_unfinished_thread=False,
        surfaced_reflection_ids=[],
    )

    assert [event[0] for event in events] == [
        "chat",
        "material",
        "mini counter",
        "total",
    ]
    assert result.body["source_mode"] == "vision"
    assert result.body["source_tag"] == "VISION"
    assert result.body["source_links"] == delivery.source_links


@pytest.mark.asyncio
async def test_source_history_preserves_title_only_web_and_music_used_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_recorders(monkeypatch, events)
    mgr = _Manager(events)
    fallback_music = {
        "title": "备用曲目",
        "artist": "备用歌手",
        "url": "https://example.test/fallback",
        "source": "音乐推荐",
    }
    delivery = service.CommittedDelivery(
        primary_channel="music",
        source_links=[fallback_music],
        delivered_tag="MUSIC",
        delivered_music_link=fallback_music,
        is_music_used=True,
        action_note="",
        vision_screenshot_b64=None,
    )

    await _record(
        delivery,
        mgr,
        source_tag="MUSIC",
        has_unfinished_thread=False,
        surfaced_reflection_ids=[],
        selected_web_link=None,
        web_parsed={"title": "只有标题的网页话题"},
        selected_music_link=_music_link(),
        selected_meme_link=_meme_link(),
    )

    # Legacy behavior intentionally records a title-only web topic and a music
    # selection once music was delivered.  The unlinked meme candidate is not
    # recorded because it never made it into committed source_links.
    assert [event[0] for event in events] == [
        "chat",
        "material",
        "mini counter",
        "total",
        "web source",
        "music source",
    ]
    assert events[-2] == ("web source", "", "只有标题的网页话题")
    assert events[-1] == (
        "music source",
        _music_link()["url"],
        "夜曲 - 周杰伦",
    )
