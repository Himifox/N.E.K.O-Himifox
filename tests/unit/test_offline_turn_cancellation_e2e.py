"""Whole offline turns: cancellation, history commits and the request view.

Only the provider transport is faked (``self.llm.astream``, or the genai
``generate_content_stream``). ``stream_text`` / ``prompt_ephemeral``, the
visible filter, the tool loop and the executor are production code, so a lost
``_response_generation`` hop, a provider call site that sends ``messages``
instead of the request view, or a history commit in the wrong place turns
these red. Each test says which of those it pins.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main_logic.omni_offline_client._genai_support as _ofc_genai
from config.prompts.prompts_screen_history import SCREEN_HISTORY_PLACEHOLDER
from main_logic.tool_calling import ToolDefinition, ToolImage, ToolResult
from tests.unit.test_offline_provider_frame_publish import _make_client, _png_b64
from tests.unit.test_tool_calling import (
    _GenaiChunk, _GenaiFunctionCall, _GenaiPart,
)
from utils.llm_client import AIMessage, HumanMessage, LLMStreamChunk

pytestmark = pytest.mark.unit

_COMMENT_A = "屏幕搭话 蓝色小车停在一棵大树旁边，树叶的影子落在了车顶上。"
_COMMENT_B = "屏幕搭话 远处的红色小车正在缓慢经过桥面，桥下的河水十分平静。"


def _text(content, finish=None):
    return LLMStreamChunk(content=content, finish_reason=finish)


def _tool_calls(*ids, text=""):
    return LLMStreamChunk(content=text, finish_reason="tool_calls", tool_call_deltas=[
        {"index": i, "id": call_id, "type": "function",
         "function": {"name": "lookup", "arguments": "{}"}}
        for i, call_id in enumerate(ids)
    ])


def _client(provider="openai", *, handler=None, cap=2, language="zh"):
    """A stream_text/prompt_ephemeral-capable client over the real tool loop.

    ``client.script`` lists one provider response per request: a list of
    chunks (callables are awaited in place, to cancel mid-stream) or an
    exception raised before the first chunk. ``client.requests`` records what
    each request carried: OpenAI messages, or genai ``contents``.
    """
    client, _ = _make_client()
    del client._astream_visible_with_tools  # the real filter + tool loop
    client._publish_provider_frames = MagicMock()
    # Bus copies are out of scope; close them so none is left un-awaited.
    client._fire_bus_task = lambda coro: coro.close()
    client.master_name = "M"
    client.lanlan_name = "L"
    client.enable_response_guard = False
    client._recent_responses = []
    client._max_recent_responses = 5
    client._repetition_threshold = 0.8
    client.on_text_delta = AsyncMock()
    client.on_response_done = AsyncMock()
    client.on_proactive_done = AsyncMock()
    client._notify_reasoning_done = AsyncMock()
    client._user_language_provider = lambda: language
    client.max_tool_iterations = cap
    client.on_tool_call = handler
    client.on_tool_round_start = None
    client._tool_definitions = [ToolDefinition(
        name="lookup", description="lookup",
        parameters={"type": "object", "properties": {}},
    )]
    client._openai_tools_unsupported = False
    client._openai_tools_unsupported_with_images = False
    client._genai_tools_unsupported = False
    client._use_genai_sdk = provider == "gemini"
    client._genai_client = None
    client.script = []
    client.requests = []

    def next_step():
        steps = client.script
        return steps[min(len(client.requests) - 1, len(steps) - 1)]

    async def play(step):
        for item in step:
            if callable(item):
                await item()
            else:
                yield item

    def astream(messages, **kwargs):
        client.requests.append(list(messages))
        step = next_step()

        async def run():
            if isinstance(step, BaseException):
                raise step
            async for chunk in play(step):
                yield chunk
        return run()

    client.llm = SimpleNamespace(astream=astream, max_completion_tokens=100)
    if provider == "gemini":
        client.model = "gemini-2.5-flash"
        client.api_key = "fake"
        client._ensure_genai_client = lambda: None

        class _Models:
            @staticmethod
            async def generate_content_stream(**kwargs):
                client.requests.append(kwargs["contents"])
                return play(next_step())

        client._genai_client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    return client


@pytest.fixture(autouse=True)
def _genai_available(monkeypatch):
    monkeypatch.setattr(_ofc_genai, "_GENAI_AVAILABLE", True)
    monkeypatch.setattr("utils.token_tracker.TokenTracker.get_instance", MagicMock())


def _gemini_calls(*ids, text=None):
    parts = [_GenaiPart(text=text)] if text else []
    parts += [_GenaiPart(function_call=_GenaiFunctionCall("lookup", id_=i)) for i in ids]
    return _GenaiChunk(parts)


def _history_shape(client):
    shape = []
    for message in client._conversation_history[1:]:
        if isinstance(message, dict):
            shape.append((message["role"], message.get("content"),
                          [c["id"] for c in message.get("tool_calls", [])] or None))
        else:
            shape.append((message.type, message.content, None))
    return shape


def _emitted(client):
    return [call.args[0] for call in client.on_text_delta.await_args_list]


# ── History commits on cancellation ─────────────────────────────────────────

async def test_cancel_after_emitting_keeps_the_visible_reply_in_its_own_turn():
    """Text the user already saw stays in history, before the turn that
    cancelled it; nothing produced after the cancellation is emitted."""
    client = _client()

    async def interrupt():
        await client.handle_interruption()
        client._conversation_history.append(HumanMessage(content="Q2"))

    client.script = [[
        _text("Hello there, I was about to "), _text("say something long"),
        interrupt, _text(" and then some"), _text("", "stop"),
    ]]
    await client.stream_text("Q1")

    assert _emitted(client) == ["Hello there, I was about to ", "say something long"]
    assert _history_shape(client) == [
        ("human", "Q1", None),
        ("ai", "Hello there, I was about to say something long", None),
        ("human", "Q2", None),
    ]
    client.on_response_done.assert_not_awaited()


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("entry", ["stream_text", "prompt_ephemeral"])
async def test_cancel_during_a_tool_keeps_the_executed_call_and_stops(provider, entry):
    """The e2e token wiring: stream_text / prompt_ephemeral hand their
    generation down to the tool loop. Cancelled inside the first tool, the
    provider is asked exactly once, the second call never runs, and the call
    that did run keeps its record (it already had its side effect)."""
    executed = []

    async def handler(call):
        executed.append(call.call_id)
        await client.handle_interruption()
        return ToolResult(call_id=call.call_id, name=call.name, output={"sent": True})

    client = _client(provider, handler=handler)
    if provider == "gemini":
        client.script = [[_gemini_calls("c1", "c2", text="好的，我这就发")]]
    else:
        client.script = [[_text("好的，我这就发"), _tool_calls("c1", "c2")]]
    if entry == "stream_text":
        await client.stream_text("帮我发消息")
    else:
        assert await client.prompt_ephemeral("callback") is True

    assert executed == ["c1"]
    assert len(client.requests) == 1, "no provider request after the cancellation"
    client.on_response_done.assert_not_awaited()
    client.on_proactive_done.assert_not_awaited()
    if entry == "stream_text":
        assert _history_shape(client) == [
            ("human", "帮我发消息", None),
            ("assistant", "好的，我这就发", ["c1"]),
            ("tool", json.dumps({"sent": True}), None),
        ], "the pre-tool text is committed once, inside the kept round"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_cancel_before_any_call_commits_the_pretool_text_as_the_reply(provider):
    """No call ran: the round is dropped, the text the user heard is not."""
    executed = []

    async def handler(call):
        executed.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client(provider, handler=handler)
    client.on_tool_round_start = client.handle_interruption
    if provider == "gemini":
        client.script = [[_gemini_calls("c1", text="好的，我这就发")]]
    else:
        client.script = [[_text("好的，我这就发"), _tool_calls("c1")]]
    await client.stream_text("帮我发消息")

    assert executed == []
    assert len(client.requests) == 1
    assert _history_shape(client) == [
        ("human", "帮我发消息", None), ("ai", "好的，我这就发", None),
    ]


async def test_replacement_and_tool_image_slots_survive_a_shifted_history():
    """A concurrent commit that inserts before this turn moves its messages;
    the long prompt must still become the short replacement and the tool
    image's base64 must still be swapped out, found by identity."""
    image = ToolImage(data_b64=_png_b64(4, 4, (10, 20, 30)), mime="image/png")

    async def handler(call):
        # Another turn's late commit lands ahead of this one.
        client._conversation_history.insert(1, AIMessage(content="late"))
        return ToolResult(call_id=call.call_id, name=call.name, output={}, images=[image])

    client = _client(handler=handler)
    client.script = [[_tool_calls("c1")], [_text("看到了"), _text("", "stop")]]
    await client.stream_text("long prompt " * 20, history_replacement_text="short")

    history = client._conversation_history
    assert [m.content for m in history if isinstance(m, HumanMessage)] == ["short"]
    assert image.data_b64 not in json.dumps(
        [m if isinstance(m, dict) else m.content for m in history], ensure_ascii=False,
    )
    assert len(client.requests) == 2
    assert image.data_b64 in json.dumps(client.requests[1], ensure_ascii=False, default=repr)


