from __future__ import annotations

from knowledge.moegirl_knowledge import MoegirlKnowledgeEntry, MoegirlKnowledgeStore
from knowledge.engine.catalog_overrides import (
    get_catalog_override_path,
    set_entry_disabled,
)
from knowledge.moegirl_knowledge.turn_context import build_meme_turn_context


def test_turn_context_matches_a_meme_title_inside_ordinary_conversation(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="treetree", terms={}, tags=("source:chime",),
        content="meaning content", summary="a speech-based meme",
    ))

    context = build_meme_turn_context("I keep hearing treetree today", database_path)

    assert context.hit_count == 1
    assert "Term: treetree" in context.text
    assert "Meaning: a speech-based meme" in context.text
    assert "EPHEMERAL MEME RESPONSE TASK" in context.text
    assert "reply only to the preceding user message" in context.text
    assert "mention this task/search/source" in context.text
    assert context.text.index("Response goal:") < context.text.index("Term: treetree")


def test_turn_context_includes_source_usage_and_response_posture(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="quoted phrase", terms={}, content=(
            "Meaning: a playful quotation.\n\n"
            "Examples:\n- quoted phrase used as a light-hearted callback"
        ),
        summary="a playful quotation", tags=("source:chime", "type:引用"),
    ))

    context = build_meme_turn_context("That quoted phrase again", database_path)

    assert "Meaning: a playful quotation" in context.text
    assert "Meme type: 引用" in context.text
    assert "Typical usage: quoted phrase used as a light-hearted callback" in context.text
    assert "Recognize it as a quote or adaptation" in context.text
    assert "default to comfort/advice" in context.text


def test_turn_context_without_chime_examples_degrades_to_meaning_only(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="plain reference", terms={}, content="A sourced explanation.",
        summary="A sourced explanation.", tags=("source:moegirl",),
    ))

    context = build_meme_turn_context("A plain reference appears here", database_path)

    assert "Meaning: A sourced explanation." in context.text
    assert "Typical usage:" not in context.text


def test_turn_context_stays_empty_when_no_meme_title_is_mentioned(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="treetree", terms={}, content="meaning content", summary="",
        tags=("source:chime",),
    ))

    context = build_meme_turn_context("I am discussing ordinary weather", database_path)

    assert context.hit_count == 0
    assert context.text == ""


def test_turn_context_matches_a_pronoun_and_filler_variant_from_an_internal_alias(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="他在 CPU 你", terms={"alias": ("人在cpu人",), "recognition": ()},
        content="meaning content", summary="being manipulated through language",
        tags=("source:chime",),
    ))

    context = build_meme_turn_context("他这是在 CPU 我吧？", database_path)

    assert context.hit_count == 1
    assert "Term: 他在 CPU 你" in context.text


def test_short_common_title_does_not_inject_context_into_ordinary_chat(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="天气", terms={}, content="not relevant to ordinary weather", summary="",
        tags=("source:chime",),
    ))

    context = build_meme_turn_context("今天天气真好", database_path)

    assert context.hit_count == 0
    assert context.text == ""


def test_short_title_is_left_to_the_model_tool_decision(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="急了", terms={}, content="meme meaning", summary="emotional reaction",
        tags=("source:chime",),
    ))

    context = build_meme_turn_context("急了是什么意思？", database_path)

    assert context.hit_count == 0
    assert context.text == ""


def test_two_character_verified_recognition_can_trigger_turn_context(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="夺笋", terms={"alias": (), "recognition": ("夺笋",)},
        content="形容说话或做法很损。", summary="形容做法很损。",
        tags=("source:chime", "type:谐音"),
    ))

    context = build_meme_turn_context("你这发言也太夺笋了", database_path)

    assert context.hit_count == 1
    assert "Term: 夺笋" in context.text


def test_turn_context_scans_all_aliases_and_refreshes_after_a_local_write(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="first entry", terms={"alias": ("first alias",), "recognition": ()},
        content="first meaning", summary="", tags=("source:chime",),
    ))

    assert build_meme_turn_context("ordinary first alias wording", database_path).hit_count == 1

    store.upsert(MoegirlKnowledgeEntry(
        title="second entry", terms={"alias": ("second alias",), "recognition": ()},
        content="second meaning", summary="", tags=("source:chime",),
    ))

    refreshed = build_meme_turn_context("ordinary second alias wording", database_path)

    assert refreshed.hit_count == 1
    assert "Term: second entry" in refreshed.text


