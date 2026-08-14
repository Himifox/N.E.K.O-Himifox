from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "rebuild_knowledge_index.py"
)
SPEC = importlib.util.spec_from_file_location("rebuild_knowledge_index", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write_v5_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata(key, value) VALUES ('schema_version', '5');
            CREATE TABLE entries (
                title TEXT NOT NULL,
                terms TEXT NOT NULL,
                tags TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO entries(title, terms, tags, summary, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "Test card",
                json.dumps({"alias": ["test"], "recognition": []}),
                json.dumps(["source:test"]),
                "A summary",
                "A paragraph that can be indexed.",
            ),
        )


def _write_v6_chunks(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata(key, value) VALUES ('schema_version', '6');
            CREATE TABLE entries (
                title TEXT NOT NULL,
                terms TEXT NOT NULL,
                tags TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT NOT NULL
            );
            INSERT INTO entries VALUES ('Test', '{}', '[]', '', 'body');
            CREATE TABLE knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                entry_rowid INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading TEXT NOT NULL DEFAULT '',
                chunk_text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding_model_id TEXT,
                embedding_dimensions INTEGER,
                embedding BLOB,
                embedding_status TEXT NOT NULL,
                embedding_attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT NOT NULL DEFAULT ''
            );
            """
        )


def _insert_failed_chunk(
    database: Path,
    *,
    chunk_id: str,
    attempts: int,
    next_retry_at: int,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO knowledge_chunks("
            "chunk_id, entry_rowid, chunk_index, chunk_text, content_hash, "
            "embedding_status, embedding_attempts, next_retry_at"
            ") VALUES (?, 1, ?, 'body', ?, 'failed', ?, ?)",
            (chunk_id, attempts, chunk_id, attempts, next_retry_at),
        )


def test_status_is_read_only_and_does_not_migrate_v5(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    _write_v5_database(database)

    status = MODULE.inspect_database(database)

    assert status["schema_version"] == 5
    assert status["entries_total"] == 1
    assert status["entries_missing_chunks"] == 1
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "knowledge_chunks" not in tables


def test_full_dry_run_counts_derived_chunks_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    _write_v5_database(database)
    target = MODULE.CollectionTarget("meme", database)

    plan = MODULE.dry_run_plan(target, full=True)

    assert plan["valid_entries"] == 1
    assert plan["derived_chunks_after_rebuild"] == 1
    assert plan["affected_entries"] == 1
    assert plan["affected_chunks"] == 1
    with sqlite3.connect(database) as connection:
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
    assert schema_version == "5"


@pytest.mark.parametrize("value", ("0", "129", "not-a-number"))
def test_batch_size_rejects_values_outside_bounds(value: str) -> None:
    with pytest.raises(Exception):
        MODULE._batch_size(value)


@pytest.mark.parametrize("value", ("1", "32", "128"))
def test_batch_size_accepts_documented_bounds(value: str) -> None:
    assert MODULE._batch_size(value) == int(value)


def test_default_batch_size_is_safe_microbatch() -> None:
    args = MODULE._build_parser().parse_args(["--rebuild"])

    assert MODULE.DEFAULT_BATCH_SIZE == 4
    assert args.batch_size == 4


def test_status_splits_failed_retry_boundaries(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    _write_v6_chunks(database)
    now = int(MODULE.time.time())
    _insert_failed_chunk(
        database,
        chunk_id="retry-now",
        attempts=7,
        next_retry_at=now,
    )
    _insert_failed_chunk(
        database,
        chunk_id="waiting",
        attempts=7,
        next_retry_at=now + 60,
    )
    _insert_failed_chunk(
        database,
        chunk_id="exhausted",
        attempts=8,
        next_retry_at=now,
    )

    status = MODULE.inspect_database(database)

    assert status["chunks_failed"] == 3
    assert status["chunks_failed_retryable_now"] == 1
    assert status["chunks_failed_waiting"] == 1
    assert status["chunks_failed_exhausted"] == 1


@pytest.mark.parametrize(
    ("overrides", "last_batch_state", "expected"),
    (
        ({}, "ready", "complete"),
        ({"chunks_failed": 1, "chunks_failed_waiting": 1}, "ready", "retry_scheduled"),
        (
            {"chunks_failed": 1, "chunks_failed_exhausted": 1},
            "ready",
            "failed_exhausted",
        ),
        ({"chunks_pending": 1}, "not_ready", "embedding_unavailable"),
        ({"chunks_stale": 1}, "ready", "processing_incomplete"),
    ),
)
def test_completion_state_is_explicit(
    overrides: dict[str, int],
    last_batch_state: str,
    expected: str,
) -> None:
    status = {
        "chunks_pending": 0,
        "chunks_stale": 0,
        "chunks_failed": 0,
        "chunks_failed_retryable_now": 0,
        "chunks_failed_waiting": 0,
        "chunks_failed_exhausted": 0,
        **overrides,
    }

    assert (
        MODULE._completion_state(status, last_batch_state=last_batch_state) == expected
    )


@pytest.mark.asyncio
async def test_work_budget_is_split_into_four_item_microbatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import knowledge.vector_index as vector_index

    calls: list[int] = []

    async def _fake_index_embedding_batch(
        store: object,
        *,
        batch_size: int,
        load_model: bool,
    ) -> SimpleNamespace:
        del store
        assert load_model is True
        calls.append(batch_size)
        return SimpleNamespace(
            selected=batch_size,
            stored=batch_size,
            failed=0,
            stale_writebacks=0,
            state="ready",
        )

    monkeypatch.setattr(
        vector_index, "index_embedding_batch", _fake_index_embedding_batch
    )

    result = await MODULE._run_embedding_work_round(object(), work_budget=9)

    assert calls == [4, 4, 1]
    assert result == (9, 9, 0, 0, "ready")
