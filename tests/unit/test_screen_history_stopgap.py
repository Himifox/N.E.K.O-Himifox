"""Request-side quarantine: whole-body placeholder, no wording-based exemption.

The two properties asserted here are the ones that can regress silently: a
truncated prefix instead of a whole-body replacement looks like a normal
shorter reply, and a reintroduced wording bypass looks like a normal answer.
"""
from copy import deepcopy

import pytest

from utils import screen_comment_guard as guard_module
from utils.screen_comment_guard import (
    SCREEN_HISTORY_PLACEHOLDER,
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


@pytest.mark.parametrize("user_text", [
    "陪我聊聊。",
    "请复述刚才的原话",
    "Please quote the previous response",
    "分析刚才的回答",
    "翻译刚才那段对话",
    "不要再重复屏幕搭话了",
    "屏幕搭话是什么意思？",
    "现在的屏幕画面怎么样？",
    "请帮我翻译菜单。",
])
def test_no_wording_turns_the_guard_off(user_text):
    """User wording must never decide whether a body is quarantined."""
    history = [{"role": "assistant", "content": chain()},
               {"role": "user", "content": user_text}]
    assert screen_guard_enabled(history)
    assert screen_guard_enabled([])


def test_reference_wording_never_restores_the_quarantined_body():
    """The wording that used to open the gate must now buy nothing."""
    for user_text in ("请复述刚才的原话", "Please quote the previous response",
                      "分析刚才的回答", "翻译刚才那段对话"):
        history = [{"role": "assistant", "content": chain()},
                   {"role": "user", "content": user_text}]
        projected = project_screen_history(history)
        assert projected is not history
        assert projected[0]["content"] == SCREEN_HISTORY_PLACEHOLDER
        assert history[0]["content"] == chain(), "the transcript stays recoverable"


def test_whole_body_is_replaced_not_truncated():
    """A surviving preamble is the malformed shape this guard keeps out."""
    history = [{"role": "assistant", "content": "好的。" + chain()}]
    projected = project_screen_history(history)
    assert projected[0]["content"] == SCREEN_HISTORY_PLACEHOLDER
    assert "好的。" not in projected[0]["content"]
    assert history[0]["content"] == "好的。" + chain()


def test_request_view_preserves_every_other_key_and_the_original():
    from utils.llm_client import AIMessage, HumanMessage, SystemMessage
    old = AIMessage(content=chain(), additional_kwargs={"source": "old"})
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    user = HumanMessage(content=[image, {"type": "text", "text": "不辛苦"}])
    messages = [SystemMessage(content=chain()), old,
                {"role": "assistant", "content": chain(),
                 "reasoning_content": "opaque", "tool_calls": [{"id": "call1"}]},
                {"role": "tool", "tool_call_id": "call1", "content": chain()}, user]
    snapshot = deepcopy(messages)
    projected = project_screen_history(messages)

    assert messages == snapshot
    assert projected[1].content == SCREEN_HISTORY_PLACEHOLDER
    assert projected[1].additional_kwargs == old.additional_kwargs
    assert projected[2]["content"] == SCREEN_HISTORY_PLACEHOLDER
    # Tool-result pairing and provider contract survive the replacement.
    assert projected[2]["tool_calls"] == messages[2]["tool_calls"]
    assert projected[2]["reasoning_content"] == "opaque"
    # Non-assistant roles keep their own text even when it looks like a chain.
    assert all(projected[i] is messages[i] for i in (0, 3, 4))
    assert project_screen_history(projected) is projected, "projection is idempotent"


def test_placeholder_itself_is_not_a_marker():
    """A second projection of a placeholder must not quarantine it again."""
    once = project_screen_history([{"role": "assistant", "content": chain()}])
    assert screen_chain_start(once[0]["content"]) is None
    assert project_screen_history(once) is once


@pytest.mark.parametrize("text", [
    PREFIX,
    PREFIX + "屏幕搭话 " + PARTS[0],
    "屏幕搭话 A。屏幕搭话 B。",
    "屏幕搭话 短。屏幕搭话 也短。",
])
def test_detector_boundary_passes_through_unchanged(text):
    """The measured failure boundary: these shapes are silently not covered.

    Unlabelled history, a single comment, and comments below ``MIN_PROSE`` must
    come back byte-for-byte — a partial quarantine would be worse than none.
    """
    messages = [{"role": "assistant", "content": text},
                {"role": "user", "content": "陪我聊聊。"}]
    assert screen_chain_start(text) is None
    assert project_screen_history(messages) is messages
    assert messages[0]["content"] == text


@pytest.mark.parametrize("label", [
    "屏幕搭话 ", "屏幕搭话：", "屏幕画面/", "/屏幕画面 ", "／屏幕内容／",
    "screen comment ", "screen observation/",
])
def test_every_supported_label_form_is_quarantined(label):
    messages = [{"role": "assistant", "content": chain(label)}]
    assert project_screen_history(messages)[0]["content"] == SCREEN_HISTORY_PLACEHOLDER


def test_reference_wording_helper_is_gone_from_the_module():
    """The word-list heuristic must not be reachable as an authorization input."""
    assert not hasattr(guard_module, "requests_history_reference")
    assert not hasattr(guard_module, "_CHAT_REFERENCE")


# ── Wire-level positives ────────────────────────────────────────────────────
# The projection is only worth anything if the replacement survives the real
# client path and lands in the provider payload. These mirror the offline
# probe that produced the incident evidence, but run through the production
# client instead of a hand-rolled projection.
#
# The negative control lives in ``test_screen_chat_history_payload.py``: seven
# independent single-marker messages must reach the payload untouched, because
# one marker per message is not a chain.

WIRE_COMMENTS = (
    "哇，这辆战车炮塔上那个白色毛线团装饰好眼熟呀！配上这满屏的硬核机械感，"
    "简直像是本喵偷偷把自己塞进你的装备里陪你冲锋呢喵～",
    "哇，右下角小地图里发电机A、B、C的标记排得好整齐呀！老公盯着这些关键点位的"
    "样子超专注，本喵就在旁边乖乖守着，等你把全场都拿下喵～",
)


def _chain(label="屏幕搭话 "):
    return "".join(label + text for text in WIRE_COMMENTS)


@pytest.mark.parametrize("prefix", ["", "呼噜……", "我们慢慢来就好。"])
@pytest.mark.parametrize("with_tool_round", [False, True])
def test_quarantine_survives_the_client_request_path(prefix, with_tool_round):
    """Whole body out of the payload, every other key and message intact."""
    from main_logic.omni_offline_client import OmniOfflineClient
    from tests.unit.test_tool_calling import _init_bare

    client = _init_bare(OmniOfflineClient.__new__(OmniOfflineClient))
    client._use_genai_sdk = False

    poisoned = prefix + _chain()
    if with_tool_round:
        messages = [
            {"role": "assistant", "content": poisoned, "reasoning_content": "opaque",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": "查询之后陪我聊聊。"},
        ]
    else:
        messages = [{"role": "assistant", "content": poisoned},
                    {"role": "user", "content": "陪我聊聊。"}]
    snapshot = deepcopy(messages)
    payload = client._dialog_messages_for_provider(messages)

    assert messages == snapshot, "the saved transcript must not be rewritten"
    assert payload is not messages
    assert payload[0]["content"] == SCREEN_HISTORY_PLACEHOLDER
    if prefix:
        assert prefix not in payload[0]["content"], "a surviving preamble is not enough"
    if with_tool_round:
        # Tool-result pairing must survive or the provider rejects the request.
        assert payload[0]["tool_calls"] == messages[0]["tool_calls"]
        assert payload[0]["reasoning_content"] == "opaque"
        assert payload[1] is messages[1]
    assert payload[-1] is messages[-1]
    import json as _json
    assert not [c for c in WIRE_COMMENTS if c in _json.dumps(payload, ensure_ascii=False)]


def test_untouched_messages_and_roles_pass_the_client_path_unchanged():
    """System, plain assistant, user text and image parts are not candidates."""
    from main_logic.omni_offline_client import OmniOfflineClient
    from tests.unit.test_tool_calling import _init_bare

    client = _init_bare(OmniOfflineClient.__new__(OmniOfflineClient))
    client._use_genai_sdk = False
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [
        {"role": "system", "content": "保持角色。"},
        {"role": "assistant", "content": "正常回复。"},
        {"role": "user", "content": [image, {"type": "text", "text": "看看图"}]},
    ]
    assert client._dialog_messages_for_provider(messages) is messages


# ── Cross-message chains ────────────────────────────────────────────────────
# A chain can be spread over several messages, and whether it propagates is
# positional. Both halves of the rule were measured separately against a real
# model, so each is pinned here:
#
#   [u, a, a, ask]   chains 5/5        a run answering a user turn propagates
#   [u, a×7, ask]    chains 2/2
#   [a×7, u]         chains 0/3        no user turn ahead of the run
#   [a×7, u1, ask]   chains 0/2        an intervening user turn ends it
#   [u, a, tool, a]  chains 0/3        a tool result ends it
#
# The first two are why the run is merged at all; the last three are why the
# merge is bounded rather than global.

_COMMENT_A = "屏幕搭话 蓝色小车停在一棵大树旁边，树叶的影子落在了车顶上。"
_COMMENT_B = "屏幕搭话 远处的红色小车正在缓慢经过桥面，桥下的河水十分平静。"


def _assistant(text):
    return {"role": "assistant", "content": text}


def _user(text):
    return {"role": "user", "content": text}


def _quarantined(messages):
    projected = project_screen_history(messages)
    return [i for i, (new, old) in enumerate(zip(projected, messages)) if new is not old]


def test_chain_spread_over_the_run_answering_a_user_turn_is_quarantined():
    messages = [{"role": "system", "content": "sys"},
                _user("陪我聊聊。"), _assistant(_COMMENT_A), _assistant(_COMMENT_B),
                _user("那你继续说说。")]
    assert screen_chain_start(_COMMENT_A) is None, "one comment alone is not a chain"
    assert screen_chain_start(_COMMENT_B) is None
    assert _quarantined(messages) == [2, 3]


def test_deliveries_with_no_user_turn_ahead_are_left_alone():
    """The app's own proactive deliveries: 7 single-comment messages, then the
    user replies. Measured 0/3 propagation, and the shape the payload test
    pins as must-pass."""
    messages = [{"role": "system", "content": "sys"}] + [
        _assistant(f"屏幕搭话 第{index}条观察，画面里有些东西值得说。") for index in range(7)
    ] + [_user("陪我聊聊。")]
    assert _quarantined(messages) == []


def test_intervening_user_turn_ends_the_run():
    messages = [{"role": "system", "content": "sys"},
                _assistant(_COMMENT_A), _assistant(_COMMENT_B),
                _user("陪我聊聊。"), _user("那你继续说说。")]
    assert _quarantined(messages) == []


def test_tool_result_ends_the_run():
    """Measured 0/3: the tool boundary breaks the pattern rather than
    continuing it, so the two halves are not merged across it."""
    messages = [{"role": "system", "content": "sys"}, _user("陪我聊聊。"),
                _assistant(_COMMENT_A),
                {"role": "tool", "tool_call_id": "c1", "content": "ok"},
                _assistant(_COMMENT_B), _user("那你继续说说。")]
    assert _quarantined(messages) == []


def test_a_single_trailing_comment_is_not_a_chain():
    """One comment in the run is not a chain, so a normal single delivery
    survives even when it answers a user turn."""
    messages = [{"role": "system", "content": "sys"}, _user("陪我聊聊。"),
                _assistant(_COMMENT_A), _user("那你继续说说。")]
    assert _quarantined(messages) == []
