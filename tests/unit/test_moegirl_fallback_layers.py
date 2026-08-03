from __future__ import annotations

from types import SimpleNamespace

import pytest

import main_logic.moegirl_knowledge_tool as knowledge_tool
from knowledge.moegirl_knowledge import MoegirlKnowledgeEntry, MoegirlKnowledgeStore


@pytest.mark.asyncio
async def test_local_miss_returns_immediately_without_source_queue(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "moegirl-knowledge").mkdir(parents=True)
    monkeypatch.setattr(
        knowledge_tool,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )

    result = await knowledge_tool.handle_moegirl_knowledge_call(
        {"query": "not in the local fixture"}, language="en"
    )

    assert result == "No relevant public knowledge is available locally."
    assert not hasattr(knowledge_tool, "enqueue_geng8_lookup")
    assert not hasattr(knowledge_tool, "MoegirlWikiApiSource")


@pytest.mark.asyncio
async def test_local_tool_result_includes_type_usage_and_source(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="quoted phrase",
        terms={"alias": (), "recognition": ("quote callback",)},
        tags=("source:chime", "type:引用"),
        summary="a playful quotation",
        content="Meaning: a playful quotation.\n\nExamples:\n- quoted phrase used as a callback",
    ))
    monkeypatch.setattr(
        knowledge_tool,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )

    result = await knowledge_tool.handle_moegirl_knowledge_call(
        {"query": "quoted phrase"}, language="en"
    )

    assert "Type: 引用" in result
    assert "Typical usage: quoted phrase used as a callback" in result
    assert "Source: CHIME | license: MIT" in result


@pytest.mark.asyncio
async def test_explicit_local_result_warns_when_usage_may_be_outdated(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    MoegirlKnowledgeStore(database_path).upsert(MoegirlKnowledgeEntry(
        title="水灵灵",
        terms={},
        tags=("source:chime", "type:现象", "quality:stale-usage"),
        summary="an older recorded usage",
        content="Meaning\n- an older example",
    ))
    monkeypatch.setattr(
        knowledge_tool,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )

    result = await knowledge_tool.handle_moegirl_knowledge_call(
        {"query": "水灵灵"}, language="en"
    )

    assert "水灵灵" in result
    assert "caution: usage may be outdated" in result


def test_normal_source_package_exports_only_local_importers():
    from knowledge.moegirl_knowledge import sources

    assert not hasattr(sources, "Geng8TagSource")
    assert not hasattr(sources, "MoegirlWikiApiSource")