# ── Independent deliveries are marked ───────────────────────────────────────

async def test_persisted_ephemeral_replies_are_marked_as_independent_deliveries():
    """Callbacks and greetings answer an instruction, not the user, so the
    guard must not join them with the reply to the user's turn."""
    from utils.screen_comment_guard import project_screen_history

    client = _client()
    client._conversation_history += [HumanMessage(content="聊"), AIMessage(content="正常回复。")]
    for comment in (_COMMENT_A, _COMMENT_B):
        client.script = [[_text(comment), _text("", "stop")]]
        client.requests.clear()
        assert await client.prompt_ephemeral("callback")
    delivered = client._conversation_history[-2:]
    assert [m.additional_kwargs for m in delivered] == [{"dialog_source": "proactive"}] * 2
    messages = client._conversation_history + [HumanMessage(content="继续")]
    assert project_screen_history(messages) is messages


# ── The request view reaches every provider call site ───────────────────────

def _poisoned(client):
    client._conversation_history += [
        HumanMessage(content="聊"), AIMessage(content=_COMMENT_A), AIMessage(content=_COMMENT_B),
    ]


def _assert_quarantined(payload, placeholder):
    text = json.dumps(payload, ensure_ascii=False, default=repr)
    assert _COMMENT_A not in text and _COMMENT_B not in text
    assert placeholder in text


