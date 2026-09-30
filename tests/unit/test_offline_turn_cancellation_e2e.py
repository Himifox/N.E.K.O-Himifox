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


# ── What handle_interruption reports (third review round) ───────────────────

async def test_interruption_reports_false_when_nothing_is_live():
    client = _client()
    assert await client.handle_interruption() == ""


async def test_interruption_reports_true_for_a_live_stream():
    client = _client()
    seen = []

    async def interrupt():
        seen.append(await client.handle_interruption())

    client.script = [[_text("hi"), interrupt, _text("", "stop")]]
    await client.stream_text("Q1")
    assert seen == ["response"]


async def test_interruption_claims_a_finished_reply_awaiting_its_completion():
    """Finished and committed, still in the cleanup await: the interruption
    claims the completion (the interrupting turn closes it) instead of a late
    callback landing inside the new turn. The reply was delivered, so the
    call still reports True."""
    client = _client()
    seen = []

    async def cleanup(_owner):
        seen.append(await client.handle_interruption())

    client._notify_reasoning_done = cleanup
    client.script = [[_text("说完了。"), _text("", "stop")]]
    assert await client.prompt_ephemeral("callback", completion_mode="response") is True
    assert seen == ["response"]
    client.on_response_done.assert_not_awaited()


async def test_interruption_leaves_a_running_completion_alone():
    """Once the completion callback started, it owns the turn end: the
    interruption must report False so no second turn end is sent."""
    client = _client()
    seen = []

    async def proactive_done(_committed):
        seen.append(await client.handle_interruption())

    client.on_proactive_done = AsyncMock(side_effect=proactive_done)
    client.script = [[_text("说完了。"), _text("", "stop")]]
    assert await client.prompt_ephemeral("callback") is True
    assert seen == [""]
    client.on_proactive_done.assert_awaited_once()


async def test_a_turn_cancelled_before_its_first_chunk_still_publishes_its_frames():
    """stream_text publishes the turn's frames on the first chunk the
    provider sends. Cancelled while the request was in flight, the tool loop
    still hands that first (empty) chunk up, so the frames the provider did
    receive reach the plugin bus; nothing is shown."""
    client = _client()

    async def interrupt():
        await client.handle_interruption()

    client.script = [[interrupt, _text("late"), _text("", "stop")]]
    await client.stream_text("look", turn_images=[_png_b64(4, 4, (9, 9, 9))])
    client._publish_provider_frames.assert_called_once()
    assert _emitted(client) == []


# ── Request view pairs tool rounds (third review round) ─────────────────────

