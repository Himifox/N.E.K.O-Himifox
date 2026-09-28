"""Reproduction contracts for duplicate context on the chat-API path.

No live provider is called. Real preparation, final swap, callback bookkeeping,
and offline system-message assembly run with configuration/HTTP stubs. These
tests assert the desired once-only behavior, so affected cases currently fail.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from main_logic.omni_offline_client import OmniOfflineClient
from tests.unit.test_hot_swap_cancellation import (
    _FakeSession,
    _drain_task,
    _make_swap_manager,
)


class _ChatSession(OmniOfflineClient):
    """Keep real connect/prime_context; never construct an API client."""

    def __init__(self):
        self.model = "qwen3.7-plus"
        self.closed = False
        self._conversation_history = []

    async def close(self):
        self.closed = True

    async def handle_messages(self):
        await asyncio.Event().wait()


def _manager(monkeypatch):
    mgr = _make_swap_manager()
    mgr.input_mode = "text"
    mgr.session = _FakeSession("old")
    mgr.pending_agent_callbacks = []
    mgr.voice_id = "test-voice"
    mgr.memory_server_port = 1
    mgr.is_preparing_new_session = True
    mgr.pending_session_warmed_up_event = asyncio.Event()
    mgr._config_manager = SimpleNamespace(
        aensure_region_resolved=AsyncMock(),
        aget_core_config=AsyncMock(return_value={"AUDIO_API_KEY": "unused"}),
        aget_model_api_config=AsyncMock(return_value={"model": "qwen3.7-plus"}),
        aget_character_data=AsyncMock(return_value=(None,) * 9),
        cleanup_invalid_voice_ids=MagicMock(return_value=(0, [])),
    )
    mgr._enqueue_voice_migration_notice = MagicMock()
    mgr._apply_voice_id_for_route = MagicMock()
    mgr._resolve_session_use_tts = MagicMock(return_value=False)
    mgr._register_builtin_tools = MagicMock()
    mgr.tool_registry = SimpleNamespace(all=lambda: [])
    mgr._get_text_guard_max_length = MagicMock(return_value=1024)
    mgr._build_initial_prompt = AsyncMock(return_value="SYSTEM\n")
    mgr._make_tool_call_handler = MagicMock(return_value=None)
    mgr._bind_session_lifecycle_callbacks = MagicMock()
    mgr._new_dialog_request_kwargs = lambda: {}
    pending = _ChatSession()
    mgr._create_offline_vlm_client = MagicMock(return_value=pending)
    monkeypatch.setattr(
        "main_logic.core.lifecycle.ensure_default_yui_voice_for_free_api",
        AsyncMock(),
    )
    return mgr, pending


async def _swap(mgr, pending):
    mgr.is_hot_swap_imminent = True
    await mgr._perform_final_swap_sequence()
    assert mgr.session is pending, "fixture must reach successful promotion"
    assert not pending.closed
    return pending._conversation_history[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["before", "during", "after"])
async def test_chat_swap_injects_each_cache_entry_once(monkeypatch, arrival):
    """Only arrivals inside the memory HTTP await should expose the race."""
    mgr, pending = _manager(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    marker = "SCREEN_CACHE_UNIQUE_001"
    entry = {"role": mgr.lanlan_name, "text": marker}

    async def get(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(is_success=True, text="MEMORY\n")

    monkeypatch.setattr(
        "utils.internal_http_client.get_internal_http_client",
        lambda: SimpleNamespace(get=get),
    )
    if arrival == "before":
        mgr.message_cache_for_new_session.append(entry)
    prep = asyncio.create_task(mgr._background_prepare_pending_session())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if arrival == "during":
            mgr.message_cache_for_new_session.append(entry)
        release.set()
        await asyncio.wait_for(prep, 3)
        assert mgr.pending_session_warmed_up_event.is_set()
        assert mgr.pending_session is pending
        if arrival == "after":
            mgr.message_cache_for_new_session.append(entry)
        content = await _swap(mgr, pending)
        assert content.count(marker) == 1, content
    finally:
        release.set()
        await _drain_task(prep)
        await _drain_task(mgr.message_handler_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_mode", ["passive", "proactive"])
async def test_consumed_chat_callback_is_not_reinjected_by_swap(monkeypatch, delivery_mode):
    """Ordinary chat consumes a callback; its fallback must not replay later."""
    mgr, pending = _manager(monkeypatch)
    mgr._normalize_context_text_for_source = lambda _source, text: text
    marker = "SCREEN_CALLBACK_UNIQUE_002"
    mgr.enqueue_agent_callback({
        "origin": "event",
        "source_kind": "agent",
        "summary": marker,
        "delivery_mode": delivery_mode,
    })
    assert len(mgr.pending_agent_callbacks) == 1
    # Exercise selection, render, ack, and queue removal, not a hand-built mirror.
    rendered = mgr.drain_agent_callbacks_for_llm()
    assert marker in rendered
    assert not mgr.pending_agent_callbacks
    assert mgr.drain_agent_callbacks_for_llm() == ""
    await pending.connect("SYSTEM\n")
    mgr.pending_session = pending
    try:
        content = await _swap(mgr, pending)
        assert marker not in content, content
    finally:
        await _drain_task(mgr.message_handler_task)
