from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge.moegirl_knowledge import (
    MoegirlKnowledgeEntry,
    MoegirlKnowledgeRetriever,
    MoegirlKnowledgeStore,
)
from knowledge.moegirl_knowledge.catalog_overrides import (
    get_catalog_override_path,
    set_entry_disabled,
)
from knowledge.moegirl_knowledge.turn_context import build_meme_turn_context
from knowledge.moegirl_knowledge.status import get_public_knowledge_status


def _insert_fixture(database_path):
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(MoegirlKnowledgeEntry(
        title="测试梗",
        terms={"alias": ("测试别名",), "recognition": ("测试固定说法",)},
        tags=("source:chime", "type:引用"),
        summary="测试含义",
        content="测试含义\n- 测试用法",
    ))
    return store


def test_disable_override_removes_entry_from_search_and_turn_delivery(tmp_path):
    database_path = tmp_path / "knowledge.db"
    store = _insert_fixture(database_path)
    retriever = MoegirlKnowledgeRetriever(store)
    override_path = get_catalog_override_path(database_path)

    assert retriever.search("测试梗", limit=1)
    assert build_meme_turn_context("这就是测试固定说法", database_path).hit_count == 1

    set_entry_disabled(
        override_path,
        source_tag="source:chime",
        title="测试梗",
        disabled=True,
    )

    assert retriever.search("测试梗", limit=1) == []
    assert build_meme_turn_context("这就是测试固定说法", database_path).hit_count == 0

    set_entry_disabled(
        override_path,
        source_tag="source:chime",
        title="测试梗",
        disabled=False,
    )
    assert retriever.search("测试梗", limit=1)


@pytest.mark.asyncio
async def test_management_list_and_detail_return_five_field_cards(monkeypatch, tmp_path):
    import main_routers.moegirl_knowledge_router as router

    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    _insert_fixture(database_path)
    monkeypatch.setattr(
        router,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )

    listing = await router.list_moegirl_knowledge_entries(
        query="测试梗", source="", limit=10, offset=0,
    )
    detail = await router.get_moegirl_knowledge_entry(
        source="chime", title="测试梗",
    )

    assert listing["ok"] is True
    assert listing["items"][0]["title"] == "测试梗"
    assert set(detail["entry"]) == {
        "title", "terms", "tags", "summary", "content", "source", "disabled",
    }


def test_status_reports_installed_local_entries_without_bundled_assets(tmp_path):
    knowledge_root = tmp_path / "knowledge"
    _insert_fixture(knowledge_root / "moegirl-knowledge" / "knowledge.db")

    status = get_public_knowledge_status(
        SimpleNamespace(knowledge_dir=knowledge_root)
    )

    assert status["database"]["entries"] == 1
    assert status["sources"]["chime"]["entries"] == 1
    assert status["sources"]["chime"]["acquisition"] == "local_package"
    assert status["sources"]["corpora"]["acquisition"] == "not_installed"