def _call(call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": "lookup", "arguments": "{}"}}


def _reply(call_id):
    return {"role": "tool", "tool_call_id": call_id, "name": "lookup", "content": "{}"}


def test_an_unanswered_tool_call_round_is_dropped_from_the_request_view():
    """Round A still waits on a slow tool when turn B builds its request:
    [.., A.assistant(tool_calls), B.user] is a 400 on OpenAI-compatible
    endpoints. The request view keeps A's text and drops the dangling call;
    the saved history is untouched."""
    client = _client()
    pending = {"role": "assistant", "content": "我查一下", "tool_calls": [_call("c1")]}
    messages = [HumanMessage(content="A"), pending, HumanMessage(content="B")]
    view = client._dialog_messages_for_provider(messages)
    assert view[1] == {"role": "assistant", "content": "我查一下"}
    assert view[0] is messages[0] and view[2] is messages[2]
    assert messages[1] is pending and pending["tool_calls"] == [_call("c1")]


def test_a_partly_answered_round_keeps_only_answered_calls():
    client = _client()
    turn = {"role": "assistant", "content": "", "tool_calls": [_call("c1"), _call("c2")]}
    messages = [HumanMessage(content="A"), turn, _reply("c1"), HumanMessage(content="B")]
    view = client._dialog_messages_for_provider(messages)
    assert view[1]["tool_calls"] == [_call("c1")]
    assert view[2] is messages[2] and len(view) == 4


def test_a_textless_unanswered_round_and_orphan_replies_are_dropped():
    client = _client()
    messages = [HumanMessage(content="A"),
                {"role": "assistant", "content": "", "tool_calls": [_call("c1")]},
                HumanMessage(content="B"), _reply("zz")]
    view = client._dialog_messages_for_provider(messages)
    assert view == [messages[0], messages[2]]


def test_a_complete_round_leaves_the_request_view_untouched():
    client = _client()
    messages = [HumanMessage(content="A"),
                {"role": "assistant", "content": "", "tool_calls": [_call("c1")]},
                _reply("c1"),
                {"role": "user", "content": [{"type": "text", "text": "tool image"}]},
                HumanMessage(content="B")]
    assert client._dialog_messages_for_provider(messages) is messages


async def test_the_interrupting_turn_never_sends_the_pending_round():
    """End to end: A is inside a slow tool when B arrives and requests."""
    release = asyncio.Event()

    async def slow_tool(call):
        await release.wait()
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client(handler=slow_tool)
    client.script = [[_text("我查一下"), _tool_calls("c1")],
                     [_text("好的"), _text("", "stop")]]
    turn_a = asyncio.create_task(client.stream_text("A"))
    for _ in range(50):
        if len(client.requests) == 1 and any(
            isinstance(m, dict) and m.get("tool_calls") for m in client._conversation_history
        ):
            break
        await asyncio.sleep(0)
    await client.handle_interruption()
    await client.stream_text("B")
    release.set()
    await turn_a
    b_request = client.requests[1]
    assert not [m for m in b_request if isinstance(m, dict) and m.get("tool_calls")]


async def test_a_kept_cancelled_round_holds_only_the_text_that_was_shown():
    """The pre-tool text sits unshown in the name-prefix buffer when the
    turn is cancelled inside the tool: the kept round must not carry it."""
    async def handler(call):
        await client.handle_interruption()
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client(handler=handler)
    client._prefix_buffer_size = 100
    client.script = [[_text("好的，我这就发"), _tool_calls("c1")]]
    await client.stream_text("帮我发消息")
    assert _emitted(client) == []
    assert _history_shape(client) == [
        ("human", "帮我发消息", None),
        ("assistant", "", ["c1"]),
        ("tool", "{}", None),
    ]


async def test_trimming_a_cancelled_round_never_touches_the_interrupters_round():
    """A is cancelled inside a slow tool; B runs its own tool round and
    finishes before A's handler returns. Trimming A's kept round must stop
    at B's user message and leave B's round alone."""
    release = asyncio.Event()

    async def handler(call):
        if call.call_id == "a1":
            await release.wait()
        return ToolResult(call_id=call.call_id, name=call.name, output={})

    client = _client(handler=handler)
    client._prefix_buffer_size = 100
    client.script = [
        [_text("A在查"), _tool_calls("a1")],
        [_text("B也在查"), _tool_calls("b1")],
        [_text("B查完了。"), _text("", "stop")],
    ]
    turn_a = asyncio.create_task(client.stream_text("A"))
    for _ in range(50):
        if any(isinstance(m, dict) and m.get("tool_calls") for m in client._conversation_history):
            break
        await asyncio.sleep(0)
    await client.handle_interruption()
    await client.stream_text("B")
    release.set()
    await turn_a
    rounds = {m["tool_calls"][0]["id"]: m for m in client._conversation_history
              if isinstance(m, dict) and m.get("tool_calls")}
    assert rounds["b1"]["content"] == "B也在查"
    assert rounds["a1"]["content"] == ""


async def test_a_tools_refusal_retry_cancelled_in_flight_still_publishes():
    """The retry after a tools refusal follows the first attempt's rule:
    the caller publishes on the first chunk, then checks cancellation."""
    client = _client(handler=_noop_tool)

    async def interrupt():
        await client.handle_interruption()

    client.script = [RuntimeError("this model does not support tools"),
                     [interrupt, _text("late"), _text("", "stop")]]
    await client.stream_text("look", turn_images=[_png_b64(4, 4, (7, 7, 7))])
    assert len(client.requests) == 2
    client._publish_provider_frames.assert_called_once()
    assert _emitted(client) == []


# ── Fourth review round ─────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,cap", [("openai", 2), ("openai", 0), ("gemini", 2)])
async def test_a_reasoning_only_start_cancelled_still_publishes_the_turn(provider, cap):
    """The stream opens with reasoning, which is never yielded; the user
    interrupts during it. The provider saw the turn's frames, so they are
    published all the same, and nothing is shown."""
    client = _client(provider, handler=_noop_tool, cap=cap)

    async def thinking(active):
        if active:
            await client.handle_interruption()

    client.on_thinking_active = thinking
    if provider == "gemini":
        thought = _GenaiPart(text="thinking")
        thought.thought = True
        client.script = [[_GenaiChunk([thought]), _GenaiChunk([_GenaiPart(text="late")])]]
    else:
        client.script = [[LLMStreamChunk(content="", reasoning_content="thinking"),
                          _text("late"), _text("", "stop")]]
    await client.stream_text("look", turn_images=[_png_b64(4, 4, (3, 3, 3))])
    client._publish_provider_frames.assert_called_once()
    assert _emitted(client) == []


async def test_an_interrupted_proactive_reply_reports_its_agent_callback_kind():
    """prompt_ephemeral's proactive completion is handle_proactive_complete,
    which closes with 'turn end agent_callback'; an interruption reports
    that kind so the core closes the turn the same way."""
    client = _client()
    seen = []

    async def interrupt():
        seen.append(await client.handle_interruption())

    client.script = [[_text("回调说到一半"), interrupt, _text("", "stop")]]
    await client.prompt_ephemeral("callback")
    assert seen == ["agent_callback"]
    client.on_proactive_done.assert_not_awaited()


async def test_a_raising_status_send_leaves_no_claimable_completion():
    """The completion-pending mark lives only for the cleanup await; if that
    await raises, the next interruption must not claim a phantom turn."""
    client = _client()
    client.on_status_message = AsyncMock(side_effect=RuntimeError("socket gone"))
    client.script = [[_text("", "stop")]]
    with pytest.raises(RuntimeError):
        await client.stream_text("hi")
    assert await client.handle_interruption() == ""


async def test_the_answered_chunk_of_a_cancelled_turn_sets_no_first_token_time(monkeypatch):
    """The empty chunk a cancelled tool loop hands up carries no output, so
    it must not be recorded as the turn's first token."""
    recorded = []
    monkeypatch.setattr(
        "utils.instrument.histogram", lambda name, value, *a, **k: recorded.append(name),
    )
    client = _client(handler=_noop_tool)

    async def thinking(active):
        if active:
            await client.handle_interruption()

    client.on_thinking_active = thinking
    client.script = [[LLMStreamChunk(content="", reasoning_content="thinking"),
                      _text("late"), _text("", "stop")]]
    await client.stream_text("hi")
    assert "llm_ttft_ms" not in recorded
    # A normal turn still records it.
    client.on_thinking_active = None
    client.script = [[_text("好"), _text("", "stop")]]
    client.requests.clear()
    await client.stream_text("again")
    assert "llm_ttft_ms" in recorded


# ── Fifth review round: who owns an interrupted turn ────────────────────────

@pytest.mark.parametrize("entry", ["stream_text", "response", "proactive"])
async def test_a_session_close_mid_reply_still_runs_the_completion(entry):
    """close() takes nothing over: nobody else closes that turn, so its
    completion runs (turn end, request id, TTS), as on main."""
    client = _client()

    async def close_mid_reply():
        client._cancel_response_generation()

    client.script = [[_text("说到一半"), close_mid_reply, _text("late"), _text("", "stop")]]
    if entry == "stream_text":
        await client.stream_text("Q1")
        client.on_response_done.assert_awaited_once()
    elif entry == "response":
        await client.prompt_ephemeral("avatar", completion_mode="response")
        client.on_response_done.assert_awaited_once()
    else:
        await client.prompt_ephemeral("callback")
        client.on_proactive_done.assert_awaited_once()
    assert _emitted(client) == ["说到一半"]


async def test_an_interrupter_takes_the_turn_over_and_leaves_no_record():
    client = _client()

    async def interrupt():
        assert await client.handle_interruption() == "response"

    client.script = [[_text("hi"), interrupt, _text("", "stop")]]
    await client.stream_text("Q1")
    client.on_response_done.assert_not_awaited()
    assert getattr(client, "_interrupter_owned_generations", set()) == set()


async def test_a_taken_over_turn_leaves_nothing_for_a_second_interruption():
    """Once an interrupter took the turn over, its cleanup await is no
    claim window: a second interruption meanwhile finds nothing to take, or
    it would close the turn again (and take the new turn's request id)."""
    client = _client()
    seen = []

    async def interrupt():
        seen.append(await client.handle_interruption())

    async def cleanup(_owner):
        seen.append(await client.handle_interruption())

    client._notify_reasoning_done = cleanup
    client.script = [[_text("说到一半"), interrupt, _text("late"), _text("", "stop")]]
    await client.prompt_ephemeral("avatar", completion_mode="response")
    assert seen == ["response", ""]
    client.on_response_done.assert_not_awaited()


async def test_the_completion_window_reads_as_busy():
    """Finished but not yet completed is not idle: a proactive start or an
    owed wrap-up must wait for that completion."""
    client = _client()
    seen = []

    async def cleanup(_owner):
        seen.append(client._is_responding)

    client._notify_reasoning_done = cleanup
    client.script = [[_text("说完了。"), _text("", "stop")]]
    await client.prompt_ephemeral("callback")
    assert seen == [True]
    assert client._is_responding is False
    client.on_proactive_done.assert_awaited_once()


async def test_the_completion_window_never_clobbers_a_newer_generation():
    client = _client()
    newer = []

    async def cleanup(_owner):
        await client.handle_interruption()
        newer.append(client._begin_response_generation())

    client._notify_reasoning_done = cleanup
    client.script = [[_text("说完了。"), _text("", "stop")]]
    await client.prompt_ephemeral("callback")
    assert client._active_response_generation == newer[0]
    assert client._is_responding is True
