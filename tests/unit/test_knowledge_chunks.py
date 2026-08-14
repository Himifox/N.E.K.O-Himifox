from __future__ import annotations

import sqlite3

from knowledge.chunking import (
    EMBEDDING_INPUT_VERSION,
    MAX_CHUNKS_PER_ENTRY,
    MAX_EMBEDDING_CHARS,
    derive_knowledge_chunks,
    knowledge_query_embedding_text,
)
from knowledge.moegirl_knowledge import store as store_module
from knowledge.moegirl_knowledge.models import MoegirlKnowledgeEntry
from knowledge.moegirl_knowledge.store import MoegirlKnowledgeStore


def _entry(*, content: str, tags=("source:test",), summary="summary"):
    return MoegirlKnowledgeEntry(
        title="Hybrid retrieval",
        terms={"alias": ("hybrid",), "recognition": ("RAG",)},
        tags=tags,
        summary=summary,
        content=content,
    )


def test_chunking_is_deterministic_and_tracks_markdown_heading():
    entry = _entry(content="# Origin\n\nFirst paragraph.\n\nSecond paragraph.")
    first = derive_knowledge_chunks(entry, entry_key="source:test:Hybrid retrieval")
    second = derive_knowledge_chunks(entry, entry_key="source:test:Hybrid retrieval")

    assert first == second
    assert first[0].heading == "Origin"
    assert first[0].embedding_text.startswith("Document:\n")
    assert "Title: Hybrid retrieval" in first[0].embedding_text
    assert "Aliases: hybrid" in first[0].embedding_text


def test_query_and_document_embedding_inputs_have_independent_prefixes():
    chunks = derive_knowledge_chunks(
        _entry(content="Document body"),
        entry_key="source:test:Hybrid retrieval",
    )

    assert knowledge_query_embedding_text("  how does hybrid RAG work?  ") == (
        "Query: how does hybrid RAG work?"
    )
    assert chunks[0].embedding_text.splitlines()[0] == "Document:"
    assert "Query:" not in chunks[0].embedding_text


def test_chunking_is_bounded_for_long_unbroken_content():
    chunks = derive_knowledge_chunks(
        _entry(content="x" * 50_000),
        entry_key="source:test:Hybrid retrieval",
    )
    assert len(chunks) == MAX_CHUNKS_PER_ENTRY
    assert all(len(chunk.chunk_text) <= 1_200 for chunk in chunks)
    assert all(len(chunk.embedding_text) <= MAX_EMBEDDING_CHARS for chunk in chunks)
    assert chunks[0].chunk_text[-120:] == chunks[1].chunk_text[:120]


