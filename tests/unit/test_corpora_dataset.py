from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import main_logic.moegirl_knowledge_tool as knowledge_tool
from knowledge import open_knowledge
from knowledge.api import KnowledgeEntry, KnowledgeStore
from knowledge.corpora_dataset import (
    CORPORA_COMMIT,
    CORPORA_ENTRY_COUNT,
    CORPORA_LICENSE,
    CORPORA_SHA256,
    load_bundled_corpora_dataset,
)
from knowledge.corpora_runtime import _import_bundled_corpora


def test_bundled_corpora_asset_is_small_valid_and_pinned():
    dataset = load_bundled_corpora_dataset()

    assert len(dataset.entries) == CORPORA_ENTRY_COUNT == 229
    assert dataset.commit == CORPORA_COMMIT
    assert dataset.sha256 == CORPORA_SHA256
    assert CORPORA_LICENSE == "CC0 1.0"
    assert {entry.source_tag for entry in dataset.entries} == {"source:corpora"}
    assert {
        tag
        for entry in dataset.entries
        for tag in entry.tags
        if tag.startswith("category:")
    } == {
        "category:animals",
        "category:colors",
        "category:divination",
        "category:film-tv",
        "category:foods",
        "category:humans",
        "category:mythology",
        "category:psychology",
    }


def test_bundled_corpora_asset_is_one_object_per_line():
    from importlib.resources import files

    lines = (
        files("knowledge.data")
        .joinpath("corpora_demo.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    assert len(lines) == CORPORA_ENTRY_COUNT
    assert all(line and isinstance(json.loads(line), dict) for line in lines)


@pytest.mark.asyncio
async def test_corpora_collection_import_search_management_and_isolation(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "corpora" / "knowledge.db"
    state_path = tmp_path / "corpora" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json", encoding="utf-8")

    await _import_bundled_corpora(
        database_path,
        state_path,
        logging.getLogger("test.corpora"),
    )
    service = open_knowledge(tmp_path)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["entries"] == CORPORA_ENTRY_COUNT
    assert service.get_status("corpora") == {
        "collection_id": "corpora",
        "entries": CORPORA_ENTRY_COUNT,
        "integrity_ok": True,
    }
    assert service.count_entries("corpora", source_tag="source:corpora") == 229
    assert service.search("corpora", "Aphrodite", limit=1)[0].entry.title == "Aphrodite"
    assert service.search("corpora", "The Godfather", limit=1)[0].entry.title == (
        "The Godfather (1972)"
    )
    assert service.search("corpora", "#0000FF", limit=1)[0].entry.title == "Blue"
    assert service.get_entry(
        "corpora",
        source_tag="source:corpora",
        title="Aphrodite",
    ) is not None
    assert service.list_entries("corpora", limit=5, offset=0)
    assert service.set_entry_disabled(
        "corpora",
        source_tag="source:corpora",
        title="Aphrodite",
        disabled=True,
    ) == 1
    assert service.search("corpora", "Aphrodite", limit=1) == []
    assert service.set_entry_disabled(
        "corpora",
        source_tag="source:corpora",
        title="Aphrodite",
        disabled=False,
    ) == 0

    # Concrete reference entries use the existing cross-collection turn-card
    # route, while common list material remains explicit/tool-only.
    context = service.build_turn_context("Aphrodite sounds like a mythic name")
    assert context.hit_count == 1
    assert context.collection_id == "corpora"
    assert "EPHEMERAL PUBLIC KNOWLEDGE RESPONSE TASK" in context.text
    assert "Category: mythology" in context.text
    assert "Meme type" not in context.text
    assert service.build_turn_context("anankeplaceholder").hit_count == 0
    assert service.build_turn_context("Ananke is a mythic name").collection_id == "corpora"

    moon = service.build_turn_context("I drew The Moon today")
    assert moon.hit_count == 1
    assert moon.collection_id == "corpora"
    assert "Light meanings:" in moon.text
    assert service.build_turn_context("my cat is calm").hit_count == 0

    tarot_request = service.build_conversation_context("给我抽一张塔罗牌")
    assert tarot_request.hit_count == 1
    assert tarot_request.collection_id == "corpora"
    assert tarot_request.match_mode == "material_sample"
    assert "selected from local material" in tarot_request.text
    assert "Category: divination" in tarot_request.text

    occupation_request = service.build_conversation_context(
        "给我一个适合酒馆老板的NPC职业"
    )
    assert occupation_request.match_mode == "material_sample"
    assert "Category: humans" in occupation_request.text
    assert service.build_conversation_context("我们在讨论职业教育").hit_count == 0
    assert service.build_conversation_context("这个塔罗体系很抽象").hit_count == 0

    direct_beats_sampling = service.build_conversation_context(
        "给我讲讲The Moon这张塔罗牌"
    )
    assert direct_beats_sampling.match_mode == "strong"

    sampled = service.sample_entries(
        "corpora",
        "dataset:occupations",
        limit=2,
    )
    assert len(sampled) == 2
    assert all("dataset:occupations" in entry.tags for entry in sampled)
    with pytest.raises(ValueError, match="sample tag is not enabled"):
        service.sample_entries("corpora", "category:humans", limit=1)

    monkeypatch.setattr(
        knowledge_tool,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=tmp_path),
    )
    lookup = await knowledge_tool.handle_public_knowledge_call(
        {
            "query": "The Moon",
            "collection": "corpora",
            "mode": "lookup",
            "limit": 1,
        },
        language="en",
    )
    assert "[corpora] The Moon" in lookup
    assert "Reference details: Keywords:" in lookup
    assert "Source: Darius Kazemi's Corpora | license: CC0 1.0" in lookup

    sample = await knowledge_tool.handle_public_knowledge_call(
        {
            "query": "dataset:occupations",
            "collection": "corpora",
            "mode": "sample",
            "limit": 1,
        },
        language="en",
    )
    assert "[corpora]" in sample
    assert "Category: humans" in sample

    assert service.count_entries("meme") == 0


@pytest.mark.asyncio
async def test_builtin_collections_disambiguate_an_equal_title_by_context(tmp_path):
    corpora_path = tmp_path / "corpora" / "knowledge.db"
    await _import_bundled_corpora(
        corpora_path,
        tmp_path / "corpora" / "state.json",
        logging.getLogger("test.corpora.conflict"),
    )
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path("meme")).upsert(KnowledgeEntry(
        title="The Moon",
        terms={},
        tags=("source:chime", "type:reference"),
        summary="A fictional moon-cookie meme.",
        content="Meaning\n- The moon is jokingly described as a cookie.",
    ))

    assert service.build_turn_context(
        "我抽到了 The Moon，这张牌是什么意思？"
    ).collection_id == "corpora"
    assert service.build_turn_context("I drew The Moon today").collection_id == "corpora"
    assert service.build_turn_context("The Moon 是什么梗？").collection_id == "meme"
    assert service.build_turn_context("The Moon").collection_id == "meme"
