from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

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
