from __future__ import annotations

import importlib.util
import asyncio
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


def test_status_lists_staging_jobs_without_opening_their_database(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / ".staging" / "fixture-job"
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "job_id": "fixture-job",
                "state": "embedding",
                "created_at": 1,
            }
        ),
        encoding="utf-8",
    )
    staging_database = job_dir / "knowledge.db"

    jobs = MODULE.inspect_pack_jobs(tmp_path)

    assert jobs[0]["state"] == "embedding"
    assert not staging_database.exists()


def test_status_sorts_jobs_safely_when_created_at_is_damaged(tmp_path: Path) -> None:
    created_values = {
        "valid-new": "10",
        "valid-old": 5,
        "invalid-bool": True,
        "invalid-float": 2.5,
        "invalid-list": [],
        "invalid-negative": -1,
        "invalid-unicode": "１２",
    }
    for job_id, created_at in created_values.items():
        job_dir = tmp_path / ".staging" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "state.json").write_text(
            json.dumps({"job_id": job_id, "created_at": created_at}),
            encoding="utf-8",
        )

    jobs = MODULE.inspect_pack_jobs(tmp_path)

    assert [item["job_id"] for item in jobs] == [
        "valid-new",
        "valid-old",
        "invalid-bool",
        "invalid-float",
        "invalid-list",
        "invalid-negative",
        "invalid-unicode",
    ]


def test_full_dry_run_counts_derived_chunks_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    _write_v5_database(database)
    target = MODULE.KnowledgeTarget(database)

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


def test_enable_local_pack_requires_rebuild_action(tmp_path: Path) -> None:
    args = MODULE._build_parser().parse_args(
        [
            "--status",
            "--enable-local-pack",
            "fixture-pack",
            "--knowledge-root",
            str(tmp_path),
        ]
    )

    assert asyncio.run(MODULE._run(args)) == 2


def test_enable_local_pack_dry_run_locates_registry_without_mutating_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "knowledge.db"
    registry = {
        "schema_version": 1,
        "packs": {
            "fixture-pack": {
                "pack_id": "fixture-pack",
                "source_tag": "source:community.fixture-pack",
                "local_embedding_enabled": False,
            }
        },
    }
    registry_path = database.with_name("packs.json")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    args = MODULE._build_parser().parse_args(
        [
            "--rebuild",
            "--dry-run",
            "--enable-local-pack",
            "fixture-pack",
            "--knowledge-root",
            str(tmp_path),
        ]
    )

    assert asyncio.run(MODULE._run(args)) == 0
    assert json.loads(registry_path.read_text(encoding="utf-8")) == registry


def test_preflight_pack_reports_work_without_staging(tmp_path: Path, capsys) -> None:
    pack_path = tmp_path / "pack.json"
    pack_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "preflight-fixture",
                "material_type": "knowledge",
                "source": {"name": "Fixture", "homepage": "", "license": "CC0"},
                "entries": [
                    {
                        "title": "Fixture",
                        "terms": {"alias": [], "recognition": []},
                        "tags": [],
                        "summary": "",
                        "content": "Fixture body",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    args = MODULE._build_parser().parse_args(
        [
            "--preflight-pack",
            str(pack_path),
            "--knowledge-root",
            str(tmp_path),
        ]
    )

    result = asyncio.run(MODULE._run(args))
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["projected_chunks"] == 1
    assert not (tmp_path / ".staging").exists()


def test_cancel_job_action_removes_staged_payload(tmp_path: Path, capsys) -> None:
    job_dir = tmp_path / ".staging" / "cancel-fixture"
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(
        json.dumps({"job_id": "cancel-fixture", "state": "queued"}),
        encoding="utf-8",
    )
    (job_dir / "pack.json").write_text("{}", encoding="utf-8")
    args = MODULE._build_parser().parse_args(
        [
            "--cancel-job",
            "cancel-fixture",
            "--knowledge-root",
            str(tmp_path),
        ]
    )

    result = asyncio.run(MODULE._run(args))
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert not (job_dir / "pack.json").exists()


def test_discard_job_action_only_removes_quarantined_job(
    tmp_path: Path,
    capsys,
) -> None:
    job_dir = tmp_path / ".staging" / "degraded-fixture"
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text("[]", encoding="utf-8")
    args = MODULE._build_parser().parse_args(
        [
            "--discard-job",
            "degraded-fixture",
            "--knowledge-root",
            str(tmp_path),
        ]
    )

    result = asyncio.run(MODULE._run(args))
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert not job_dir.exists()


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


def test_status_treats_v6_chunks_as_local_policy(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    _write_v6_chunks(database)
    _insert_failed_chunk(
        database,
        chunk_id="legacy-local",
        attempts=1,
        next_retry_at=0,
    )

    status = MODULE.inspect_database(database)

    assert status["chunks_local"] == 1
    assert status["chunks_prebuilt_only"] == 0
    assert status["chunks_local_failed_retryable_now"] == 1


def test_maintenance_counts_only_local_policy_work(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.db"
    _write_v6_chunks(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE knowledge_chunks ADD COLUMN embedding_policy TEXT "
            "NOT NULL DEFAULT 'local'"
        )
        connection.execute("UPDATE metadata SET value='7' WHERE key='schema_version'")
        connection.execute(
            "INSERT INTO knowledge_chunks("
            "chunk_id, entry_rowid, chunk_index, chunk_text, content_hash, "
            "embedding_status, embedding_policy) "
            "VALUES ('prebuilt', 1, 0, 'body', 'hash', 'pending', 'prebuilt_only')"
        )

    status = MODULE.inspect_database(database)

    assert status["chunks_pending"] == 1
    assert status["chunks_local_pending"] == 0
    assert status["chunks_prebuilt_only"] == 1
    assert MODULE._eligible_chunk_count(status) == 0
    assert MODULE._completion_state(status) == "complete"


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
