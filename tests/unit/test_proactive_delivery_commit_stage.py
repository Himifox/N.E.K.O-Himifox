"""Focused tests for the proactive delivery-commit stage."""

from __future__ import annotations

from typing import Any

import pytest

from main_logic.proactive_chat import contracts, service


class _FakeState:
    def __init__(self, *, preempted: bool = False) -> None:
        self.preempted = preempted
        self.checked_sids: list[str] = []

    def is_proactive_preempted(self, speech_id: str) -> bool:
        self.checked_sids.append(speech_id)
        return self.preempted


class _FakeManager:
    """Strict commit collaborator: it deliberately has no history recorder."""

    def __init__(
        self,
        *,
        finish_result: bool = True,
        finish_error: Exception | None = None,
        preempted: bool = False,
    ) -> None:
        self.finish_result = finish_result
        self.finish_error = finish_error
        self.state = _FakeState(preempted=preempted)
        self.current_speech_id = "current-turn"
        self.events: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def feed_tts_chunk(self, *args: Any, **kwargs: Any) -> None:
        self.events.append(("feed", args, kwargs))

    async def finish_proactive_delivery(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        self.events.append(("finish", args, kwargs))
        if self.finish_error is not None:
            raise self.finish_error
        return self.finish_result

    async def handle_new_message(self) -> None:
        self.events.append(("handle_new_message", (), {}))


def _music_track() -> dict[str, str]:
    return {
        "title": "夜曲",
        "artist": "周杰伦",
        "url": "https://example.test/nocturne",
        "source": "音乐推荐",
        "type": "music",
    }


async def _commit(
    mgr: _FakeManager,
    **overrides: Any,
) -> service.DeliveryCommit:
    values: dict[str, Any] = {
        "mgr": mgr,
        "proactive_sid": "proactive-turn",
        "lanlan_name": "兰兰",
        "response_text": "给你放一首《夜曲》。",
        "source_tag": "MUSIC",
        "active_channels": ["music"],
        "selected_web_link": None,
        "selected_music_link": _music_track(),
        "selected_meme_link": None,
        "music_content": None,
        "is_music_used": True,
        "is_playing_music": False,
        "music_cooldown": False,
        "vision_content": None,
        "phase2_use_vision": True,
        "screenshot_b64": "screen-base64",
        "proactive_lang": "zh",
        "master_name": "博士",
    }
    values.update(overrides)
    return await service._commit_proactive_delivery(**values)


@pytest.mark.asyncio
async def test_success_feeds_before_finish_and_returns_committed_facts() -> None:
    mgr = _FakeManager()
    selected_track = _music_track()

    committed = await _commit(mgr, selected_music_link=selected_track)

    assert [event[0] for event in mgr.events] == ["feed", "finish"]
    assert mgr.events[0] == (
        "feed",
        ("给你放一首《夜曲》。",),
        {"expected_speech_id": "proactive-turn"},
    )

    finish_args = mgr.events[1]
    assert finish_args[1] == ("给你放一首《夜曲》。",)
    assert finish_args[2]["expected_speech_id"] == "proactive-turn"
    assert finish_args[2]["source_tag"] == "MUSIC"
    assert finish_args[2]["vision_screenshot_b64"] == "screen-base64"
    assert "夜曲" in finish_args[2]["action_note"]
    assert "博士" in finish_args[2]["action_note"]

    assert committed.result is None
    assert committed.delivery is not None
    assert committed.delivery.primary_channel == "music"
    assert committed.delivery.delivered_tag == "MUSIC"
    assert committed.delivery.delivered_music_link == selected_track
    assert committed.delivery.source_links == [selected_track]
    assert committed.delivery.is_music_used is True
    assert committed.delivery.action_note == finish_args[2]["action_note"]
    assert committed.delivery.vision_screenshot_b64 == "screen-base64"


@pytest.mark.asyncio
async def test_finish_false_returns_preempted_without_recordable_delivery() -> None:
    mgr = _FakeManager(finish_result=False)

    committed = await _commit(mgr)

    assert [event[0] for event in mgr.events] == ["feed", "finish"]
    assert committed.delivery is None
    assert committed.result is not None
    assert committed.result.body["action"] == "pass"
    assert (
        committed.result.body["reason_code"]
        == contracts.PROACTIVE_REASON_DELIVERY_PREEMPTED
    )
    assert committed.result.body["lanlan_name"] == "兰兰"
    assert committed.result.body["turn_id"] == "current-turn"
    # No post-commit/history collaborator exists at this stage.  With no
    # CommittedDelivery returned, the following recording stage cannot run.
    assert "handle_new_message" not in [event[0] for event in mgr.events]


@pytest.mark.asyncio
async def test_unpreempted_delivery_error_clears_tts_and_returns_failed() -> None:
    mgr = _FakeManager(finish_error=RuntimeError("finish failed"))

    committed = await _commit(mgr)

    assert [event[0] for event in mgr.events] == [
        "feed",
        "finish",
        "handle_new_message",
    ]
    assert mgr.state.checked_sids == ["proactive-turn"]
    assert committed.delivery is None
    assert committed.result is not None
    assert committed.result.body["action"] == "pass"
    assert (
        committed.result.body["reason_code"]
        == contracts.PROACTIVE_REASON_DELIVERY_FAILED
    )


@pytest.mark.asyncio
async def test_preempted_delivery_error_does_not_clear_user_tts() -> None:
    mgr = _FakeManager(
        finish_error=RuntimeError("finish failed after takeover"),
        preempted=True,
    )

    committed = await _commit(mgr)

    assert [event[0] for event in mgr.events] == ["feed", "finish"]
    assert mgr.state.checked_sids == ["proactive-turn"]
    assert committed.delivery is None
    assert committed.result is not None
    assert (
        committed.result.body["reason_code"]
        == contracts.PROACTIVE_REASON_DELIVERY_FAILED
    )

