from __future__ import annotations

from knowledge import (
    KnowledgeEntry,
    KnowledgeRetriever,
    KnowledgeStore,
)


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


def test_damaged_metadata_row_does_not_block_other_results(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(1))
    store.upsert(_entry(2))
    with store._connection(writable=True) as connection:
        connection.execute("UPDATE entries SET terms = 'not-json' WHERE title = '梗条目 1'")

    hits = KnowledgeRetriever(store).search("梗条目", limit=3)
    assert [hit.entry.title for hit in hits] == ["梗条目 2"]
