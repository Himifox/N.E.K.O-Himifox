from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace

import pytest

from knowledge.moegirl_knowledge import (
    MoegirlKnowledgeEntry,
    MoegirlKnowledgeRetriever,
    MoegirlKnowledgeStore,
)
from knowledge.moegirl_knowledge.catalog_overrides import (
    get_catalog_override_path,
    load_disabled_entries,
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


def test_concurrent_disable_overrides_keep_both_entries(tmp_path):
    override_path = tmp_path / "catalog.override.json"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                set_entry_disabled,
                override_path,
                source_tag="source:first",
                title="first entry",
                disabled=True,
            ),
            executor.submit(
                set_entry_disabled,
                override_path,
                source_tag="source:second",
                title="second entry",
                disabled=True,
            ),
        )
        tuple(future.result() for future in futures)

    assert load_disabled_entries(override_path) == frozenset({
        ("source:first", "first entry"),
        ("source:second", "second entry"),
    })
    assert json.loads(override_path.read_text(encoding="utf-8"))["disabled"] == [
        {"source": "source:first", "title": "first entry"},
        {"source": "source:second", "title": "second entry"},
    ]


def test_concurrent_updates_of_one_disabled_key_keep_valid_json(tmp_path):
    override_path = tmp_path / "catalog.override.json"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                set_entry_disabled,
                override_path,
                source_tag="source:fixture",
                title="same entry",
                disabled=True,
            ),
            executor.submit(
                set_entry_disabled,
                override_path,
                source_tag="source:fixture",
                title="same entry",
                disabled=False,
            ),
        )
        tuple(future.result() for future in futures)

    payload = json.loads(override_path.read_text(encoding="utf-8"))
    assert payload in (
        {"disabled": []},
        {"disabled": [{"source": "source:fixture", "title": "same entry"}]},
    )


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
    browsing = await router.list_moegirl_knowledge_entries(
        query="", source="", limit=10, offset=0,
    )
    detail = await router.get_moegirl_knowledge_entry(
        source="chime", title="测试梗",
    )

    assert listing["ok"] is True
    assert listing["items"][0]["title"] == "测试梗"
    assert browsing["total"] == 1
    assert browsing["has_more"] is False
    assert set(detail["entry"]) == {
        "title", "terms", "tags", "summary", "content", "source", "disabled",
    }


@pytest.mark.asyncio
async def test_legacy_management_search_is_bounded_source_scoped_and_restorable(
    monkeypatch,
    tmp_path,
):
    import main_routers.moegirl_knowledge_router as router
    from knowledge.service import KnowledgeService

    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    for title, source in (
        ("needle alpha", "source:a"),
        ("needle beta", "source:a"),
        ("needle target one", "source:b"),
        ("needle target two", "source:b"),
    ):
        store.upsert(MoegirlKnowledgeEntry(
            title=title,
            terms={},
            tags=(source, "type:引用"),
            summary="needle",
            content="needle content",
        ))
    set_entry_disabled(
        get_catalog_override_path(database_path),
        source_tag="source:b",
        title="needle target one",
        disabled=True,
    )
    monkeypatch.setattr(
        router,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=knowledge_root),
    )
    monkeypatch.setattr(
        KnowledgeService,
        "count_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("query mode must not count the whole collection")
        ),
    )

    first = await router.list_moegirl_knowledge_entries(
        query="needle", source="b", limit=1, offset=0,
    )
    second = await router.list_moegirl_knowledge_entries(
        query="needle", source="b", limit=1, offset=1,
    )
    missing = await router.list_moegirl_knowledge_entries(
        query="needle", source="missing", limit=1, offset=0,
    )

    assert first["total"] is None
    assert first["has_more"] is True
    assert second["total"] is None
    assert second["has_more"] is False
    assert first["items"][0]["source"]["tag"] == "source:b"
    assert second["items"][0]["source"]["tag"] == "source:b"
    assert first["items"][0]["title"] != second["items"][0]["title"]
    rows = {item["title"]: item for item in (*first["items"], *second["items"])}
    assert rows["needle target one"]["disabled"] is True
    assert missing["items"] == []
    assert missing["has_more"] is False


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


def test_legacy_status_missing_root_does_not_create_database(tmp_path):
    knowledge_root = tmp_path / "missing"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"

    status = get_public_knowledge_status(
        SimpleNamespace(knowledge_dir=knowledge_root)
    )

    assert status["database"]["integrity_ok"] is False
    assert not knowledge_root.exists()
    assert not database_path.exists()


def test_legacy_status_existing_directory_does_not_create_database(tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    database_path.parent.mkdir(parents=True)

    status = get_public_knowledge_status(
        SimpleNamespace(knowledge_dir=knowledge_root)
    )

    assert status["database"]["integrity_ok"] is False
    assert not database_path.exists()


def test_legacy_status_database_directory_is_not_replaced(tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    database_path.mkdir(parents=True)

    status = get_public_knowledge_status(
        SimpleNamespace(knowledge_dir=knowledge_root)
    )

    assert status["database"]["integrity_ok"] is False
    assert database_path.is_dir()


def test_legacy_status_corrupt_database_is_not_modified(tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"not a sqlite database")
    digest = hashlib.sha256(database_path.read_bytes()).hexdigest()

    status = get_public_knowledge_status(
        SimpleNamespace(knowledge_dir=knowledge_root)
    )

    assert status["database"]["integrity_ok"] is False
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == digest


def test_legacy_status_valid_empty_database_is_healthy(tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_path = knowledge_root / "moegirl-knowledge" / "knowledge.db"
    MoegirlKnowledgeStore(database_path).replace_source("source:fixture", ())

    status = get_public_knowledge_status(
        SimpleNamespace(knowledge_dir=knowledge_root)
    )

    assert status["database"]["integrity_ok"] is True
    assert status["database"]["entries"] == 0