def test_schema_v6_migration_keeps_fts_and_backfills_lazily(tmp_path):
    path = tmp_path / "knowledge.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata VALUES ('schema_version', '5');
        CREATE TABLE entries (
            title TEXT NOT NULL,
            terms TEXT NOT NULL,
            tags TEXT NOT NULL,
            summary TEXT NOT NULL,
            content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE entries_fts USING fts5(
            entry_rowid UNINDEXED, title, terms, tags, summary, content,
            tokenize='unicode61'
        );
        INSERT INTO entries VALUES (
            'Legacy title', '{"alias":["old"],"recognition":[]}',
            '["source:test"]', 'Legacy summary', 'Legacy semantic content'
        );
        INSERT INTO entries_fts VALUES (
            1, 'Legacy title', 'old', 'source:test',
            'Legacy summary', 'Legacy semantic content'
        );
    """)
    connection.commit()
    connection.close()

    store = MoegirlKnowledgeStore(path)
    assert store.count() == 1
    assert store.query_fts('"Legacy"', limit=1)
    assert store.chunk_status()["entries_missing_chunks"] == 1
    assert store.backfill_missing_chunks(limit=1) == 1
    assert store.chunk_status()["chunks_pending"] == 1


def test_embedding_input_v2_migration_only_clears_derived_chunks(tmp_path):
    path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(path)
    store.upsert(_entry(content="The answer remains searchable."))
    with store._connection(writable=True) as connection:
        connection.execute(
            "UPDATE knowledge_chunks SET embedding_status='ready', "
            "embedding_model_id='old-contract', embedding_dimensions=2, embedding=?",
            (b"\x00\x00\x00\x00",),
        )
        connection.execute(
            "DELETE FROM metadata WHERE key='embedding_input_version'"
        )
        old_revision = int(connection.execute(
            "SELECT value FROM metadata WHERE key='chunks_revision'"
        ).fetchone()[0])

    # Force a fresh database-open initialization, as an existing v6 database
    # from before the input contract version key would experience on upgrade.
    store_module._INITIALIZED_DATABASES.pop(str(path.resolve()), None)
    reopened = MoegirlKnowledgeStore(path)

    assert reopened.count() == 1
    assert reopened.query_fts('"answer"', limit=1)
    with reopened._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_chunks"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='embedding_input_version'"
        ).fetchone()[0] == str(EMBEDDING_INPUT_VERSION)
        assert int(connection.execute(
            "SELECT value FROM metadata WHERE key='chunks_revision'"
        ).fetchone()[0]) == old_revision + 1
        assert connection.execute(
            "SELECT COUNT(*) FROM entries_fts"
        ).fetchone()[0] == 1

    assert reopened.chunk_status()["entries_missing_chunks"] == 1


def test_current_embedding_input_version_keeps_derived_chunks_on_reopen(tmp_path):
    path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(path)
    store.upsert(_entry(content="Current input contract."))
    before = store.chunk_status()

    store_module._INITIALIZED_DATABASES.pop(str(path.resolve()), None)
    reopened = MoegirlKnowledgeStore(path)
    after = reopened.chunk_status()

    assert after["chunks_total"] == before["chunks_total"] == 1
    assert after["chunks_revision"] == before["chunks_revision"]


def test_tag_only_update_preserves_ready_embedding(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    original = _entry(content="same content")
    store.upsert(original)
    with store._connection(writable=True) as connection:
        connection.execute(
            "UPDATE knowledge_chunks SET embedding_status='ready', "
            "embedding_model_id='fixture', embedding_dimensions=2, embedding=?",
            (b"\x00\x00\x00\x00",),
        )

    store.upsert(_entry(content="same content", tags=("source:test", "topic:new")))
    with store._connection() as connection:
        row = connection.execute("SELECT * FROM knowledge_chunks").fetchone()
    assert row["embedding_status"] == "ready"
    assert row["embedding"] == b"\x00\x00\x00\x00"


def test_content_update_reuses_unchanged_chunks_and_deletes_orphans(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(content="# A\n\n" + "a" * 1_100 + "\n\n# B\n\n" + "b" * 1_100))
    with store._connection(writable=True) as connection:
        rows = connection.execute("SELECT chunk_id FROM knowledge_chunks ORDER BY chunk_index").fetchall()
        connection.execute(
            "UPDATE knowledge_chunks SET embedding_status='ready', embedding_model_id='fixture', "
            "embedding_dimensions=2, embedding=?",
            (b"\x00\x00\x00\x00",),
        )
    original_ids = {row["chunk_id"] for row in rows}

    store.upsert(_entry(content="# New\n\nnew\n\n# A\n\n" + "a" * 1_100 + "\n\n# B\n\n" + "b" * 1_100))
    with store._connection() as connection:
        updated = connection.execute("SELECT * FROM knowledge_chunks").fetchall()
    reused = [row for row in updated if row["chunk_id"] in original_ids]
    assert reused
    assert all(row["embedding_status"] == "ready" for row in reused)

    store.replace_source("source:test", ())
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == 0


def test_source_deletion_invalidates_ready_vector_cache_revision(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(content="ready content"))
    before = store.chunks_revision()

    store.replace_source("source:test", ())

    assert store.chunks_revision() > before


def test_embedding_result_cannot_overwrite_a_changed_chunk(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(content="old text"))
    pending = store.pending_embedding_chunks(model_id="fixture", limit=1)[0]

    store.upsert(_entry(content="new text"))

    assert store.store_chunk_embedding(
        chunk_id=str(pending["chunk_id"]),
        content_hash=str(pending["content_hash"]),
        model_id="fixture",
        dimensions=2,
        embedding=b"\x00\x00\x00\x00",
    ) is False
    assert store.chunk_status()["chunks_ready"] == 0


def test_model_change_marks_only_other_ready_vectors_stale(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry(content="same content"))
    pending = store.pending_embedding_chunks(model_id="old-model", limit=1)[0]
    assert store.store_chunk_embedding(
        chunk_id=str(pending["chunk_id"]),
        content_hash=str(pending["content_hash"]),
        model_id="old-model",
        dimensions=2,
        embedding=b"\x00\x00\x00\x00",
    )

    assert store.mark_other_models_stale("new-model") == 1
    status = store.chunk_status()
    assert status["chunks_ready"] == 0
    assert status["chunks_stale"] == 1


def test_source_replacement_reuses_unchanged_vectors(tmp_path):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
    original = _entry(content="same packaged content")
    store.replace_source("source:test", (original,))
    pending = store.pending_embedding_chunks(model_id="fixture", limit=1)[0]
    assert store.store_chunk_embedding(
        chunk_id=str(pending["chunk_id"]),
        content_hash=str(pending["content_hash"]),
        model_id="fixture",
        dimensions=2,
        embedding=b"\x00\x00\x00\x00",
    )

    result = store.replace_source("source:test", (original,))

    assert result[0].unchanged is True
    assert store.chunk_status()["chunks_ready"] == 1
