"""Focused compatibility tests for Phase 2 dedup and data-level guards."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.proactive_chat import contracts, generation


class _FakeState:
    def __init__(self, *, preempted: bool = False) -> None:
        self.preempted = preempted

    def is_proactive_preempted(self, _speech_id: str | None = None) -> bool:
        return self.preempted


class _FakeManager:
    def __init__(self, *, preempted: bool = False) -> None:
        self.state = _FakeState(preempted=preempted)
        self.handle_new_message = AsyncMock()


class _NoRepeatCorpus:
    def score_draft(self, _name: str, _text: str) -> tuple[float, dict[str, float]]:
        return 0.0, {}


def _patch_state_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each test independent from process-global proactive history."""
    monkeypatch.setattr(
        generation,
        "_proactive_material_key",
        lambda _tag, _music, _meme: "material-key",
    )
    monkeypatch.setattr(
        generation,
        "_is_recent_proactive_material",
        lambda _name, _tag, _key: True,
    )
    monkeypatch.setattr(
        generation,
        "_is_similar_to_recent_proactive_chat",
        lambda _name, _text: (False, 0.0),
    )

    # The helper intentionally retains the legacy lazy import. Patch both the
    # source module and a possible direct import so the tests stay local.
    from memory import anti_repeat

    corpus = _NoRepeatCorpus()
    monkeypatch.setattr(anti_repeat, "get_anti_repeat_corpus", lambda: corpus)
    monkeypatch.setattr(
        generation,
        "get_anti_repeat_corpus",
        lambda: corpus,
        raising=False,
    )


async def _guard(
    mgr: _FakeManager,
    **overrides: object,
) -> generation.Phase2GuardedOutput:
    values: dict[str, object] = {
        "mgr": mgr,
        "proactive_sid": "proactive-sid",
        "lanlan_name": "兰兰",
        "response_text": "博士，今天也辛苦啦。",
        "full_text": "博士，今天也辛苦啦。",
        "source_tag": "CHAT",
        "active_channels": ["chat"],
        "selected_music_link": None,
        "selected_meme_link": None,
        "music_content": None,
        "meme_content": None,
        "is_playing_music": False,
        "music_cooldown": False,
        "expects_source_tag": True,
        "make_llm": AsyncMock(side_effect=AssertionError("unexpected regen")),
        "messages": [SimpleNamespace(content="system"), SimpleNamespace(content="human")],
        "human_text": "开始生成",
        "screenshot_b64": None,
        "phase2_use_vision": False,
        "phase2_disable_thinking": True,
        "proactive_lang": "zh",
        "master_name": "博士",
    }
    values.update(overrides)
    return await generation._guard_phase2_output(**values)


@pytest.mark.asyncio
async def test_literal_duplicate_returns_pass_and_cleans_proactive_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state_helpers(monkeypatch)
    monkeypatch.setattr(
        generation,
        "_is_similar_to_recent_proactive_chat",
        lambda _name, _text: (True, 0.92),
    )
    mgr = _FakeManager()

    guarded = await _guard(mgr)

    assert guarded.result is not None
    assert guarded.result.body["action"] == "pass"
    assert (
        guarded.result.body["reason_code"]
        == contracts.PROACTIVE_REASON_PASS_DUPLICATE
    )
    assert guarded.result.body["similarity"] == pytest.approx(0.92)
    mgr.handle_new_message.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_literal_duplicate_after_user_takeover_does_not_clear_reply_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state_helpers(monkeypatch)
    monkeypatch.setattr(
        generation,
        "_is_similar_to_recent_proactive_chat",
        lambda _name, _text: (True, 0.92),
    )
    mgr = _FakeManager(preempted=True)

    guarded = await _guard(mgr)

    assert guarded.result is not None
    assert (
        guarded.result.body["reason_code"]
        == contracts.PROACTIVE_REASON_PASS_DUPLICATE
    )
    mgr.handle_new_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_music_cooldown_downgrades_music_output_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state_helpers(monkeypatch)
    mgr = _FakeManager()
    music_link = {"title": "Night Walk", "artist": "Neko"}

    guarded = await _guard(
        mgr,
        source_tag="MUSIC",
        active_channels=["music"],
        selected_music_link=music_link,
        music_content={"data": [music_link]},
        music_cooldown=True,
    )

    assert guarded.result is None
    assert guarded.source_tag == "CHAT"
    assert guarded.full_text == "博士，今天也辛苦啦。"
    assert guarded.response_text == "博士，今天也辛苦啦。"
    assert guarded.selected_music_link == music_link
    assert guarded.music_content is None
    assert guarded.is_music_used is False
    mgr.handle_new_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_playing_music_blocks_new_music_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state_helpers(monkeypatch)
    mgr = _FakeManager()

    guarded = await _guard(
        mgr,
        source_tag="MUSIC",
        active_channels=["music"],
        music_content={"data": [{"title": "Another Song"}]},
        is_playing_music=True,
    )

    assert guarded.result is not None
    assert (
        guarded.result.body["reason_code"]
        == contracts.PROACTIVE_REASON_PASS_MODEL_PASS
    )
    assert guarded.source_tag == "PASS"
    assert guarded.music_content is None
    assert guarded.is_music_used is False
    mgr.handle_new_message.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_tagless_nonempty_output_falls_back_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state_helpers(monkeypatch)
    mgr = _FakeManager()

    guarded = await _guard(
        mgr,
        source_tag="",
        expects_source_tag=False,
    )

    assert guarded.result is None
    assert guarded.source_tag == "CHAT"
    assert guarded.full_text == "博士，今天也辛苦啦。"
    assert guarded.response_text == "博士，今天也辛苦啦。"
    assert guarded.is_music_used is False
    mgr.handle_new_message.assert_not_awaited()