@pytest.mark.parametrize("with_image", [False, True])
async def test_openai_tool_loop_and_forced_final_both_send_the_request_view(with_image):
    """Request 1 is the tool loop, request 2 the forced-final call (cap=1).
    A tool image appends a {"role": "user"} turn in place; the forced-final
    request must still find the run before the real user turn."""
    image = ToolImage(data_b64=_png_b64(4, 4, (1, 2, 3)), mime="image/png")

    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output={},
                          images=[image] if with_image else [])

    client = _client(handler=handler, cap=1)
    _poisoned(client)
    client.script = [[_tool_calls("c1")], [_text("好"), _text("", "stop")]]
    await client.stream_text("继续")

    assert len(client.requests) == 2
    for payload in client.requests:
        _assert_quarantined(payload, SCREEN_HISTORY_PLACEHOLDER["zh"])
    assert AIMessage(content=_COMMENT_A) in client._conversation_history, "saved as is"


async def _noop_tool(call):
    return ToolResult(call_id=call.call_id, name=call.name, output={})


async def test_tools_refusal_retry_sends_the_request_view():
    client = _client(handler=_noop_tool)
    _poisoned(client)
    client.script = [RuntimeError("this model does not support tools"),
                     [_text("好"), _text("", "stop")]]
    await client.stream_text("继续")
    assert len(client.requests) == 2
    for payload in client.requests:
        _assert_quarantined(payload, SCREEN_HISTORY_PLACEHOLDER["zh"])


async def test_gemini_tool_loop_and_forced_final_both_send_the_request_view():
    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client("gemini", handler=handler, cap=1, language="en")
    _poisoned(client)
    client.script = [[_gemini_calls("c1")], [_GenaiChunk([_GenaiPart(text="ok")])]]
    await client.stream_text("go on")

    assert len(client.requests) == 2
    for contents in client.requests:
        _assert_quarantined(contents, SCREEN_HISTORY_PLACEHOLDER["en"])


# ── Individual cancellation checks the loops rely on ────────────────────────

