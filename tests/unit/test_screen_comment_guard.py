"""Screen narration chains: request projection, stream safety, both entrypoints."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.screen_comment_guard import (
    ScreenCommentChainFilter,
    project_screen_history,
    screen_chain_start,
    screen_guard_enabled,
)


PREFIX = "谢谢你陪我，我们慢慢来就好。"
PARTS = (
    "蓝色小车停在一棵大树旁边，树叶的影子落在了车顶上。",
    "远处的红色小车正在缓慢经过桥面，桥下的河水十分平静。",
    "道路两旁的路灯已经亮了起来，暖色的灯光照在了路面上。",
)


def chain(label="屏幕搭话 "):
    return PREFIX + "".join(label + part for part in PARTS)


def filtered(chunks, *, enabled=True):
    guard = ScreenCommentChainFilter(enabled=enabled)
    return "".join(guard.feed(chunk) for chunk in chunks) + guard.finalize()


@pytest.mark.parametrize("label", ["屏幕搭话 ", "屏幕搭话：", "屏幕画面/", "/屏幕画面 ", "／屏幕内容／", "screen comment ", "screen observation/"])
def test_continuation_is_removed_at_every_split(label):
    text = chain(label)
    assert screen_chain_start(text) == len(PREFIX)
    expected = PREFIX + label + PARTS[0]
    for split in range(len(text) + 1):
        assert filtered([text[:split], text[split:]]) == expected
    assert filtered(list(text)) == expected


@pytest.mark.parametrize("text", [
    "", PREFIX, "屏", "屏幕搭", "屏幕搭话", "屏幕搭话 " + PARTS[0],
    "屏幕搭话 A。屏幕搭话 B。", "屏幕内容：" + PARTS[0] + "屏幕内容：" + PARTS[1],
    "```text\n" + chain() + "\n```", "~~~text\n" + chain() + "\n~~~",
    "例子：`" + chain() + "`", "他说：“" + chain() + "”",
    "引用：「" + chain() + "」", 'Quote: "' + chain() + '"',
    "引用：‘" + chain() + "’", "Quote: '" + chain() + "'",
    'JSON: "quoted \\"word\\"; ' + chain() + '"',
    "> " + chain(), "未闭合代码：```\n" + chain(),
    "<think>" + chain() + "</think>" + PREFIX,
    "/" + " " * 100 + "screen" + " " * 100 + "observation " + PARTS[0],
    PREFIX + "屏幕搭话 " + PARTS[0] + "屏幕搭话 短。screen observation/" + PARTS[1],
])
def test_normal_text_quotes_code_and_ambiguous_labels_are_preserved(text):
    assert screen_chain_start(text) is None
    for split in range(len(text) + 1):
        assert filtered([text[:split], text[split:]]) == text
    assert filtered(list(text)) == text


def test_single_candidate_is_not_lost_on_finalize_and_reset():
    guard = ScreenCommentChainFilter()
    text = "屏幕搭话 " + PARTS[0]
    assert guard.feed(text) + guard.finalize() == text
    guard.reset()
    assert guard.feed(chain()) + guard.finalize() == PREFIX + "屏幕搭话 " + PARTS[0]
    guard.reset()
    assert guard.feed(PREFIX) + guard.finalize() == PREFIX


def test_plain_text_streams_without_a_fixed_lookahead_delay():
    guard = ScreenCommentChainFilter()
    assert guard.feed("你好。") == "你好。"
    assert guard.feed("再见。") == "再见。"
    assert guard.finalize() == ""


@pytest.mark.parametrize("user_text", ["不要再重复屏幕搭话了", "别重复原话", "Please don't repeat your previous answer"])
def test_requests_to_stop_repeating_do_not_disable_guard(user_text):
    assert screen_guard_enabled([{"role": "user", "content": user_text}])


@pytest.mark.parametrize("user_text", ["分析这张地图该怎么走", "Analyze this map", "总结一下下一步的战术"])
def test_analysis_of_current_subject_does_not_exempt_history_replay(user_text):
    assert screen_guard_enabled([{"role": "user", "content": user_text}])


@pytest.mark.parametrize("user_text", ["请复述刚才的原话", "Please quote the previous response", "屏幕搭话是什么意思？", "分析刚才的回答"])
def test_explicit_reference_requests_keep_originals(user_text):
    messages = [{"role": "assistant", "content": chain()}, {"role": "user", "content": user_text}]
    assert not screen_guard_enabled(messages)
    assert project_screen_history(messages) is messages
    assert filtered(list(chain()), enabled=False) == chain()


def test_request_view_preserves_originals_roles_metadata_and_images():
    from utils.llm_client import AIMessage, HumanMessage, SystemMessage
    old = AIMessage(content=chain(), additional_kwargs={"source": "old"})
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    user = HumanMessage(content=[image, {"type": "text", "text": "不辛苦"}])
    messages = [SystemMessage(content=chain()), old,
                {"role": "assistant", "content": chain(), "tool_calls": [{"id": "call1"}]},
                {"role": "tool", "tool_call_id": "call1", "content": chain()}, user]
    snapshot = deepcopy(messages)
    projected = project_screen_history(messages)
    assert messages == snapshot
    assert projected[1].content == PREFIX
    assert projected[1].additional_kwargs == old.additional_kwargs
    assert projected[2]["content"] == PREFIX
    assert projected[2]["tool_calls"] == messages[2]["tool_calls"]
    assert all(projected[i] is messages[i] for i in (0, 3, 4))
    assert project_screen_history(projected) is projected


def test_quote_policy_can_be_frozen_before_tool_image_messages_are_appended():
    messages = [{"role": "assistant", "content": chain()},
                {"role": "user", "content": "Please quote the previous reply"}]
    enabled = screen_guard_enabled(messages)
    messages.append({"role": "user", "content": "Tool image context"})
    assert project_screen_history(messages, guard_enabled=enabled) is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["user", "callback"])
@pytest.mark.parametrize("split_size", [1, 13, 9999])
async def test_output_history_and_provider_request_are_guarded(monkeypatch, entry, split_size):
    from tests.unit.test_offline_provider_frame_publish import _make_client
    from utils.llm_client import AIMessage, LLMStreamChunk

    monkeypatch.setattr("utils.token_tracker.TokenTracker.get_instance", MagicMock())
    corpus = MagicMock()
    corpus.stage_output.return_value = None
    monkeypatch.setattr("memory.anti_repeat.get_anti_repeat_corpus", lambda: corpus)
    client, _ = _make_client()
    del client._astream_visible_with_tools
    client.model = "qwen3.7-plus"
    client.max_tool_iterations = 1
    client._tool_definitions = []
    client._use_genai_sdk = False
    client._recent_responses = []
    client._max_recent_responses = 5
    client._repetition_threshold = 0.8
    client.enable_response_guard = True
    client.on_text_delta = AsyncMock()
    client.on_response_done = AsyncMock()
    client.on_proactive_done = AsyncMock()
    client._publish_conversation_turn = AsyncMock()
    old = AIMessage(content=chain())
    client._conversation_history.append(old)
    payloads = []

    async def astream(messages, **kwargs):
        payloads.append(deepcopy(messages))
        for offset in range(0, len(chain()), split_size):
            yield LLMStreamChunk(content=chain()[offset:offset + split_size])
        yield LLMStreamChunk(content="", finish_reason="stop")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    if entry == "user":
        await client.stream_text("不辛苦")
        client.on_response_done.assert_awaited_once()
    else:
        assert await client.prompt_ephemeral("请分析新的陪伴话题并自然回应")
        client.on_proactive_done.assert_awaited_once_with(True)
    assert payloads[0][1].content == PREFIX
    assert old.content == chain(), "original history must remain recoverable"
    expected = PREFIX + "屏幕搭话 " + PARTS[0]
    assert "".join(call.args[0] for call in client.on_text_delta.call_args_list) == expected
    assert client._conversation_history[-1].content == expected


@pytest.mark.asyncio
async def test_tool_round_and_forced_final_answer_share_the_guard():
    from main_logic.omni_offline_client import OmniOfflineClient
    from main_logic.tool_calling import ToolDefinition, ToolResult
    from tests.unit.test_tool_calling import _init_bare
    from utils.llm_client import LLMStreamChunk

    client = _init_bare(OmniOfflineClient.__new__(OmniOfflineClient))
    client._use_genai_sdk = False
    client._tool_definitions = [ToolDefinition(name="lookup", description="lookup")]
    client.max_tool_iterations = 1  # next provider request is forced-finalize
    payloads = []

    async def astream(messages, **kwargs):
        payloads.append(deepcopy(messages))
        if len(payloads) == 1:
            yield LLMStreamChunk(content=chain())
            yield LLMStreamChunk(content="", finish_reason="tool_calls", tool_call_deltas=[{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }])
        else:
            yield LLMStreamChunk(content="查询结束。", finish_reason="stop")

    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output="ok")

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=3000)
    client.on_tool_call = handler
    messages = [{"role": "assistant", "content": chain()}, {"role": "user", "content": "查一下"}]
    chunks = [chunk async for chunk in client._astream_visible_with_tools(messages)]
    expected = PREFIX + "屏幕搭话 " + PARTS[0]
    assert "".join(chunk.content for chunk in chunks) == expected + "查询结束。"
    assert len(payloads) == 2
    assert all(payload[0]["content"] == PREFIX for payload in payloads)
    assert messages[0]["content"] == chain()
    pretool = next(message for message in messages if message.get("tool_calls"))
    assert pretool["content"] == expected
