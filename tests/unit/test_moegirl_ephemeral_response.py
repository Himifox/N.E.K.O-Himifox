from __future__ import annotations

from types import SimpleNamespace

import pytest


def _make_client():
    from main_logic.omni_offline_client import OmniOfflineClient
    from utils.llm_client import SystemMessage

    client = OmniOfflineClient.__new__(OmniOfflineClient)
    client._proactive_image_to_inject = None
    client._proactive_image_staged_at = 0.0
    client._proactive_image_history_len = 0
    client.vision_provider_type = None
    client.llm = SimpleNamespace(max_completion_tokens=None)
    client.base_url = "free"
    client.lanlan_name = "Test"
    client.master_name = "User"
    client._prefix_buffer_size = 0
    client._conversation_history = [SystemMessage(content="system")]
    client._pending_images = []
    client._is_responding = False
    client._recent_responses = []
    client._repetition_threshold = 0.8
    client._max_recent_responses = 3
    client.max_response_length = 9999
    client.max_response_rerolls = 0
    client.max_tool_iterations = 1
    client.enable_response_guard = False
    client.enable_long_response_summary = False
    client.vision_model = ""
    client.model = "test-model"
    client._tool_definitions = []
    client.on_text_delta = None
    client.on_input_transcript = None
    client.on_response_discarded = None
    client.on_repetition_detected = None
    return client


@pytest.mark.asyncio
async def test_ephemeral_meme_instruction_follows_user_and_is_not_persisted(
    monkeypatch,
):
    from main_logic.omni_offline_client import OmniOfflineClient
    from utils.llm_client import AIMessage, HumanMessage, LLMStreamChunk

    sent_messages = []

    async def _astream(self, messages, **_overrides):
        sent_messages.extend(messages)
        yield LLMStreamChunk(content="reply")

    async def _noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(OmniOfflineClient, "_astream_with_tools", _astream)
    client = _make_client()
    client.on_response_done = _noop
    client.on_status_message = _noop

    await client.stream_text(
        "raw user message",
        ephemeral_response_instruction="reply to the preceding user message using this meme card",
        history_replacement_text="memory summary",
    )

    sent_humans = [
        message.content
        for message in sent_messages
        if isinstance(message, HumanMessage)
    ]
    assert sent_humans[-2:] == [
        "raw user message",
        "reply to the preceding user message using this meme card",
    ]
    assert isinstance(client._conversation_history[1], HumanMessage)
    assert client._conversation_history[1].content == "memory summary"
    assert not any(
        "meme card" in str(getattr(message, "content", ""))
        for message in client._conversation_history
    )
    assert isinstance(client._conversation_history[-1], AIMessage)
    assert client._conversation_history[-1].content == "reply"


@pytest.mark.asyncio
async def test_ephemeral_meme_instruction_is_removed_after_stream_error(monkeypatch):
    from main_logic.omni_offline_client import OmniOfflineClient

    async def _astream(self, messages, **_overrides):
        raise RuntimeError("test stream failure")
        if False:  # pragma: no cover - keeps this an async generator
            yield None

    async def _noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(OmniOfflineClient, "_astream_with_tools", _astream)
    client = _make_client()
    client.on_response_done = _noop
    client.on_status_message = _noop

    await client.stream_text(
        "raw user message",
        ephemeral_response_instruction="discard this temporary meme instruction",
    )

    assert not any(
        "temporary meme instruction" in str(getattr(message, "content", ""))
        for message in client._conversation_history
    )


@pytest.mark.asyncio
async def test_ephemeral_instruction_is_not_added_when_transcript_callback_fails():
    from utils.llm_client import HumanMessage

    async def _fail(_text: str):
        raise RuntimeError("test transcript failure")

    client = _make_client()

    with pytest.raises(RuntimeError, match="test transcript failure"):
        await client.stream_text(
            "raw user message",
            ephemeral_response_instruction="must never persist",
            input_transcript_callback=_fail,
        )

    human_messages = [
        message.content
        for message in client._conversation_history
        if isinstance(message, HumanMessage)
    ]
    assert human_messages == ["raw user message"]


@pytest.mark.asyncio
async def test_companion_chat_public_knowledge_tool_is_sample_only(monkeypatch):
    import main_logic.moegirl_knowledge_tool as knowledge_tool
    from main_logic.tool_calling import ToolRegistry

    captured = {}

    async def _handle(arguments, *, language, deadline_monotonic=None):
        del language, deadline_monotonic
        captured.update(arguments)
        return "sampled"

    monkeypatch.setattr(knowledge_tool, "handle_public_knowledge_call", _handle)
    registry = ToolRegistry()
    knowledge_tool.register_public_knowledge_tool(
        registry,
        language="zh",
        lookup_enabled=False,
    )

    tool = registry.get("query_public_knowledge")
    assert tool is not None
    assert tool.parameters["properties"]["mode"]["enum"] == ["sample"]
    assert tool.parameters["properties"]["mode"]["default"] == "sample"
    assert (
        await tool.handler({"query": "dataset:animals", "mode": "lookup"}) == "sampled"
    )
    assert captured["mode"] == "sample"


@pytest.mark.asyncio
async def test_every_plain_user_turn_runs_host_owned_material_selection(monkeypatch):
    import main_logic.moegirl_knowledge_tool as knowledge_tool
    from knowledge.service import KnowledgeTurnContext

    received = []

    class _Service:
        async def abuild_conversation_context(
            self,
            user_text,
            *,
            lexical_queries,
            limit,
        ):
            received.append((user_text, lexical_queries, limit))
            return KnowledgeTurnContext(match_mode="automatic_miss")

    monkeypatch.setattr(knowledge_tool, "open_knowledge", lambda _root: _Service())

    context = await knowledge_tool.build_public_knowledge_turn_context(
        "你好呀",
        tool_calls_supported=True,
    )

    assert context == ""
    assert received
    assert received[0][0] == "你好呀"
