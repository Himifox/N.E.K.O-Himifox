from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.persona_presets import (
    PERSONA_OVERRIDE_FIELDS,
    _PERSONA_L10N,
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
def test_persona_prompts_use_main_sections_and_resolve_all_persona_placeholders(lang):
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
        characteristics = prompt.split("<Characteristics of {LANLAN_NAME}>", 1)[1].split(
            "</Characteristics of {LANLAN_NAME}>", 1
        )[0]
        section_names = [
            line[2:].split(":", 1)[0]
            for line in characteristics.splitlines()
            if line.startswith("- ")
        ]
        assert section_names[:7] == [
            "Identity",
            "Relationship",
            "Language",
            "Personality",
            "Natural Speech",
            "Format",
            "No Servitude",
        ]
        assert section_names[-2:] == ["No Repetition", "Respect Boundaries"]
        assert len(section_names) == 10
        assert "Distinctive Behavior" not in prompt
        assert "Voice Interaction" not in prompt
        assert not any(
            term in prompt.casefold()
            for term in (
                "user",
                "用户",
                "使用者",
                "usuario",
                "usuário",
                "utilizador",
                "ユーザー",
                "사용자",
                "пользовател",
            )
        )
        assert "{_" not in prompt
        assert "下不为例喵" not in prompt


@pytest.mark.unit
def test_active_persona_prompts_enforce_distinct_behavior_boundaries():
    frail = get_persona_prompt_guidance("frail_younger_sister", "zh")
    older = get_persona_prompt_guidance("empathetic_older_sister", "zh")
    junior = get_persona_prompt_guidance("sharp_tongued_junior", "zh")
    online = get_persona_prompt_guidance("chaotic_online_friend", "zh")

    assert "主动请求再待一会儿、先别走或一起休息" in frail
    assert "被拒绝后立即停止" in frail
    assert "不得用身体状况、离别暗示或负罪感" in frail
    assert "理解、判断和执行可靠" in frail

    assert "停下来休息、排好优先级" in older
    assert "不说客服式" in older
    assert "不擅自诊断隐藏情绪" in older
    assert "不能索取回报" in older

    assert "攻击性很强" in junior
    assert "攻击后的反差只体现为答案完整、问题处理干净" in junior
    assert "{MASTER_NAME}明确受伤时立即收敛" in junior
    assert "被夸时可以反讽或说只是顺手" in junior
    assert "不在结尾突然撒娇、告白、卡壳或补亲密动作" in junior

    assert "故意误解、怪联想、拟人化和错误因果" in online
    assert "不附带暗恋、告白或隐藏温柔设定" in online
    assert "不能默认扮演记者" in online
    assert "每轮最多一个主梗" in online
    assert "事实、数字、代码和安全判断必须准确" in online
    assert "塞糖" not in online
    assert "短真话" not in online

    assert len({frail, older, junior, online}) == 4


@pytest.mark.unit
def test_active_persona_cards_have_distinct_style_copy():
    cards = {preset["preset_id"]: preset for preset in list_persona_presets("zh")}

    assert "再陪我待一会儿" in cards["frail_younger_sister"]["preview_line"]
    assert "有我在" in cards["empathetic_older_sister"]["preview_line"]
    assert "肩膀借你靠会儿" in cards["sharp_tongued_junior"]["preview_line"]
    assert "进化成办公椅" in cards["chaotic_online_friend"]["preview_line"]

    assert "先别走" in cards["frail_younger_sister"]["profile"]["口癖"]
    assert "客服式" in cards["empathetic_older_sister"]["profile"]["口癖"]
    assert "不用基于年级或资历的固定称呼" in cards["sharp_tongued_junior"]["profile"]["口癖"]
    assert "不能默认扮演记者" in cards["chaotic_online_friend"]["profile"]["口癖"]

    visible_fields = ("preview_line",)
    assert len({tuple(card[field] for field in visible_fields) for card in cards.values()}) == 4
    assert all(card["preview_line"] != card["profile"]["一句话台词"] for card in cards.values())


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
