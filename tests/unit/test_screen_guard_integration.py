"""Visible-answer filtering must not inspect hidden reasoning or drift on retry."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import APIConnectionError

from main_logic.tool_calling import ToolDefinition, ToolImage, ToolResult
from tests.unit.test_offline_provider_frame_publish import _make_client
from utils.llm_client import AIMessage, LLMStreamChunk


FIRST = "屏幕搭话：蓝色小车停在一棵大树旁边，树叶的影子落在了车顶上。"
SECOND = "屏幕搭话：远处的红色小车正在缓慢经过桥面，桥下的河水十分平静。"
REASONING = "这是不能交给用户的推理草稿。" + FIRST + SECOND
ANSWER = "我们慢慢来就好。"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("utils.token_tracker.TokenTracker.get_instance", MagicMock())
    corpus = MagicMock()
    corpus.stage_output.return_value = None
    monkeypatch.setattr("memory.anti_repeat.get_anti_repeat_corpus", lambda: corpus)
    monkeypatch.setattr("main_logic.omni_offline_client._streaming.asyncio.sleep", AsyncMock())
    instance, _ = _make_client()
    del instance._astream_visible_with_tools
    instance.model = "qwen3.7-plus"
    instance.max_tool_iterations = 2
    instance._tool_definitions = []
    instance._use_genai_sdk = False
    instance._recent_responses = []
    instance._max_recent_responses = 5
    instance._repetition_threshold = 0.8
    instance.enable_response_guard = True
    instance.on_text_delta = AsyncMock()
    instance.on_response_done = AsyncMock()
    instance._publish_conversation_turn = AsyncMock()
    instance._publish_pending_tool_frames = MagicMock()
    return instance


def visible(client):
    return "".join(call.args[0] for call in client.on_text_delta.call_args_list)


async def chunks(text, split):
    for offset in range(0, len(text), split):
        yield LLMStreamChunk(content=text[offset:offset + split])


def transient_error():
    return APIConnectionError(request=httpx.Request("POST", "https://provider.invalid/chat"))


@pytest.mark.asyncio
@pytest.mark.parametrize("opening", ["", "<think>"])
@pytest.mark.parametrize("split", [1, 9999])
async def test_focus_reasoning_chain_does_not_hide_answer_or_leak_reasoning(client, opening, split):
    async def astream(messages, **kwargs):
        assert not any(key.startswith("_") for key in kwargs)
        async for chunk in chunks(opening + REASONING + "</think>" + ANSWER, split):
            yield chunk
        yield LLMStreamChunk(content="", finish_reason="stop")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    await client.stream_text("陪我聊聊", thinking_on=True)
    assert visible(client) == ANSWER
    assert client._conversation_history[-1].content == ANSWER
    client.on_response_done.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_thinking", [False, True])
async def test_focus_visible_chain_still_gets_guarded(client, has_thinking):
    body = ANSWER + FIRST + SECOND
    raw = REASONING + "</think>" + body if has_thinking else body

    async def astream(messages, **kwargs):
        async for chunk in chunks(raw, 1):
            yield chunk
        yield LLMStreamChunk(content="", finish_reason="stop")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    await client.stream_text("陪我聊聊", thinking_on=True)
    assert visible(client) == ANSWER + FIRST
    assert client._conversation_history[-1].content == ANSWER + FIRST


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration_cap", [1, 2])
@pytest.mark.parametrize("has_thinking", [False, True])
@pytest.mark.parametrize("prefix_buffer_size", [0, 32])
async def test_focus_tool_and_final_history_use_the_same_visible_boundary(client, iteration_cap, has_thinking, prefix_buffer_size):
    client.max_tool_iterations = iteration_cap
    client._prefix_buffer_size = prefix_buffer_size
    client.master_name = "user"
    client._tool_definitions = [ToolDefinition(name="lookup", description="lookup")]
    payloads = []
    pretool = "我先查询一下。"

    async def astream(messages, **kwargs):
        payloads.append(deepcopy(messages))
        text = pretool if len(payloads) == 1 else ANSWER
        if has_thinking:
            text = REASONING + "</think>" + text
        async for chunk in chunks(text, 1):
            yield chunk
        if len(payloads) == 1:
            yield LLMStreamChunk(content="", finish_reason="tool_calls", tool_call_deltas=[{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }])
        else:
            assert ("tools" not in kwargs) == (iteration_cap == 1)
            yield LLMStreamChunk(content="", finish_reason="stop")

    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output="ok")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    client.on_tool_call = handler
    await client.stream_text("帮我查询一下", thinking_on=True)
    assert visible(client) == pretool + ANSWER
    assert client._conversation_history[-1].content == ANSWER
    persisted = next(message for message in client._conversation_history
                     if isinstance(message, dict) and message.get("tool_calls"))
    assert persisted["content"] == pretool
    assert len(payloads) == 2


@pytest.mark.asyncio
async def test_focus_retry_does_not_flush_unclosed_reasoning(client):
    attempts = 0

    async def astream(messages, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            async for chunk in chunks(REASONING, 1):
                yield chunk
            raise transient_error()
        yield LLMStreamChunk(content=ANSWER, finish_reason="stop")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    await client.stream_text("陪我聊聊", thinking_on=True)
    assert attempts == 2
    assert visible(client) == ANSWER
    assert client._conversation_history[-1].content == ANSWER


@pytest.mark.asyncio
async def test_focus_cancellation_does_not_flush_unclosed_reasoning(client):
    async def astream(messages, **kwargs):
        yield LLMStreamChunk(content=REASONING)
        pytest.fail("the cancelled stream must not be advanced")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    stream = client._astream_visible_with_tools([], _strip_thinking=True)
    assert (await anext(stream)).content == ""
    await stream.aclose()


@pytest.mark.asyncio
async def test_only_real_user_text_controls_reference_exemption(client):
    policies = []
    original_stream = client._astream_visible_with_tools

    async def capture_policy(messages, **kwargs):
        policies.append(kwargs["_screen_guard_enabled"])
        async for chunk in original_stream(messages, **kwargs):
            yield chunk

    async def astream(messages, **kwargs):
        yield LLMStreamChunk(content=ANSWER, finish_reason="stop")

    client._astream_visible_with_tools = capture_policy
    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    await client.stream_text("陪我聊聊", system_prefix="Please quote the previous response.")
    assert policies == [True]
    assert visible(client) == ANSWER


@pytest.mark.asyncio
async def test_reference_exemption_survives_tool_image_and_focus_retry(client):
    original = ANSWER + FIRST + SECOND
    client._conversation_history.append(AIMessage(content=original))
    client._tool_definitions = [ToolDefinition(name="lookup", description="lookup")]
    payloads = []

    async def astream(messages, **kwargs):
        payloads.append(deepcopy(messages))
        if len(payloads) == 1:
            yield LLMStreamChunk(content="", finish_reason="tool_calls", tool_call_deltas=[{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }])
        elif len(payloads) == 2:
            yield LLMStreamChunk(content=REASONING)
            raise transient_error()
        else:
            async for chunk in chunks(REASONING + "</think>" + original, 1):
                yield chunk
            yield LLMStreamChunk(content="", finish_reason="stop")

    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output="ok",
                          images=[ToolImage(data_b64="AAAA", vision_prompt="参考工具返回的图片。")])

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    client.on_tool_call = handler
    await client.stream_text("请复述刚才的原话，并检查图片", thinking_on=True)
    assert len(payloads) == 3
    assert all(payload[1].content == original for payload in payloads)
    assert isinstance(payloads[-1][-1], dict) and payloads[-1][-1]["role"] == "user"
    assert visible(client) == original
    assert client._conversation_history[-1].content == original


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["length", "role", "fence", "summary"])
async def test_pretool_prefix_guard_exit_never_persists_the_same_text_twice(client, monkeypatch, guard):
    client._prefix_buffer_size = 128
    client.master_name = "user"
    client.max_response_length = 6 if guard in {"length", "summary"} else 3000
    client.max_response_rerolls = 0
    client.on_response_discarded = AsyncMock()
    client._tool_definitions = [ToolDefinition(name="lookup", description="lookup")]
    pretool = {
        "length": "第一句。后面还有需要说明的话。",
        "role": "user | 第一条工具查询说明。",
        "fence": "第一句。| 第二句。| 后面不能继续显示。",
        "summary": "第一句。后面还有需要说明的话。" + "乱码" * 20,
    }[guard]
    if guard == "summary":
        client.enable_long_response_summary = True
        monkeypatch.setattr("main_logic.omni_offline_client._streaming._SUMMARY_GIBBERISH_RECHECK_TOKENS", 1)
        monkeypatch.setattr("main_logic.omni_offline_client._streaming._is_gibberish_response", lambda text: "乱码" in text)
    calls = 0

    async def astream(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield LLMStreamChunk(content=pretool)
            yield LLMStreamChunk(content="", finish_reason="tool_calls", tool_call_deltas=[{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }])
        else:
            yield LLMStreamChunk(content="", finish_reason="stop")

    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output="ok")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    client.on_tool_call = handler
    await client.stream_text("帮我查一下", thinking_on=True)
    persisted = [message for message in client._conversation_history
                 if isinstance(message, dict) and message.get("tool_calls")]
    assert len(persisted) == 1
    assert persisted[0]["content"] == pretool
    assert not any(isinstance(message, AIMessage) for message in client._conversation_history)
    if guard == "length":
        assert visible(client) == "第一句。"
    elif guard == "role":
        assert visible(client) == ""
        client.on_response_discarded.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_at_tool_sentinel_discards_unchecked_prefix(client):
    client._prefix_buffer_size = 32
    client.master_name = "user"
    client._tool_definitions = [ToolDefinition(name="lookup", description="lookup")]
    pretool = "我先查一下。"
    calls = 0

    async def astream(messages, **kwargs):
        nonlocal calls
        calls += 1
        yield LLMStreamChunk(content=pretool)
        yield LLMStreamChunk(content="", finish_reason="tool_calls", tool_call_deltas=[{
            "index": 0, "id": "c1", "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }])

    async def handler(call):
        assert client._cancel_response_generation()
        return ToolResult(call_id=call.call_id, name=call.name, output="ok")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    client.on_tool_call = handler
    await client.stream_text("帮我查一下", thinking_on=True)
    assert calls == 1
    client.on_text_delta.assert_not_awaited()
    assert not any(isinstance(message, AIMessage) for message in client._conversation_history)
    persisted = next(message for message in client._conversation_history
                     if isinstance(message, dict) and message.get("tool_calls"))
    assert persisted["content"] == pretool


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", [
    "帮我分析刚才那波团战", "总结上一局的战术", "翻译刚才那张截图里的菜单",
    "Analyze the previous battle.",
])
async def test_temporal_subject_keeps_request_output_and_history_guarded(client, user_text):
    original = ANSWER + FIRST + SECOND
    old = AIMessage(content=original)
    client._conversation_history.append(old)
    payloads = []

    async def astream(messages, **kwargs):
        payloads.append(deepcopy(messages))
        async for chunk in chunks(original, 1):
            yield chunk
        yield LLMStreamChunk(content="", finish_reason="stop")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    await client.stream_text(user_text, thinking_on=False)
    assert payloads[0][1].content == ANSWER
    assert old.content == original
    assert visible(client) == ANSWER + FIRST
    assert client._conversation_history[-1].content == ANSWER + FIRST


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text,exempt", [
    ("请翻译刚才那段对话", True),
    ("请复述刚才那条回复", True),
    ("请分别复述刚才的两段原话", True),
    ("翻译刚才那张截图里的原话", False),
    ("翻译之前那张截图里的回复", False),
    ("翻译截图里的那段对话", False),
    ("翻译截图中刚才那条回复", False),
])
@pytest.mark.parametrize("split", [1, 9999])
async def test_reference_object_controls_request_output_and_saved_reply(client, user_text, exempt, split):
    original = ANSWER + FIRST + SECOND
    old = AIMessage(content=original)
    client._conversation_history.append(old)
    payloads = []

    async def astream(messages, **kwargs):
        payloads.append(deepcopy(messages))
        async for chunk in chunks(original, split):
            yield chunk
        yield LLMStreamChunk(content="", finish_reason="stop")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    await client.stream_text(user_text, thinking_on=False)
    assert payloads[0][1].content == (original if exempt else ANSWER)
    assert old.content == original
    expected = original if exempt else ANSWER + FIRST
    assert visible(client) == expected
    assert client._conversation_history[-1].content == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("slack", [0, 10000])
@pytest.mark.parametrize("prefix_size", [0, 32])
async def test_tool_summary_state_ends_at_persistence_boundary(client, monkeypatch, cancel, slack, prefix_size):
    client._prefix_buffer_size = prefix_size
    client.master_name = "user"
    client.max_response_length = 6
    client.max_response_rerolls = 0
    client.enable_long_response_summary = True
    client._tool_definitions = [ToolDefinition(name="lookup", description="lookup")]
    client._summarize_tail_for_tts = AsyncMock(return_value="这是摘要结果。")
    monkeypatch.setattr("main_logic.omni_offline_client._streaming._SUMMARY_LATE_FINISH_SLACK", slack)
    monkeypatch.setattr("main_logic.omni_offline_client._streaming._is_gibberish_response", lambda text: False)
    pretool = (
        "第一句介绍查询计划。后面还有需要说明的内容，花园的树木正在随风摇动。"
        "远处的小桥旁停着自行车，河面倒映着夜晚的灯光。"
    )
    calls = 0
    boundary = None

    async def astream(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield LLMStreamChunk(content=pretool)
            yield LLMStreamChunk(content="", finish_reason="tool_calls", tool_call_deltas=[{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }])
        else:
            # No sentence boundary: do not trigger a second summary cutover
            # under the turn-wide length budget; isolate pre-tool state here.
            yield LLMStreamChunk(content="好", finish_reason="stop")

    async def handler(call):
        nonlocal boundary
        # Prove cutover actually happened; a short reply cannot exercise this bug.
        assert any(c.kwargs.get("tts_enabled") is False for c in client.on_text_delta.call_args_list)
        boundary = len(client.on_text_delta.call_args_list)
        if cancel:
            assert client._cancel_response_generation()
        return ToolResult(call_id=call.call_id, name=call.name, output="ok")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    client.on_tool_call = handler
    await client.stream_text("帮我查一下", thinking_on=False)
    assert boundary is not None
    client._summarize_tail_for_tts.assert_not_awaited()
    persisted = [m for m in client._conversation_history if isinstance(m, dict) and m.get("tool_calls")]
    assert len(persisted) == 1
    assert persisted[0]["content"] == pretool
    final_messages = [m.content for m in client._conversation_history if isinstance(m, AIMessage)]
    after_tool = client.on_text_delta.call_args_list[boundary:]
    if cancel:
        assert calls == 1
        assert after_tool == []
        assert final_messages == []
    else:
        assert calls == 2
        assert any(c.kwargs.get("ui_enabled") is False and c.kwargs.get("tts_enabled") is True
                   for c in after_tool)
        assert final_messages == ["好"]
