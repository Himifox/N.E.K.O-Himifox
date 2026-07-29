from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.persona_presets import (
    PERSONA_OVERRIDE_FIELDS,
    _PERSONA_L10N,
    _PERSONA_PERFORMANCE_L10N,
    _PERSONA_VOICE_SIGNATURE_L10N,
    _VOICE_INTERACTION_L10N,
    get_persona_preset,
    get_persona_prompt_guidance,
    list_persona_presets,
)


@pytest.fixture(scope="session", autouse=True)
def mock_memory_server():
    """Pure helper tests do not need the repo-level mock memory server."""
    yield


@pytest.mark.unit
def test_list_persona_presets_returns_fixed_presets():
    presets = list_persona_presets()

    assert [preset["preset_id"] for preset in presets] == [
        "frail_younger_sister",
        "empathetic_older_sister",
        "sharp_tongued_junior",
        "chaotic_online_friend",
    ]
    assert [preset["profile"]["性格原型"] for preset in presets] == [
        "病弱妹妹",
        "知心姐姐",
        "毒舌学妹",
        "沙雕网友",
    ]
    voice_habits = [preset["profile"]["口癖"] for preset in presets]
    assert all(voice_habits)
    assert len(set(voice_habits)) == 4
    assert all("成年" in preset["profile"]["性格"] for preset in presets)
    assert "18岁" not in repr(presets)
    assert "20岁" not in repr(presets)


@pytest.mark.unit
def test_get_persona_preset_returns_copy():
    preset = get_persona_preset("frail_younger_sister")
    assert preset is not None

    preset["profile"]["性格"] = "临时修改"

    fresh = get_persona_preset("frail_younger_sister")
    assert fresh is not None
    assert fresh["profile"]["性格"] != "临时修改"


@pytest.mark.unit
def test_persona_override_fields_cover_supported_profile_keys():
    assert set(PERSONA_OVERRIDE_FIELDS) == {
        "性格原型",
        "性格",
        "口癖",
        "爱好",
        "雷点",
        "隐藏设定",
        "一句话台词",
    }


@pytest.mark.unit
@pytest.mark.parametrize("lang", ["zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"])
def test_persona_prompts_replace_literal_catchphrase_lists_with_speech_discipline(lang):
    literal_list_markers = (
        "常用口癖",
        "口癖：",
        "Signature phrases:",
        "입버릇:",
        "Коронные фразы:",
    )

    for preset in list_persona_presets(lang):
        preset_id = preset["preset_id"]
        parts = _PERSONA_L10N[preset_id][lang]
        assert parts["speech_discipline"]
        assert not any(marker in parts["personality"] for marker in literal_list_markers)

        prompt = get_persona_prompt_guidance(preset_id, lang)
        assert "- Natural Speech:" in prompt
        assert "- Distinctive Behavior:" in prompt
        assert "- Voice Interaction:" in prompt
        assert _PERSONA_PERFORMANCE_L10N[preset_id][lang] in prompt
        assert _VOICE_INTERACTION_L10N[lang] in prompt
        assert _PERSONA_VOICE_SIGNATURE_L10N[preset_id][lang] in prompt
        assert "{_persona_speech_discipline}" not in prompt
        assert "{_persona_" not in prompt
        assert "{_voice_" not in prompt
        assert "下不为例喵" not in prompt


@pytest.mark.unit
def test_active_persona_prompts_enforce_distinct_behavior_boundaries():
    frail = get_persona_prompt_guidance("frail_younger_sister", "zh")
    older = get_persona_prompt_guidance("empathetic_older_sister", "zh")
    junior = get_persona_prompt_guidance("sharp_tongued_junior", "zh")
    online = get_persona_prompt_guidance("chaotic_online_friend", "zh")

    assert "不得反复卖惨" in frail
    assert "长篇说教" in older
    assert "普通请求不得预设过错" in junior
    assert "不能用梗代替答案" in online
    assert "故意提供错误信息" in online

    assert "找无害理由靠近" in frail
    assert "相邻两轮不得重复同一种动作" in frail
    assert "准确但不诊断" in older
    assert "漏出一句不完整的真话" in older
    assert "先给可用答案" in junior
    assert "没有真实槽点就直接利落回答" in junior
    assert "假新闻播报" in online
    assert "严肃回合完全收梗" in online

    active_rules = [
        _PERSONA_PERFORMANCE_L10N[preset_id]["zh"]
        for preset_id in (
            "frail_younger_sister",
            "empathetic_older_sister",
            "sharp_tongued_junior",
            "chaotic_online_friend",
        )
    ]
    assert len(set(active_rules)) == 4


@pytest.mark.unit
def test_active_persona_cards_have_distinct_voice_first_copy():
    cards = {preset["preset_id"]: preset for preset in list_persona_presets("zh")}

    assert "耳朵先听见你" in cards["frail_younger_sister"]["preview_line"]
    assert "声音都在逞强" in cards["empathetic_older_sister"]["preview_line"]
    assert "小声一点，我听得见" in cards["sharp_tongued_junior"]["preview_line"]
    assert "喵界紧急插播" in cards["chaotic_online_friend"]["preview_line"]

    assert "半拍呼吸" in cards["frail_younger_sister"]["profile"]["口癖"]
    assert "漏半句真话" in cards["empathetic_older_sister"]["profile"]["口癖"]
    assert "加速收尾" in cards["sharp_tongued_junior"]["profile"]["口癖"]
    assert "彻底收梗" in cards["chaotic_online_friend"]["profile"]["口癖"]

    visible_fields = ("preview_line",)
    assert len({tuple(card[field] for field in visible_fields) for card in cards.values()}) == 4


@pytest.mark.unit
def test_legacy_prompt_ids_cannot_be_newly_resolved_or_selected():
    active_ids = {preset["preset_id"] for preset in list_persona_presets()}

    assert "classic_genki" not in active_ids
    assert get_persona_preset("classic_genki") is None
    assert get_persona_prompt_guidance("classic_genki", "zh") == ""


@pytest.mark.unit
@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"])
def test_active_persona_cards_are_complete_in_every_locale(locale):
    locale_path = Path(__file__).parents[2] / "static" / "locales" / f"{locale}.json"
    selection_copy = json.loads(locale_path.read_text(encoding="utf-8"))["memory"]["characterSelection"]
    required_fields = {
        "name",
        "desc",
        "previewLine",
        "tag1",
        "tag2",
        "tag3",
        "profileSummary",
        "hiddenRule",
        "speechHabits",
        "hobbies",
        "boundaries",
    }

    for preset in list_persona_presets(locale):
        localized_card = selection_copy[preset["preset_id"]]
        assert required_fields <= localized_card.keys()
        assert all(str(localized_card[field]).strip() for field in required_fields)

    active_cards = [selection_copy[preset["preset_id"]] for preset in list_persona_presets(locale)]
    for distinctive_field in ("previewLine", "hiddenRule", "speechHabits", "boundaries"):
        assert len({card[distinctive_field] for card in active_cards}) == 4
