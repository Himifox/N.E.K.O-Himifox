from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from plugin.plugins.data_backup.backup import BackupEngine, BackupError


def _engine(tmp_path: Path, *, retention: int = 10) -> BackupEngine:
    data_root = tmp_path / "data"
    data_root.mkdir()
    return BackupEngine(
        data_root,
        data_root / "plugins" / "data_backup" / "data" / "snapshots",
        retention=retention,
    )


def test_snapshot_and_restore_exact_core_state(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config_file = engine.data_root / "config" / "config.json"
    memory_file = engine.data_root / "memory" / "cat" / "memory.db"
    config_file.parent.mkdir(parents=True)
    memory_file.parent.mkdir(parents=True)
    config_file.write_text('{"name":"before"}', encoding="utf-8")
    memory_file.write_bytes(b"memory-before")

    snapshot = engine.create_snapshot("core")
    config_file.write_text('{"name":"after"}', encoding="utf-8")
    memory_file.unlink()
    new_file = engine.data_root / "character_cards" / "new.json"
    new_file.parent.mkdir()
    new_file.write_text("new", encoding="utf-8")

    result = engine.restore_snapshot("core", snapshot["id"])

    assert config_file.read_text(encoding="utf-8") == '{"name":"before"}'
    assert memory_file.read_bytes() == b"memory-before"
    assert not new_file.exists()
    assert result["restored"] == snapshot["id"]
    assert result["safety_snapshot"] != snapshot["id"]
    assert result["restart_required"] is True


def test_snapshot_retention_keeps_latest(tmp_path: Path) -> None:
    engine = _engine(tmp_path, retention=2)
    source = engine.data_root / "config" / "value.txt"
    source.parent.mkdir(parents=True)

    created = []
    for value in ("one", "two", "three"):
        source.write_text(value, encoding="utf-8")
        created.append(engine.create_snapshot("core")["id"])

    assert [item["id"] for item in engine.list_snapshots("core")] == list(
        reversed(created[-2:])
    )


def test_unchanged_files_are_hard_linked_when_supported(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = engine.data_root / "config" / "same.txt"
    source.parent.mkdir(parents=True)
    source.write_text("same", encoding="utf-8")
    first = engine.create_snapshot("core")
    second = engine.create_snapshot("core")
    first_file = (
        engine.backup_root / "core" / first["id"] / "files" / "config" / "same.txt"
    )
    second_file = (
        engine.backup_root / "core" / second["id"] / "files" / "config" / "same.txt"
    )

    if os.stat(first_file).st_ino == 0:
        pytest.skip("filesystem does not expose inode identifiers")
    assert os.path.samefile(first_file, second_file)


def test_rejects_unknown_group_and_snapshot_traversal(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    with pytest.raises(BackupError, match="unknown backup group"):
        engine.create_snapshot("../config")
    with pytest.raises(BackupError, match="invalid snapshot id"):
        engine.delete_snapshot("core", "../../outside")


def test_restore_rejects_tampered_snapshot(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = engine.data_root / "config" / "value.txt"
    source.parent.mkdir(parents=True)
    source.write_text("original", encoding="utf-8")
    snapshot = engine.create_snapshot("core")
    archived = (
        engine.backup_root / "core" / snapshot["id"] / "files" / "config" / "value.txt"
    )
    archived.write_text("tampered", encoding="utf-8")

    with pytest.raises(BackupError, match="checksum mismatch"):
        engine.restore_snapshot("core", snapshot["id"])
    assert source.read_text(encoding="utf-8") == "original"


def test_restore_rejects_unlisted_snapshot_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = engine.data_root / "config" / "value.txt"
    source.parent.mkdir(parents=True)
    source.write_text("original", encoding="utf-8")
    snapshot = engine.create_snapshot("core")
    injected = (
        engine.backup_root / "core" / snapshot["id"] / "files" / "config" / "extra.txt"
    )
    injected.write_text("unexpected", encoding="utf-8")

    with pytest.raises(BackupError, match="do not match"):
        engine.restore_snapshot("core", snapshot["id"])


def test_new_snapshot_does_not_link_tampered_previous_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = engine.data_root / "config" / "value.txt"
    source.parent.mkdir(parents=True)
    source.write_text("original", encoding="utf-8")
    first = engine.create_snapshot("core")
    first_file = (
        engine.backup_root / "core" / first["id"] / "files" / "config" / "value.txt"
    )
    first_file.write_text("tampered", encoding="utf-8")

    second = engine.create_snapshot("core")
    second_file = (
        engine.backup_root / "core" / second["id"] / "files" / "config" / "value.txt"
    )

    assert second_file.read_text(encoding="utf-8") == "original"
    assert not os.path.samefile(first_file, second_file)


def test_sqlite_snapshot_includes_uncheckpointed_wal_data(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    database = engine.data_root / "memory" / "cat" / "time_indexed.db"
    database.parent.mkdir(parents=True)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE facts (value TEXT)")
        connection.execute("INSERT INTO facts VALUES ('remember me')")
        connection.commit()
        snapshot = engine.create_snapshot("core")

    archived = (
        engine.backup_root
        / "core"
        / snapshot["id"]
        / "files"
        / "memory"
        / "cat"
        / "time_indexed.db"
    )
    assert not archived.with_name("time_indexed.db-wal").exists()
    with sqlite3.connect(archived) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone() == (
            "remember me",
        )