def _weak_chime_entry(title: str, *, terms=None, tags=None, content=None):
    return MoegirlKnowledgeEntry(
        title=title,
        terms=terms or {"alias": (), "recognition": ()},
        tags=tags or ("source:chime", "type:现象"),
        summary="a non-literal internet usage",
        content=content or "Meaning\n- a typical non-literal example",
    )


def test_two_character_chime_title_with_type_and_example_is_a_weak_hint(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_weak_chime_entry("上头"))

    context = build_meme_turn_context("最近做这个方案越改越上头", database_path)

    assert context.hit_count == 1
    assert context.match_mode == "weak_short"
    assert "EPHEMERAL POSSIBLE SHORT MEME TASK" in context.text
    assert "only possibly uses" in context.text
    assert "medical, safety-related, financial" in context.text
    assert "safety takes priority" in context.text


def test_another_eligible_two_character_chime_title_is_a_weak_hint(tmp_path):
    database_path = tmp_path / "knowledge.db"
    MoegirlKnowledgeStore(database_path).upsert(_weak_chime_entry("内卷"))

    context = build_meme_turn_context("大家把日报写成论文，太内卷了", database_path)

    assert context.hit_count == 1
    assert context.match_mode == "weak_short"
    assert "Term: 内卷" in context.text


def test_two_character_recognition_remains_a_strong_match(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="夺笋",
        terms={"alias": (), "recognition": ("夺笋",)},
        tags=("source:chime", "type:谐音"),
        summary="a playful way to say something is mean",
        content="Meaning\n- 你这发言太夺笋了",
    ))

    context = build_meme_turn_context("你这发言多少有点夺笋", database_path)

    assert context.hit_count == 1
    assert context.match_mode == "strong"
    assert "confirmed to use the non-literal sense" in context.text


def test_strong_match_wins_over_an_earlier_weak_short_term(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_weak_chime_entry("上头"))
    store.upsert(MoegirlKnowledgeEntry(
        title="电子榨菜",
        terms={},
        tags=("source:chime", "type:现象"),
        summary="content watched while eating",
        content="Meaning\n- 吃饭时看电子榨菜",
    ))

    context = build_meme_turn_context("上头了，还是先看点电子榨菜吧", database_path)

    assert context.match_mode == "strong"
    assert "Term: 电子榨菜" in context.text
    assert "Term: 上头" not in context.text


def test_disabled_two_character_entry_does_not_create_a_weak_hint(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_weak_chime_entry("内卷"))
    set_entry_disabled(
        get_catalog_override_path(database_path),
        source_tag="source:chime",
        title="内卷",
        disabled=True,
    )

    context = build_meme_turn_context("这也太内卷了", database_path)

    assert context == type(context)()


def test_weak_short_hint_requires_chime_type_and_example(tmp_path):
    cases = (
        _weak_chime_entry("无型", tags=("source:chime",)),
        _weak_chime_entry("无例", content="Meaning without an example list"),
        _weak_chime_entry("他源", tags=("source:geng-guide", "type:现象")),
    )
    for index, entry in enumerate(cases):
        database_path = tmp_path / str(index) / "knowledge.db"
        MoegirlKnowledgeStore(database_path).upsert(entry)
        assert build_meme_turn_context(f"这里出现{entry.title}二字", database_path).hit_count == 0


def test_stale_usage_entry_is_excluded_from_automatic_context(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_weak_chime_entry(
        "水灵灵",
        tags=("source:chime", "type:现象", "quality:stale-usage"),
    ))

    context = build_meme_turn_context("她就这么水灵灵地把 bug 带上线了", database_path)

    assert context.hit_count == 0
    assert context.match_mode == "none"


def test_response_task_forbids_mechanical_repetition(tmp_path):
    database_path = tmp_path / "knowledge.db"
    MoegirlKnowledgeStore(database_path).upsert(_weak_chime_entry("上头"))

    context = build_meme_turn_context("这个项目越改越上头", database_path)

    assert "Do not merely echo the wording" in context.text
    assert "relevant reaction or stance" in context.text
    assert "asks for meaning or a distinction" in context.text
    assert context.text.index("Response goal:") < context.text.index("Term: 上头")
