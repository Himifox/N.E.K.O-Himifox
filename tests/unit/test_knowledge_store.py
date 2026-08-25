from __future__ import annotations

import sqlite3

import pytest

from knowledge import (
    KnowledgeEntry,
    KnowledgeRetriever,
    KnowledgeStore,
)
from knowledge.catalog_overrides import get_catalog_override_path
from knowledge.store import KnowledgeSchemaTooNewError, KnowledgeStoreError


def _entry(index: int, *, content: str | None = None) -> KnowledgeEntry:
    return KnowledgeEntry(
        title="急了" if index == 0 else f"梗条目 {index}",
        terms={
            "alias": ("红温", "他急了") if index == 0 else (f"别名 {index}",),
            "recognition": (),
        },
        tags=("source:moegirl", "topic:网络流行语") if index == 0 else ("source:moegirl", "topic:测试"),
        content=content or ("调侃某人情绪出现明显波动。" if index == 0 else f"这是第 {index} 条知识正文。"),
        summary="轻松语境使用，避免真实冲突。" if index == 0 else "离线检索夹具。",
    )


def test_store_supports_title_alias_and_fulltext_retrieval_for_500_entries(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    for index in range(500):
        store.upsert(_entry(index))

    retriever = KnowledgeRetriever(store)
    assert store.count() == 500
    assert store.integrity_ok()
    assert retriever.search("急了", limit=1)[0].entry.title == "急了"
    assert retriever.search("红温", limit=1)[0].entry.title == "急了"
    assert retriever.search("第 321 条知识正文", limit=1)[0].entry.title == "梗条目 321"


def test_upsert_uses_content_hash_and_keeps_fts_in_sync(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    assert store.upsert(_entry(0)).created
    assert store.upsert(_entry(0)).unchanged
    assert store.upsert(_entry(0, content="更新后的梗解释。" )).updated
    hits = KnowledgeRetriever(store).search("更新后的梗解释", limit=1)
    assert len(hits) == 1
    assert hits[0].entry.content == "更新后的梗解释。"


def test_store_allows_matching_titles_from_different_sources(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    moegirl = KnowledgeEntry(
        title="Shared entry", terms={}, tags=("source:moegirl",),
        summary="Moegirl summary.", content="Moegirl explanation.",
    )
    chime = KnowledgeEntry(
        title="Shared entry", terms={}, tags=("source:chime",),
        summary="CHIME summary.", content="CHIME explanation.",
    )

    assert store.upsert(moegirl).created
    assert store.upsert(chime).created
    assert store.count() == 2


def test_corrupt_database_degrades_reads_without_deleting_it(tmp_path):
    database_path = tmp_path / "knowledge.db"
    database_path.write_bytes(b"not a sqlite database")
    store = KnowledgeStore(database_path)
    assert store.count() == 0
    assert store.integrity_ok() is False
    assert KnowledgeRetriever(store).search("急了") == []


def test_invalid_catalog_override_fails_automatic_search_closed(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(0))
    get_catalog_override_path(store.database_path).write_text(
        "not-json",
        encoding="utf-8",
    )

    assert KnowledgeRetriever(store).search("急了") == []
    assert KnowledgeRetriever(store).search("急了", include_disabled=True)


def test_damaged_metadata_row_does_not_block_other_results(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(1))
    store.upsert(_entry(2))
    with store._connection(writable=True) as connection:
        connection.execute("UPDATE entries SET terms = 'not-json' WHERE title = '梗条目 1'")

    hits = KnowledgeRetriever(store).search("梗条目", limit=3)
    assert [hit.entry.title for hit in hits] == ["梗条目 2"]


def test_store_applies_request_scoped_busy_timeout(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db", busy_timeout_ms=37)

    with store._connection() as connection:
        configured = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])

    assert configured == 37


def _write_schema_marker_database(path, *, metadata="8", user_version=0):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    if metadata is not None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
            (metadata,),
        )
    connection.execute(f"PRAGMA user_version={int(user_version)}")
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    ("metadata", "user_version", "detected"),
    (("8", 0, 8), ("7", 8, 8)),
)
def test_future_schema_is_rejected_without_mutating_database(
    tmp_path,
    metadata,
    user_version,
    detected,
):
    database_path = tmp_path / "knowledge.db"
    _write_schema_marker_database(
        database_path,
        metadata=metadata,
        user_version=user_version,
    )
    before = database_path.read_bytes()
    store = KnowledgeStore(database_path)

    with pytest.raises(KnowledgeSchemaTooNewError) as caught:
        store.assert_compatible()
    with pytest.raises(KnowledgeSchemaTooNewError):
        store.upsert(_entry(0))

    assert caught.value.detected_version == detected
    assert database_path.read_bytes() == before
    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert connection.execute("PRAGMA user_version").fetchone()[0] == user_version
    assert connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()[0] == metadata
    connection.close()


@pytest.mark.parametrize("metadata", ("not-a-number", "-1", "07"))
def test_invalid_schema_marker_fails_closed(tmp_path, metadata):
    database_path = tmp_path / "knowledge.db"
    _write_schema_marker_database(database_path, metadata=metadata)
    before = database_path.read_bytes()

    with pytest.raises(KnowledgeStoreError):
        KnowledgeStore(database_path).assert_compatible()

    assert database_path.read_bytes() == before


def test_current_metadata_only_schema_backfills_user_version(tmp_path):
    database_path = tmp_path / "knowledge.db"
    _write_schema_marker_database(database_path, metadata="7", user_version=0)

    KnowledgeStore(database_path).assert_compatible()

    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    connection.close()
