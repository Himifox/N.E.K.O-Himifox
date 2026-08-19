from __future__ import annotations

from types import SimpleNamespace

import pytest

import main_logic.moegirl_knowledge_tool as knowledge_tool
from knowledge.moegirl_knowledge import MoegirlKnowledgeEntry, MoegirlKnowledgeStore
from knowledge.packs import validate_pack


@pytest.mark.asyncio
async def test_local_miss_returns_immediately_without_source_queue(
    monkeypatch, tmp_path
):
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
    store.upsert(
        MoegirlKnowledgeEntry(
            title="quoted phrase",
            terms={"alias": (), "recognition": ("quote callback",)},
            tags=("source:chime", "type:引用"),
            summary="a playful quotation",
            content="Meaning: a playful quotation.\n\nExamples:\n- quoted phrase used as a callback",
        )
    )
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
async def test_explicit_local_result_warns_when_usage_may_be_outdated(
    monkeypatch, tmp_path
):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    MoegirlKnowledgeStore(database_path).upsert(
        MoegirlKnowledgeEntry(
            title="水灵灵",
            terms={},
            tags=("source:chime", "type:现象", "quality:stale-usage"),
            summary="an older recorded usage",
            content="Meaning\n- an older example",
        )
    )
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


@pytest.mark.asyncio
async def test_corpora_lookup_exposes_unlabelled_reference_content(
    monkeypatch, tmp_path
):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "corpora" / "knowledge.db"
    from knowledge.service import KnowledgeService

    store = MoegirlKnowledgeStore(database_path)
    store.upsert(
        MoegirlKnowledgeEntry(
            title="这个梗也太老了吧",
            terms={"alias": (), "recognition": ("这个梗也太老了吧",)},
            tags=("source:corpora", "dataset:dialogue-samples"),
            summary="这是对话样例，不是事实来源。",
            content="用户输入：这个梗也太老了吧\n参考回复：确实，这梗都快成 internet fossil 了。",
        )
    )
    monkeypatch.setattr(
        knowledge_tool,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )
    monkeypatch.setattr(
        knowledge_tool,
        "open_knowledge",
        lambda _root: KnowledgeService(
            knowledge_root,
            database_paths={"corpora": database_path},
        ),
    )

    result = await knowledge_tool.handle_public_knowledge_call(
        {"collection": "corpora", "query": "这个梗也太老了吧"},
        language="zh",
    )

    assert "参考回复" in result
    assert "internet fossil" in result


@pytest.mark.asyncio
async def test_reply_intent_prefers_corpus_and_exposes_reference_material(
    monkeypatch,
    tmp_path,
):
    from knowledge.service import KnowledgeService

    knowledge_root = tmp_path / "knowledge"
    service = KnowledgeService(knowledge_root)
    service.install_pack(
        validate_pack(
            {
                "schema_version": 2,
                "pack_id": "reply-fixture",
                "collection_id": "corpora",
                "material_type": "corpus",
                "source": {
                    "name": "Reply Fixture",
                    "homepage": "",
                    "license": "CC0-1.0",
                },
                "entries": [
                    {
                        "title": "你这个梗太老了",
                        "terms": {
                            "alias": [],
                            "recognition": ["你这个梗太老了怎么回复"],
                        },
                        "tags": [],
                        "summary": "回复参考语料",
                        "content": "参考回复：那我下次争取用个更新的梗。",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        knowledge_tool,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )
    monkeypatch.setattr(knowledge_tool, "open_knowledge", lambda _root: service)

    result = await knowledge_tool.handle_public_knowledge_call(
        {"query": "你这个梗太老了怎么回复"},
        language="zh",
    )

    assert "Material type: corpus" in result
    assert "Reference material: 参考回复:那我下次争取用个更新的梗。" in result


def test_material_query_plan_does_not_require_a_second_fallback_search():
    assert knowledge_tool._material_query_plan("这个要怎么回复", "auto") == (
        ("corpus", "knowledge"),
        "corpus",
    )
    assert knowledge_tool._material_query_plan("电车难题是什么", "auto") == (
        ("knowledge",),
        "knowledge",
    )


def test_normal_source_package_exports_only_local_importers():
    from knowledge.moegirl_knowledge import sources

    assert not hasattr(sources, "Geng8TagSource")
    assert not hasattr(sources, "MoegirlWikiApiSource")
