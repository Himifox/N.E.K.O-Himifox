from __future__ import annotations

from knowledge.moegirl_knowledge import MoegirlKnowledgeEntry, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.turn_context import build_meme_turn_context


def test_turn_context_matches_a_meme_title_inside_ordinary_conversation(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:test", title="treetree", content="meaning content", summary="a speech-based meme",
        source_url="https://example.test/chime", source_license="MIT", tags=("source:chime",),
    ))

    context = build_meme_turn_context("I keep hearing treetree today", database_path)

    assert context.hit_count == 1
    assert "Term: treetree" in context.text
    assert "Meaning: a speech-based meme" in context.text
    assert "TURN-LOCAL REFERENCE" in context.text
    assert "acknowledge its figurative meme meaning" in context.text
    assert "never mention a meme, its usage, searching" in context.text


def test_turn_context_stays_empty_when_no_meme_title_is_mentioned(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:test", title="treetree", content="meaning content",
        source_url="https://example.test/chime", source_license="MIT", tags=("source:chime",),
    ))

    context = build_meme_turn_context("I am discussing ordinary weather", database_path)

    assert context.hit_count == 0
    assert context.text == ""


def test_turn_context_matches_a_pronoun_and_filler_variant_from_an_internal_alias(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:cpu", title="他在 CPU 你", aliases=("人在cpu人",),
        content="meaning content", summary="being manipulated through language",
        source_url="https://example.test/chime", source_license="MIT", tags=("source:chime",),
    ))

    context = build_meme_turn_context("他这是在 CPU 我吧？", database_path)

    assert context.hit_count == 1
    assert "Term: 他在 CPU 你" in context.text


def test_short_common_title_does_not_inject_context_into_ordinary_chat(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:weather", title="天气", content="not relevant to ordinary weather",
        source_url="https://example.test/chime", source_license="MIT", tags=("source:chime",),
    ))

    context = build_meme_turn_context("今天天气真好", database_path)

    assert context.hit_count == 0
    assert context.text == ""


def test_short_title_is_left_to_the_model_tool_decision(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:short", title="急了", content="meme meaning", summary="emotional reaction",
        source_url="https://example.test/chime", source_license="MIT", tags=("source:chime",),
    ))

    context = build_meme_turn_context("急了是什么意思？", database_path)

    assert context.hit_count == 0
    assert context.text == ""


def test_turn_context_scans_all_aliases_and_refreshes_after_a_background_write(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        id="chime:one", title="first entry", aliases=("first alias",),
        content="first meaning", source_url="https://example.test/one", source_license="MIT",
    ))

    assert build_meme_turn_context("ordinary first alias wording", database_path).hit_count == 1

    store.upsert(MoegirlKnowledgeEntry(
        id="chime:second", title="second entry", aliases=("second alias",),
        content="second meaning", source_url="https://example.test/two", source_license="MIT",
    ))

    refreshed = build_meme_turn_context("ordinary second alias wording", database_path)

    assert refreshed.hit_count == 1
    assert "Term: second entry" in refreshed.text
