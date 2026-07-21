from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import main_logic.moegirl_knowledge_tool as knowledge_tool
from knowledge.moegirl_knowledge import MoegirlKnowledgeRetriever, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.sources import SourcePage


@pytest.mark.asyncio
async def test_local_miss_uses_encyclopedia_then_escalates_to_plugin(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "moegirl-knowledge").mkdir(parents=True)
    monkeypatch.setattr(knowledge_tool, "get_config_manager", lambda: SimpleNamespace(knowledge_dir=knowledge_root))

    async def _encyclopedia_miss(*args, **kwargs):
        return [], "encyclopedia_miss"

    monkeypatch.setattr(knowledge_tool, "_fetch_and_store_on_miss", _encyclopedia_miss)

    result = await knowledge_tool.handle_moegirl_knowledge_call(
        {"query": "not in the local fixture"}, language="en"
    )

    assert "local database or encyclopedia sources" in result
    assert "enabled web_search plugin" in result


def test_knowledge_sources_do_not_embed_a_general_web_search_adapter():
    from knowledge.moegirl_knowledge import sources

    assert not hasattr(sources, "BingRssWebSearchSource")


@pytest.mark.asyncio
async def test_encyclopedia_sources_run_serially_within_one_budget(monkeypatch, tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    retriever = MoegirlKnowledgeRetriever(store)
    first_finished = False
    call_order: list[str] = []

    class _MoegirlSource:
        def __init__(self, **_kwargs) -> None:
            pass

        async def find_relevant_page(self, *_args, **_kwargs):
            nonlocal first_finished
            call_order.append("moegirl")
            await __import__("asyncio").sleep(0)
            first_finished = True
            return None

    class _WikipediaSource:
        def __init__(self, **_kwargs) -> None:
            pass

        async def find_relevant_page(self, *_args, **_kwargs):
            assert first_finished
            call_order.append("wikipedia")
            return SourcePage(
                title="Target meme", content="Target meme explanation.",
                source_url="https://zh.wikipedia.org/wiki/Target_meme", page_id=8,
            )

    monkeypatch.setattr(knowledge_tool, "MoegirlWikiApiSource", _MoegirlSource)
    monkeypatch.setattr(knowledge_tool, "ChineseWikipediaApiSource", _WikipediaSource)
    monkeypatch.setattr(knowledge_tool, "MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS", 1.0)

    hits, source = await knowledge_tool._fetch_and_store_on_miss(
        "Target meme", store, retriever, limit=1
    )

    assert call_order == ["moegirl", "wikipedia"]
    assert source == "wikipedia_stored"
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_slow_first_encyclopedia_does_not_starve_the_second_source(monkeypatch, tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    retriever = MoegirlKnowledgeRetriever(store)
    call_order: list[str] = []

    class _SlowMoegirlSource:
        def __init__(self, **_kwargs) -> None:
            pass

        async def find_relevant_page(self, *_args, **_kwargs):
            call_order.append("moegirl")
            await asyncio.sleep(1.0)
            return None

    class _WikipediaSource:
        def __init__(self, **_kwargs) -> None:
            pass

        async def find_relevant_page(self, *_args, **_kwargs):
            call_order.append("wikipedia")
            return SourcePage(
                title="Target meme", content="Target meme explanation.",
                source_url="https://zh.wikipedia.org/wiki/Target_meme", page_id=9,
            )

    monkeypatch.setattr(knowledge_tool, "MoegirlWikiApiSource", _SlowMoegirlSource)
    monkeypatch.setattr(knowledge_tool, "ChineseWikipediaApiSource", _WikipediaSource)
    monkeypatch.setattr(knowledge_tool, "MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS", 0.5)

    hits, source = await knowledge_tool._fetch_and_store_on_miss(
        "Target meme", store, retriever, limit=1
    )

    assert call_order == ["moegirl", "wikipedia"]
    assert source == "wikipedia_stored"
    assert len(hits) == 1
