from __future__ import annotations

from knowledge.moegirl_knowledge import (
    MoegirlKnowledgeEntry,
    MoegirlKnowledgeRetriever,
    MoegirlKnowledgeStore,
)


def _entry(index: int, *, content: str | None = None) -> MoegirlKnowledgeEntry:
    return MoegirlKnowledgeEntry(
        id=f"entry-{index}",
        title="急了" if index == 0 else f"梗条目 {index}",
        aliases=("红温", "他急了") if index == 0 else (f"别名 {index}",),
        tags=("网络流行语", "ACG") if index == 0 else ("测试",),
        content=content or ("调侃某人情绪出现明显波动。" if index == 0 else f"这是第 {index} 条知识正文。"),
        summary="轻松语境使用，避免真实冲突。" if index == 0 else "离线检索夹具。",
        source_url=f"https://mzh.moegirl.org.cn/entry-{index}",
        source_page_id=1000 + index,
        synced_at="2026-07-20T00:00:00Z",
    )


def test_store_supports_title_alias_and_fulltext_retrieval_for_500_entries(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    for index in range(500):
        store.upsert(_entry(index))

    retriever = MoegirlKnowledgeRetriever(store)
    assert store.count() == 500
    assert store.integrity_ok()
    assert retriever.search("急了", limit=1)[0].entry.id == "entry-0"
    assert retriever.search("红温", limit=1)[0].entry.id == "entry-0"
    assert retriever.search("第 321 条知识正文", limit=1)[0].entry.id == "entry-321"


def test_upsert_uses_content_hash_and_keeps_fts_in_sync(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    assert store.upsert(_entry(0)).created
    assert store.upsert(_entry(0)).unchanged
    assert store.upsert(_entry(0, content="更新后的梗解释。" )).updated
    hits = MoegirlKnowledgeRetriever(store).search("更新后的梗解释", limit=1)
    assert len(hits) == 1
    assert hits[0].entry.content == "更新后的梗解释。"


def test_store_allows_matching_page_ids_from_different_wikis(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    moegirl = MoegirlKnowledgeEntry(
        id="moegirl:8", title="Moegirl entry", content="Moegirl explanation.",
        source_url="https://mzh.moegirl.org.cn/example", source_page_id=8,
    )
    wikipedia = MoegirlKnowledgeEntry(
        id="wikipedia:8", title="Wikipedia entry", content="Wikipedia explanation.",
        source_url="https://zh.wikipedia.org/wiki/Example", source_page_id=8,
    )

    assert store.upsert(moegirl).created
    assert store.upsert(wikipedia).created
    assert store.count() == 2


def test_corrupt_database_degrades_reads_without_deleting_it(tmp_path):
    database_path = tmp_path / "knowledge.db"
    database_path.write_bytes(b"not a sqlite database")
    store = MoegirlKnowledgeStore(database_path)
    assert store.count() == 0
    assert store.integrity_ok() is False
    assert MoegirlKnowledgeRetriever(store).search("急了") == []


def test_damaged_metadata_row_does_not_block_other_results(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(0))
    store.upsert(_entry(1))
    with store._connection(writable=True) as connection:
        connection.execute("UPDATE entries SET aliases = 'not-json' WHERE id = 'entry-0'")

    hits = MoegirlKnowledgeRetriever(store).search("梗条目", limit=3)
    assert [hit.entry.id for hit in hits] == ["entry-1"]