async def test_cancel_during_tools_refusal_prevents_the_retry():
    client = _client(handler=_noop_tool)

    async def refuse():
        await client.handle_interruption()
        raise RuntimeError("this model does not support tools")

    client.script = [[refuse]]
    await client.stream_text("hi")
    assert len(client.requests) == 1


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("cap", [1, 2])
async def test_cancel_while_the_round_sentinel_is_consumed_stops_the_next_request(
    provider, cap,
):
    """A round ends at the sentinel. A cancellation while the consumer handles
    it must stop the next request: the forced-final one after the last
    allowed round (cap=1), the next loop round otherwise (cap=2)."""
    async def handler(call):
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client(provider, handler=handler, cap=cap)
    client.script = ([[_gemini_calls("c1")], [_GenaiChunk([_GenaiPart(text="late")])]]
                     if provider == "gemini"
                     else [[_tool_calls("c1")], [_text("late"), _text("", "stop")]])
    generation = client._begin_response_generation()
    seen = []
    async for chunk in client._astream_with_tools(
        [HumanMessage(content="hi")], _response_generation=generation,
    ):
        seen.append(chunk)
        if getattr(chunk, "tool_round_persisted", False):
            await client.cancel_response()
    assert len(client.requests) == 1
    assert not [c for c in seen if getattr(c, "content", "")]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_cancel_between_tool_calls_stops_the_batch(provider):
    executed = []

    async def handler(call):
        executed.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client(provider, handler=handler)
    client._on_tool_result = None
    client.script = ([[_gemini_calls("c1", "c2")]] if provider == "gemini"
                     else [[_tool_calls("c1", "c2")]])
    real = client.on_tool_call

    async def cancelling(call):
        result = await real(call)
        await client.cancel_response()
        return result

    client.on_tool_call = cancelling
    generation = client._begin_response_generation()
    messages = [HumanMessage(content="hi")]
    _ = [c async for c in client._astream_with_tools(messages, _response_generation=generation)]
    assert executed == ["c1"]
    assert [m["tool_call_id"] for m in messages if isinstance(m, dict) and m["role"] == "tool"] == ["c1"]
    await asyncio.sleep(0)


# ── Where a cancelled reply lands (second review round) ─────────────────────

async def test_cancelled_proactive_reply_goes_before_the_interrupting_user_turn():
    """prompt_ephemeral's instruction is never saved, so the reply is anchored
    to the last message the turn saw; the interrupter's HumanMessage that was
    appended meanwhile stays after it."""
    client = _client()
    client._conversation_history.append(HumanMessage(content="earlier"))

    async def interrupt():
        await client.handle_interruption()
        client._conversation_history.append(HumanMessage(content="Q-new"))

    client.script = [[_text("刚才看到"), interrupt, _text("late"), _text("", "stop")]]
    assert await client.prompt_ephemeral("callback") is True
    assert _history_shape(client) == [
        ("human", "earlier", None), ("ai", "刚才看到", None), ("human", "Q-new", None),
    ]
    assert client._conversation_history[2].additional_kwargs == {"dialog_source": "proactive"}


async def test_cancel_during_the_prefix_flush_commits_before_the_interrupter():
    """A short reply sits whole in the name-prefix buffer and is emitted by
    the end-of-stream flush; a cancellation during that emit is caught at
    the commit, not only right after the stream loop."""
    client = _client()
    client._prefix_buffer_size = 100

    async def on_text_delta(text, is_first, **_kw):
        await client.handle_interruption()
        client._conversation_history.append(HumanMessage(content="Q2"))

    client.on_text_delta = AsyncMock(side_effect=on_text_delta)
    client.script = [[_text("短回复"), _text("", "stop")]]
    await client.stream_text("Q1")
    assert _history_shape(client) == [
        ("human", "Q1", None), ("ai", "短回复", None), ("human", "Q2", None),
    ]
    client.on_response_done.assert_not_awaited()


@pytest.mark.parametrize("content", ["", "   "])
def test_an_empty_cancelled_reply_is_never_written(content):
    """Some providers reject an empty assistant message."""
    client = _client()
    anchor = HumanMessage(content="Q1")
    client._conversation_history.append(anchor)
    before = list(client._conversation_history)
    client._commit_cancelled_reply(anchor, AIMessage(content=content))
    assert client._conversation_history == before
