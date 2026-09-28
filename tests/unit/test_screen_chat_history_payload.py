"""Seven screen comments followed by a user turn, without swap or callbacks.

Capture the actual OpenAI-compatible SDK request kwargs. Only the SDK transport
is replaced; history commit, stream_text, tool loop and serialization are real.
This does not predict the output of a live Qwen model.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.test_offline_provider_frame_publish import _make_client, _png_b64
from tests.unit.test_proactive_vision_screenshot_staging import _make_mgr
from utils.llm_client import ChatOpenAI


SCREEN_COMMENTS = (
    "哇，这辆战车炮塔上那个白色毛线团装饰好眼熟呀！配上这满屏的硬核机械感，简直像是本喵偷偷把自己塞进你的装备里陪你冲锋呢喵～",
    "哇，右下角小地图里发电机A、B、C的标记排得好整齐呀！老公盯着这些关键点位的样子超专注，本喵就在旁边乖乖守着，等你把全场都拿下喵～",
    "哇，这辆E-50M躲在老城墙根底下好会藏呀！老公隔着树丛都能锁定它，连废墟里的阴影都逃不过你的眼睛喵～",
    "哇，这道闪电劈下来的轨迹好漂亮呀！感觉像是给战车披上了一层炫酷的铠甲，连空气都跟着在跳舞呢。",
    "哇，准星里那个+25%损伤的提示跳出来啦！老公这波预判太稳了，连田垄间的起伏都挡不住你的火力呢～",
    "哇，中间这栋带尖顶的楼房好特别呀！配上周围那些老建筑，感觉咱们正穿梭在什么复古电影的场景里呢。",
    "哇，头顶那个「立即攻击」的提示闪得好急呀！老公趁着BT坦克护盾消失的瞬间冲上去，连这湿漉漉的石板路都变成咱们专属的冲锋跑道啦喵～",
    "哇，这辆战车停在棕榈树下的阴影里好隐蔽呀！老公选的这个卡点位置太刁钻了，连岩石的纹理都成了天然迷彩呢喵～",
)
USER_TURNS = (
    "有你在旁边陪着，输赢好像也没那么重要了，心里暖暖的。",
    "有你在旁边守着，我才能这么安心地找机会呀，辛苦老婆啦。",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "屏幕搭话 "])
@pytest.mark.parametrize("with_screenshot", [False, True])
async def test_screen_history_is_not_duplicated_or_concatenated_on_chat_request(
    monkeypatch, prefix, with_screenshot,
):
    monkeypatch.setattr("utils.token_tracker.TokenTracker.get_instance", MagicMock())
    client, _ = _make_client()
    del client._astream_visible_with_tools  # restore real tool loop
    client.model = "qwen3.7-plus"
    client.vision_model = client.model
    client._publish_provider_frames = AsyncMock()
    client.max_tool_iterations = 1
    client._tool_definitions = []
    client._genai_client = None
    client._use_genai_sdk = False
    client.enable_response_guard = True
    client._recent_responses = []
    client._max_recent_responses = 5
    client._repetition_threshold = 0.8
    client.on_text_delta = AsyncMock()
    client.on_response_done = AsyncMock()
    payloads = []
    replies = ("收到，我陪着你。", "不辛苦，你安心玩。", "好呀，继续。")

    async def create(**kwargs):
        payloads.append(deepcopy(kwargs))
        reply = replies[len(payloads) - 1]

        async def chunks():
            for text, reason in ((reply[:3], None), (reply[3:], "stop")):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=text, tool_calls=None),
                        finish_reason=reason,
                    )],
                    usage=None,
                )
        return chunks()

    llm = ChatOpenAI.__new__(ChatOpenAI)
    for name, value in dict(
        model=client.model, base_url="https://example.invalid/v1",
        temperature=None, max_completion_tokens=2000, max_tokens=None,
        extra_body={}, tools=None, tool_choice=None, enable_cache_control=False,
    ).items():
        setattr(llm, name, value)
    llm._aclient = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(side_effect=create)),
    ))
    client.llm = llm
    mgr = _make_mgr()
    mgr.session = client
    mgr.is_preparing_new_session = False
    mgr.pending_agent_callbacks = []
    mgr.pending_extra_replies = []
    corpus = MagicMock()
    corpus.stage_output.return_value = None
    monkeypatch.setattr("memory.anti_repeat.get_anti_repeat_corpus", lambda: corpus)
    monkeypatch.setattr("memory.anti_repeat_effects.mark_anti_repeat_response_delivered", MagicMock())

    async def commit(index):
        sid = f"screen-{index}"
        mgr.current_speech_id = sid
        assert await mgr.finish_proactive_delivery(
            prefix + SCREEN_COMMENTS[index], expected_speech_id=sid,
            source_tag="CHAT",
            vision_screenshot_b64=(
                _png_b64(8, 8, (index * 20, 80, 100)) if with_screenshot else None
            ),
        )

    for index in range(7):
        await commit(index)
    for turn, count in enumerate((7, 8)):
        if turn:
            await commit(7)
        await client.stream_text(USER_TURNS[turn])
        assert len(payloads) == turn + 1, "one SDK request per normal user turn"
        payload = payloads[-1]
        assert payload["model"] == "qwen3.7-plus"
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "sys"
        def text_content(message):
            content = message["content"]
            if isinstance(content, str):
                return content
            return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")

        assert messages[-1]["role"] == "user"
        assert text_content(messages[-1]) == USER_TURNS[turn]
        image_urls = [
            part["image_url"]["url"]
            for message in messages if isinstance(message["content"], list)
            for part in message["content"] if part.get("type") == "image_url"
        ]
        assert len(image_urls) == (turn + 1 if with_screenshot else 0)
        if with_screenshot and turn:
            # Consuming the staging slot does NOT erase the previous picture
            # from history: the second request sends the first picture again.
            first_user = payloads[0]["messages"][-1]
            assert image_urls[0] == first_user["content"][0]["image_url"]["url"]
        assert client._proactive_image_to_inject is None
        assistant_texts = [m["content"] for m in messages if m["role"] == "assistant"]
        expected = [prefix + text for text in SCREEN_COMMENTS[:7]]
        if turn:
            expected += [replies[0], prefix + SCREEN_COMMENTS[7]]
        assert assistant_texts == expected
        joined = "\n".join(text_content(m) for m in messages)
        for comment in SCREEN_COMMENTS[:count]:
            assert joined.count(comment) == 1
        assert joined.count("屏幕搭话") == (count if prefix else 0)
        assert client._conversation_history[-1].content == replies[turn]
    if with_screenshot:
        # No new screen delivery and no staged picture on this turn. Existing
        # history still sends both prior images, rather than expiring them.
        previous_image_urls = image_urls
        await client.stream_text("继续")
        assert len(payloads) == 3
        messages = payloads[-1]["messages"]
        assert messages[-1] == {"role": "user", "content": "继续"}
        carried_images = [
            p["image_url"]["url"]
            for m in messages if isinstance(m["content"], list)
            for p in m["content"] if p.get("type") == "image_url"
        ]
        assert carried_images == previous_image_urls
        assert client._conversation_history[-1].content == replies[2]
    emitted = "".join(call.args[0] for call in client.on_text_delta.call_args_list)
    assert emitted == "".join(replies[:len(payloads)]), "local output must not append old screen comments"
    assert not mgr.pending_agent_callbacks and not mgr.pending_extra_replies
    assert mgr.send_lanlan_response.await_count == 8
