"""SQLite persistence for five-field public knowledge cards."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from knowledge.chunking import (
    CHUNKER_VERSION,
    EMBEDDING_INPUT_VERSION,
    derive_knowledge_chunks,
    knowledge_embedding_text,
)

from .models import KnowledgeEntry, UpsertResult


SCHEMA_VERSION = 7
MAX_EMBEDDING_ATTEMPTS = 8
EMBEDDING_POLICIES = frozenset(("local", "prebuilt_only"))
_INITIALIZED_DATABASES: dict[str, tuple[int, int] | None] = {}
_INITIALIZE_LOCK = threading.Lock()


class KnowledgeStoreError(RuntimeError):
    pass


class KnowledgeStore:
    """Own a small rebuildable database without touching character memory."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.database_path = Path(database_path)
        self.busy_timeout_ms = min(max(int(busy_timeout_ms), 1), 5_000)

    @contextmanager
    def _connection(self, *, writable: bool = False) -> Iterator[sqlite3.Connection]:
        if writable:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_key = str(self.database_path.resolve())
            identity = _database_identity(self.database_path)
            if (
                cache_key not in _INITIALIZED_DATABASES
                or _INITIALIZED_DATABASES[cache_key] != identity
            ):
                with _INITIALIZE_LOCK:
                    identity = _database_identity(self.database_path)
                    if (
                        cache_key not in _INITIALIZED_DATABASES
                        or _INITIALIZED_DATABASES[cache_key] != identity
                    ):
                        connection = self._open_connection(writable=writable)
                        self._initialize(connection)
                        # Migration and compatibility writes must persist before
                        # any caller observes the initialized database.
                        connection.commit()
                        _INITIALIZED_DATABASES[cache_key] = _database_identity(
                            self.database_path
                        )
                    else:
                        connection = self._open_connection(writable=writable)
            else:
                connection = self._open_connection(writable=writable)
            yield connection
            if writable:
                connection.commit()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeStoreError(
                f"knowledge database is unavailable: {exc}"
            ) from exc
        finally:
            if "connection" in locals():
                connection.close()

    def _open_connection(self, *, writable: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        if writable:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(entries)").fetchall()
        }
        if columns and "terms" not in columns:
            self._migrate_legacy_entries(connection)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                title TEXT NOT NULL,
                terms TEXT NOT NULL DEFAULT '{"alias":[],"recognition":[]}',
                tags TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                entry_rowid UNINDEXED,
                title,
                terms,
                tags,
                summary,
                content,
                tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                entry_rowid INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading TEXT NOT NULL DEFAULT '',
                chunk_text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding_policy TEXT NOT NULL DEFAULT 'local'
                    CHECK (embedding_policy IN ('local', 'prebuilt_only')),
                embedding_model_id TEXT,
                embedding_dimensions INTEGER,
                embedding BLOB,
                embedding_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (embedding_status IN ('pending', 'ready', 'stale', 'failed')),
                embedding_attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT NOT NULL DEFAULT '',
                UNIQUE(entry_rowid, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS knowledge_chunks_entry_idx
                ON knowledge_chunks(entry_rowid);
            CREATE INDEX IF NOT EXISTS knowledge_chunks_status_idx
                ON knowledge_chunks(embedding_status);
            CREATE INDEX IF NOT EXISTS knowledge_chunks_model_status_idx
                ON knowledge_chunks(embedding_model_id, embedding_status);
            CREATE INDEX IF NOT EXISTS knowledge_chunks_retry_idx
                ON knowledge_chunks(next_retry_at, embedding_status);
            DROP TRIGGER IF EXISTS entries_delete_knowledge_chunks;
            CREATE TRIGGER entries_delete_knowledge_chunks
            AFTER DELETE ON entries BEGIN
                DELETE FROM knowledge_chunks WHERE entry_rowid = OLD.rowid;
                INSERT OR IGNORE INTO metadata(key, value)
                    VALUES ('chunks_revision', '0');
                UPDATE metadata SET value = CAST(value AS INTEGER) + 1
                    WHERE key = 'chunks_revision';
            END;
            """
        )
        chunk_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(knowledge_chunks)")
        }
        if "embedding_policy" not in chunk_columns:
            connection.execute(
                "ALTER TABLE knowledge_chunks ADD COLUMN embedding_policy TEXT "
                "NOT NULL DEFAULT 'local' "
                "CHECK (embedding_policy IN ('local', 'prebuilt_only'))"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS knowledge_chunks_policy_status_idx "
            "ON knowledge_chunks(embedding_policy, embedding_status)"
        )
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "chunker_version": str(CHUNKER_VERSION),
            "chunks_revision": "0",
            "embedding_model_id": "",
            "indexer_status": "idle",
        }
        connection.executemany(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
            metadata.items(),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        input_version_row = connection.execute(
            "SELECT value FROM metadata WHERE key='embedding_input_version'"
        ).fetchone()
        if input_version_row is None or str(input_version_row["value"]) != str(
            EMBEDDING_INPUT_VERSION
        ):
            has_derived_chunks = bool(
                connection.execute("SELECT 1 FROM knowledge_chunks LIMIT 1").fetchone()
            )
            if has_derived_chunks:
                connection.execute("DELETE FROM knowledge_chunks")
                self._increment_chunks_revision(connection)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES "
                "('embedding_input_version', ?)",
                (str(EMBEDDING_INPUT_VERSION),),
            )
        connection.execute(
            "DELETE FROM knowledge_chunks WHERE entry_rowid NOT IN (SELECT rowid FROM entries)"
        )
        self._repair_legacy_source_tags(connection)

    @staticmethod
    def _repair_legacy_source_tags(connection: sqlite3.Connection) -> None:
        """Normalize the five-field migration's former source tag spelling."""
        known = {"chime", "geng-guide", "moegirl", "geng8"}
        changed = False
        for row in connection.execute("SELECT rowid, tags FROM entries").fetchall():
            tags = list(_json_values(row["tags"]))
            if any(tag.startswith("source:") for tag in tags):
                continue
            source = next((tag for tag in tags if tag in known), "")
            if not source:
                continue
            tags = [f"source:{source}", *(tag for tag in tags if tag != source)]
            connection.execute(
                "UPDATE entries SET tags=? WHERE rowid=?",
                (_values_json(tags), row["rowid"]),
            )
            connection.execute(
                "UPDATE entries_fts SET tags=? WHERE entry_rowid=?",
                (" ".join(tags), row["rowid"]),
            )
            changed = True
        if changed:
            KnowledgeStore._increment_entries_revision(connection)

    def _migrate_legacy_entries(self, connection: sqlite3.Connection) -> None:
        """Copy the old attributed schema into a five-field table once.

        The external backup is intentionally retained.  The guide importer later
        replaces its source slice from the original export, removing historical
        tag-as-alias pollution.
        """
        backup = self.database_path.with_suffix(
            self.database_path.suffix + ".legacy.bak"
        )
        if self.database_path.exists() and not backup.exists():
            connection.commit()
            shutil.copy2(self.database_path, backup)
        rows = connection.execute("SELECT * FROM entries").fetchall()
        connection.execute("DROP TABLE IF EXISTS entries_fts")
        connection.execute("ALTER TABLE entries RENAME TO entries_legacy")
        connection.execute(
            "CREATE TABLE entries (title TEXT NOT NULL, terms TEXT NOT NULL, tags TEXT NOT NULL, summary TEXT NOT NULL, content TEXT NOT NULL)"
        )
        for row in rows:
            tags = _json_values(row["tags"])
            aliases = (
                () if "source:geng-guide" in tags else _json_values(row["aliases"])
            )
            terms = {"alias": list(aliases), "recognition": []}
            connection.execute(
                "INSERT INTO entries(title, terms, tags, summary, content) VALUES (?, ?, ?, ?, ?)",
                (
                    row["title"],
                    json.dumps(terms, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    row["summary"],
                    row["content"],
                ),
            )
        connection.execute("DROP TABLE entries_legacy")

    def upsert(self, entry: KnowledgeEntry) -> UpsertResult:
        with self._connection(writable=True) as connection:
            result = self._upsert_with_connection(connection, entry)
            if not result.unchanged:
                self._increment_entries_revision(connection)
        if not result.unchanged:
            self._notify_routing_changed()
        return result

    def upsert_many(
        self, entries: Sequence[KnowledgeEntry]
    ) -> tuple[UpsertResult, ...]:
        with self._connection(writable=True) as connection:
            results = tuple(
                self._upsert_with_connection(connection, entry) for entry in entries
            )
            if any(not result.unchanged for result in results):
                self._increment_entries_revision(connection)
        if any(not result.unchanged for result in results):
            self._notify_routing_changed()
        return results

    def replace_source(
        self,
        source_tag: str,
        entries: Sequence[KnowledgeEntry],
        *,
        embedding_policy: str = "local",
    ) -> tuple[UpsertResult, ...]:
        """Atomically replace a fixed bundled/imported source namespace."""
        embedding_policy = _validate_embedding_policy(embedding_policy)
        if not source_tag.startswith("source:") or any(
            entry.source_tag != source_tag for entry in entries
        ):
            raise ValueError("entries must all belong to the requested source")
        with self._connection(writable=True) as connection:
            existing_rows = connection.execute(
                "SELECT entries.rowid, entries.title FROM entries "
                "JOIN json_each(entries.tags) tag WHERE tag.value=?",
                (source_tag,),
            ).fetchall()
            incoming_titles = {entry.title for entry in entries}
            title_counts: dict[str, int] = {}
            for row in existing_rows:
                title = str(row["title"])
                title_counts[title] = title_counts.get(title, 0) + 1
            removed_rowids = [
                int(row["rowid"])
                for row in existing_rows
                if str(row["title"]) not in incoming_titles
                or title_counts[str(row["title"])] > 1
            ]
            if removed_rowids:
                connection.executemany(
                    "DELETE FROM entries_fts WHERE entry_rowid=?",
                    ((rowid,) for rowid in removed_rowids),
                )
                connection.executemany(
                    "DELETE FROM entries WHERE rowid=?",
                    ((rowid,) for rowid in removed_rowids),
                )
            results = tuple(
                self._upsert_with_connection(
                    connection,
                    entry,
                    embedding_policy=embedding_policy,
                )
                for entry in entries
            )
            changed = bool(removed_rowids) or any(
                not result.unchanged for result in results
            )
            if changed:
                self._increment_entries_revision(connection)
        if changed:
            self._notify_routing_changed()
        return results

    def _notify_routing_changed(self) -> None:
        # Local import avoids a persistence -> routing import cycle.
        from knowledge.indexer import notify_knowledge_index_changed
        from knowledge.routing import notify_database_changed

        notify_database_changed(self.database_path)
        notify_knowledge_index_changed()

    @staticmethod
    def _entry_key(entry: KnowledgeEntry) -> str:
        return f"{entry.source_tag}:{entry.title}"

    def _upsert_with_connection(
        self,
        connection: sqlite3.Connection,
        entry: KnowledgeEntry,
        *,
        embedding_policy: str = "local",
    ) -> UpsertResult:
        embedding_policy = _validate_embedding_policy(embedding_policy)
        rows = connection.execute(
            "SELECT rowid, * FROM entries WHERE title = ? AND EXISTS (SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value = ?)",
            (entry.title, entry.source_tag),
        ).fetchall()
        if len(rows) == 1:
            existing = _entry_from_row(rows[0])
            if existing.content_hash == entry.content_hash:
                if self._reconcile_chunks(
                    connection,
                    int(rows[0]["rowid"]),
                    entry,
                    embedding_policy=embedding_policy,
                ):
                    return UpsertResult(self._entry_key(entry), updated=True)
                return UpsertResult(self._entry_key(entry), unchanged=True)
            rowid = rows[0]["rowid"]
            connection.execute(
                "UPDATE entries SET terms=?, tags=?, summary=?, content=? WHERE rowid=?",
                (
                    _terms_json(entry),
                    _values_json(entry.tags),
                    entry.summary,
                    entry.content,
                    rowid,
                ),
            )
            self._replace_fts(connection, rowid, entry)
            self._reconcile_chunks(
                connection,
                rowid,
                entry,
                embedding_policy=embedding_policy,
            )
            return UpsertResult(self._entry_key(entry), updated=True)
        return self._insert_with_connection(
            connection,
            entry,
            embedding_policy=embedding_policy,
        )

    def _insert_with_connection(
        self,
        connection: sqlite3.Connection,
        entry: KnowledgeEntry,
        *,
        embedding_policy: str = "local",
    ) -> UpsertResult:
        cursor = connection.execute(
            "INSERT INTO entries(title, terms, tags, summary, content) VALUES (?, ?, ?, ?, ?)",
            (
                entry.title,
                _terms_json(entry),
                _values_json(entry.tags),
                entry.summary,
                entry.content,
            ),
        )
        rowid = int(cursor.lastrowid)
        self._replace_fts(connection, rowid, entry)
        self._reconcile_chunks(
            connection,
            rowid,
            entry,
            embedding_policy=embedding_policy,
        )
        return UpsertResult(self._entry_key(entry), created=True)

    @staticmethod
    def _replace_fts(
        connection: sqlite3.Connection, rowid: int, entry: KnowledgeEntry
    ) -> None:
        connection.execute("DELETE FROM entries_fts WHERE entry_rowid = ?", (rowid,))
        connection.execute(
            "INSERT INTO entries_fts(entry_rowid, title, terms, tags, summary, content) VALUES (?, ?, ?, ?, ?, ?)",
            (
                rowid,
                entry.title,
                _terms_search_text(entry),
                " ".join(entry.tags),
                entry.summary,
                entry.content,
            ),
        )

    @classmethod
    def _reconcile_chunks(
        cls,
        connection: sqlite3.Connection,
        rowid: int,
        entry: KnowledgeEntry,
        *,
        embedding_policy: str = "local",
    ) -> bool:
        embedding_policy = _validate_embedding_policy(embedding_policy)
        derived = derive_knowledge_chunks(entry, entry_key=cls._entry_key(entry))
        existing = {
            str(row["chunk_id"]): row
            for row in connection.execute(
                "SELECT * FROM knowledge_chunks WHERE entry_rowid=?",
                (rowid,),
            ).fetchall()
        }
        desired_ids = {chunk.chunk_id for chunk in derived}
        changed = desired_ids != set(existing)
        if not changed:
            changed = any(
                int(existing[chunk.chunk_id]["chunk_index"]) != chunk.chunk_index
                or str(existing[chunk.chunk_id]["heading"]) != chunk.heading
                or str(existing[chunk.chunk_id]["chunk_text"]) != chunk.chunk_text
                or str(existing[chunk.chunk_id]["embedding_policy"]) != embedding_policy
                for chunk in derived
            )
        if not changed:
            return False

        connection.execute("DELETE FROM knowledge_chunks WHERE entry_rowid=?", (rowid,))
        for chunk in derived:
            old = existing.get(chunk.chunk_id)
            connection.execute(
                "INSERT INTO knowledge_chunks("
                "chunk_id, entry_rowid, chunk_index, heading, chunk_text, content_hash, "
                "embedding_policy, "
                "embedding_model_id, embedding_dimensions, embedding, embedding_status, "
                "embedding_attempts, next_retry_at, last_error_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.chunk_id,
                    rowid,
                    chunk.chunk_index,
                    chunk.heading,
                    chunk.chunk_text,
                    chunk.content_hash,
                    embedding_policy,
                    old["embedding_model_id"] if old is not None else None,
                    old["embedding_dimensions"] if old is not None else None,
                    old["embedding"] if old is not None else None,
                    old["embedding_status"] if old is not None else "pending",
                    int(old["embedding_attempts"]) if old is not None else 0,
                    int(old["next_retry_at"]) if old is not None else 0,
                    str(old["last_error_code"]) if old is not None else "",
                ),
            )
        cls._increment_chunks_revision(connection)
        return True

    @staticmethod
    def _increment_entries_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('entries_revision', '0')"
        )
        connection.execute(
            "UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'entries_revision'"
        )

    @staticmethod
    def _increment_chunks_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('chunks_revision', '0')"
        )
        connection.execute(
            "UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'chunks_revision'"
        )

    def count(self) -> int:
        try:
            with self._connection() as connection:
                return int(
                    connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                )
        except KnowledgeStoreError:
            return 0

    def count_by_source_tag(self, source_tag: str) -> int:
        try:
            with self._connection() as connection:
                return int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entries WHERE EXISTS (SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value = ?)",
                        (source_tag,),
                    ).fetchone()[0]
                )
        except KnowledgeStoreError:
            return 0

    def count_by_source_tags(self) -> tuple[dict[str, object], ...]:
        """Return compact source counts without materializing entry rows."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT tag.value source_tag, COUNT(*) entry_count "
                    "FROM entries JOIN json_each(entries.tags) tag "
                    "WHERE tag.value LIKE 'source:%' "
                    "GROUP BY tag.value ORDER BY tag.value"
                ).fetchall()
                return tuple(
                    {
                        "tag": str(row["source_tag"]),
                        "entries": int(row["entry_count"]),
                    }
                    for row in rows
                )
        except KnowledgeStoreError:
            return ()

    def community_usage(self, *, source_tag: str = "") -> dict[str, int]:
        """Count user-pack source data without materializing entry text."""
        source_match = "= ?" if source_tag else "LIKE 'source:community.%'"
        parameters = (source_tag,) if source_tag else ()
        try:
            with self._connection() as connection:
                entry_row = connection.execute(
                    "SELECT COUNT(*) entries_total, "
                    "COALESCE(SUM(LENGTH(CAST(content AS BLOB))), 0) content_bytes "
                    "FROM entries WHERE EXISTS (SELECT 1 FROM json_each(entries.tags) tag "
                    f"WHERE tag.value {source_match})",
                    parameters,
                ).fetchone()
                chunks_total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks JOIN entries "
                        "ON entries.rowid=knowledge_chunks.entry_rowid WHERE EXISTS "
                        "(SELECT 1 FROM json_each(entries.tags) tag "
                        f"WHERE tag.value {source_match})",
                        parameters,
                    ).fetchone()[0]
                )
                return {
                    "entries_total": int(entry_row["entries_total"]),
                    "chunks_total": chunks_total,
                    "content_bytes": int(entry_row["content_bytes"]),
                }
        except KnowledgeStoreError:
            return {
                "entries_total": 0,
                "chunks_total": 0,
                "content_bytes": 0,
            }

    def entries_revision(self) -> int:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'entries_revision'"
                ).fetchone()
                return int(row["value"]) if row else 0
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0

    def chunks_revision(self) -> int:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'chunks_revision'"
                ).fetchone()
                return int(row["value"]) if row else 0
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0

    def backfill_missing_chunks(
        self,
        *,
        limit: int = 64,
        embedding_policy_by_source: Mapping[str, str] | None = None,
    ) -> int:
        """Derive chunks for legacy rows without doing model inference."""
        limit = min(max(int(limit), 1), 256)
        policies = embedding_policy_by_source or {}
        processed = 0
        with self._connection(writable=True) as connection:
            rows = connection.execute(
                "SELECT entries.rowid, entries.* FROM entries "
                "LEFT JOIN knowledge_chunks ON knowledge_chunks.entry_rowid=entries.rowid "
                "WHERE knowledge_chunks.entry_rowid IS NULL "
                "ORDER BY entries.rowid LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                try:
                    entry = _entry_from_row(row)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                embedding_policy = policies.get(entry.source_tag)
                if embedding_policy is None:
                    embedding_policy = (
                        "prebuilt_only"
                        if entry.source_tag.startswith("source:community.")
                        else "local"
                    )
                self._reconcile_chunks(
                    connection,
                    int(row["rowid"]),
                    entry,
                    embedding_policy=embedding_policy,
                )
                processed += 1
        return processed

    def chunk_status(self) -> dict[str, object]:
        try:
            with self._connection() as connection:
                counts = {
                    str(row["embedding_status"]): int(row["count"])
                    for row in connection.execute(
                        "SELECT embedding_status, COUNT(*) count FROM knowledge_chunks "
                        "GROUP BY embedding_status"
                    ).fetchall()
                }
                missing = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entries WHERE NOT EXISTS ("
                        "SELECT 1 FROM knowledge_chunks WHERE entry_rowid=entries.rowid)"
                    ).fetchone()[0]
                )
                revision_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='chunks_revision'"
                ).fetchone()
                now = int(time.time())
                failed_retryable_now = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks "
                        "WHERE embedding_status='failed' AND embedding_attempts<? "
                        "AND next_retry_at<=?",
                        (MAX_EMBEDDING_ATTEMPTS, now),
                    ).fetchone()[0]
                )
                failed_waiting = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks "
                        "WHERE embedding_status='failed' AND embedding_attempts<? "
                        "AND next_retry_at>?",
                        (MAX_EMBEDDING_ATTEMPTS, now),
                    ).fetchone()[0]
                )
                failed_exhausted = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks "
                        "WHERE embedding_status='failed' AND embedding_attempts>=?",
                        (MAX_EMBEDDING_ATTEMPTS,),
                    ).fetchone()[0]
                )
                chunks_total = sum(counts.values())
                policy_counts = {
                    (str(row["embedding_policy"]), str(row["embedding_status"])): int(
                        row["count"]
                    )
                    for row in connection.execute(
                        "SELECT embedding_policy, embedding_status, COUNT(*) count "
                        "FROM knowledge_chunks GROUP BY embedding_policy, embedding_status"
                    ).fetchall()
                }
                local_total = sum(
                    count
                    for (policy, _), count in policy_counts.items()
                    if policy == "local"
                )
                prebuilt_total = sum(
                    count
                    for (policy, _), count in policy_counts.items()
                    if policy == "prebuilt_only"
                )
                local_failed_retryable_now = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks "
                        "WHERE embedding_policy='local' AND embedding_status='failed' "
                        "AND embedding_attempts<? AND next_retry_at<=?",
                        (MAX_EMBEDDING_ATTEMPTS, now),
                    ).fetchone()[0]
                )
                local_failed_waiting = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks "
                        "WHERE embedding_policy='local' AND embedding_status='failed' "
                        "AND embedding_attempts<? AND next_retry_at>?",
                        (MAX_EMBEDDING_ATTEMPTS, now),
                    ).fetchone()[0]
                )
                local_failed_exhausted = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks "
                        "WHERE embedding_policy='local' AND embedding_status='failed' "
                        "AND embedding_attempts>=?",
                        (MAX_EMBEDDING_ATTEMPTS,),
                    ).fetchone()[0]
                )
                return {
                    "entries_total": int(
                        connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                    ),
                    "entries_missing_chunks": missing,
                    "chunks_total": chunks_total,
                    "chunks_pending": counts.get("pending", 0),
                    "chunks_ready": counts.get("ready", 0),
                    "chunks_stale": counts.get("stale", 0),
                    "chunks_failed": counts.get("failed", 0),
                    "chunks_failed_retryable_now": failed_retryable_now,
                    "chunks_failed_waiting": failed_waiting,
                    "chunks_failed_exhausted": failed_exhausted,
                    "chunks_local": local_total,
                    "chunks_prebuilt_only": prebuilt_total,
                    "chunks_local_pending": policy_counts.get(("local", "pending"), 0),
                    "chunks_local_ready": policy_counts.get(("local", "ready"), 0),
                    "chunks_local_stale": policy_counts.get(("local", "stale"), 0),
                    "chunks_local_failed": policy_counts.get(("local", "failed"), 0),
                    "chunks_local_failed_retryable_now": local_failed_retryable_now,
                    "chunks_local_failed_waiting": local_failed_waiting,
                    "chunks_local_failed_exhausted": local_failed_exhausted,
                    "indexed_percent": round(
                        100.0 * counts.get("ready", 0) / chunks_total, 1
                    )
                    if chunks_total
                    else 0.0,
                    "chunks_revision": int(revision_row["value"])
                    if revision_row
                    else 0,
                }
        except KnowledgeStoreError:
            return {
                "entries_total": 0,
                "entries_missing_chunks": 0,
                "chunks_total": 0,
                "chunks_pending": 0,
                "chunks_ready": 0,
                "chunks_stale": 0,
                "chunks_failed": 0,
                "chunks_failed_retryable_now": 0,
                "chunks_failed_waiting": 0,
                "chunks_failed_exhausted": 0,
                "chunks_local": 0,
                "chunks_prebuilt_only": 0,
                "chunks_local_pending": 0,
                "chunks_local_ready": 0,
                "chunks_local_stale": 0,
                "chunks_local_failed": 0,
                "chunks_local_failed_retryable_now": 0,
                "chunks_local_failed_waiting": 0,
                "chunks_local_failed_exhausted": 0,
                "indexed_percent": 0.0,
                "chunks_revision": 0,
            }

    def embedding_policy_counts(self, *, source_tag: str = "") -> dict[str, int]:
        """Count derived chunks by generation policy without loading their text."""
        parameters: tuple[object, ...] = ()
        source_clause = ""
        if source_tag:
            source_clause = (
                " JOIN entries ON entries.rowid=knowledge_chunks.entry_rowid "
                "WHERE EXISTS (SELECT 1 FROM json_each(entries.tags) tag "
                "WHERE tag.value=?)"
            )
            parameters = (source_tag,)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT embedding_policy, COUNT(*) count FROM knowledge_chunks"
                    f"{source_clause} GROUP BY embedding_policy",
                    parameters,
                ).fetchall()
                counts = {policy: 0 for policy in EMBEDDING_POLICIES}
                counts.update(
                    {str(row["embedding_policy"]): int(row["count"]) for row in rows}
                )
                return counts
        except KnowledgeStoreError:
            return {policy: 0 for policy in EMBEDDING_POLICIES}

    def source_chunk_status(self, source_tag: str) -> dict[str, int]:
        """Return compact activation counts for one source namespace."""
        if not source_tag.startswith("source:"):
            raise ValueError("source_tag must start with source:")
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) chunks_total, "
                    "SUM(CASE WHEN embedding_status='ready' THEN 1 ELSE 0 END) "
                    "chunks_ready, "
                    "SUM(CASE WHEN embedding_policy='prebuilt_only' THEN 1 ELSE 0 END) "
                    "chunks_prebuilt_only FROM knowledge_chunks JOIN entries "
                    "ON entries.rowid=knowledge_chunks.entry_rowid WHERE EXISTS ("
                    "SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value=?)",
                    (source_tag,),
                ).fetchone()
                return {
                    "chunks_total": int(row["chunks_total"] or 0),
                    "chunks_ready": int(row["chunks_ready"] or 0),
                    "chunks_prebuilt_only": int(row["chunks_prebuilt_only"] or 0),
                }
        except KnowledgeStoreError:
            return {
                "chunks_total": 0,
                "chunks_ready": 0,
                "chunks_prebuilt_only": 0,
            }

    def set_source_embedding_policy(self, source_tag: str, policy: str) -> int:
        """Switch generation ownership for every existing chunk in one source."""
        if not source_tag.startswith("source:"):
            raise ValueError("source_tag must start with source:")
        policy = _validate_embedding_policy(policy)
        with self._connection(writable=True) as connection:
            cursor = connection.execute(
                "UPDATE knowledge_chunks SET embedding_policy=? "
                "WHERE embedding_policy<>? AND entry_rowid IN ("
                "SELECT entries.rowid FROM entries JOIN json_each(entries.tags) tag "
                "WHERE tag.value=?)",
                (policy, policy, source_tag),
            )
            changed = max(int(cursor.rowcount), 0)
            if changed:
                self._increment_chunks_revision(connection)
        if changed:
            self._notify_routing_changed()
        return changed

    def has_embedding_work(self, *, embedding_policy: str = "local") -> bool:
        """Report immediately eligible work; local generation is the safe default."""
        embedding_policy = _validate_embedding_policy(embedding_policy)
        now = int(time.time())
        try:
            with self._connection() as connection:
                return bool(
                    connection.execute(
                        "SELECT 1 FROM knowledge_chunks WHERE embedding_policy=? AND ("
                        "embedding_status IN ('pending', 'stale') OR "
                        "(embedding_status='failed' AND embedding_attempts<? "
                        "AND next_retry_at<=?)) LIMIT 1",
                        (embedding_policy, MAX_EMBEDDING_ATTEMPTS, now),
                    ).fetchone()
                )
        except KnowledgeStoreError:
            return False

    def mark_other_models_stale(
        self,
        model_id: str,
        *,
        embedding_policy: str = "local",
    ) -> int:
        if not model_id:
            return 0
        embedding_policy = _validate_embedding_policy(embedding_policy)
        with self._connection(writable=True) as connection:
            cursor = connection.execute(
                "UPDATE knowledge_chunks SET embedding_status='stale', next_retry_at=0 "
                "WHERE embedding_policy=? AND embedding_status='ready' "
                "AND embedding_model_id<>?",
                (embedding_policy, model_id),
            )
            changed = max(int(cursor.rowcount), 0)
            if changed:
                self._increment_chunks_revision(connection)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('embedding_model_id', ?)",
                (model_id,),
            )
            return changed

    def pending_embedding_chunks(
        self,
        *,
        model_id: str,
        limit: int = 32,
        include_failed: bool = True,
        embedding_policy: str = "local",
    ) -> tuple[dict[str, object], ...]:
        embedding_policy = _validate_embedding_policy(embedding_policy)
        limit = min(max(int(limit), 1), 128)
        statuses = (
            ("pending", "stale", "failed") if include_failed else ("pending", "stale")
        )
        placeholders = ",".join("?" for _ in statuses)
        now = int(time.time())
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT knowledge_chunks.*, entries.title, entries.terms, entries.tags, "
                    "entries.summary, entries.content FROM knowledge_chunks JOIN entries "
                    "ON entries.rowid=knowledge_chunks.entry_rowid "
                    f"WHERE knowledge_chunks.embedding_status IN ({placeholders}) "
                    "AND knowledge_chunks.embedding_policy=? "
                    "AND knowledge_chunks.next_retry_at<=? "
                    "AND (knowledge_chunks.embedding_status<>'failed' "
                    "OR knowledge_chunks.embedding_attempts<?) "
                    "ORDER BY knowledge_chunks.entry_rowid, knowledge_chunks.chunk_index LIMIT ?",
                    (*statuses, embedding_policy, now, MAX_EMBEDDING_ATTEMPTS, limit),
                ).fetchall()
                result: list[dict[str, object]] = []
                for row in rows:
                    try:
                        entry = _entry_from_row(row)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    result.append(
                        {
                            "chunk_id": str(row["chunk_id"]),
                            "content_hash": str(row["content_hash"]),
                            "text": knowledge_embedding_text(
                                entry,
                                heading=str(row["heading"]),
                                chunk_text=str(row["chunk_text"]),
                            ),
                            "model_id": model_id,
                        }
                    )
                return tuple(result)
        except KnowledgeStoreError:
            return ()

    def store_chunk_embedding(
        self,
        *,
        chunk_id: str,
        content_hash: str,
        model_id: str,
        dimensions: int,
        embedding: bytes,
        embedding_policy: str = "local",
    ) -> bool:
        embedding_policy = _validate_embedding_policy(embedding_policy)
        if (
            not chunk_id
            or not content_hash
            or not model_id
            or dimensions <= 0
            or not embedding
        ):
            return False
        with self._connection(writable=True) as connection:
            cursor = connection.execute(
                "UPDATE knowledge_chunks SET embedding_model_id=?, embedding_dimensions=?, "
                "embedding=?, embedding_status='ready', embedding_attempts=0, next_retry_at=0, "
                "last_error_code='' WHERE embedding_policy=? AND chunk_id=? AND content_hash=?",
                (
                    model_id,
                    dimensions,
                    embedding,
                    embedding_policy,
                    chunk_id,
                    content_hash,
                ),
            )
            changed = int(cursor.rowcount) == 1
            if changed:
                self._increment_chunks_revision(connection)
            return changed

    def store_chunk_embeddings(
        self,
        records: Sequence[dict[str, object]],
    ) -> int:
        """Compatibility alias for the strict all-or-nothing importer."""
        return self.store_chunk_embeddings_strict(records)

    def store_chunk_embeddings_strict(
        self,
        records: Sequence[dict[str, object]],
    ) -> int:
        """Atomically import vectors only when every content address still matches."""
        validated: list[tuple[str, str, str, int, bytes]] = []
        seen: set[str] = set()
        for record in records:
            chunk_id = str(record.get("chunk_id") or "")
            content_hash = str(record.get("content_hash") or "")
            model_id = str(record.get("model_id") or "")
            dimensions = int(record.get("dimensions") or 0)
            embedding = record.get("embedding")
            if (
                not chunk_id
                or chunk_id in seen
                or not content_hash
                or not model_id
                or dimensions <= 0
                or not isinstance(embedding, bytes)
                or len(embedding) != dimensions * 2
            ):
                raise ValueError("invalid or duplicate chunk embedding record")
            seen.add(chunk_id)
            validated.append((chunk_id, content_hash, model_id, dimensions, embedding))
        if not validated:
            return 0

        with self._connection(writable=True) as connection:
            for chunk_id, content_hash, model_id, dimensions, embedding in validated:
                cursor = connection.execute(
                    "UPDATE knowledge_chunks SET embedding_model_id=?, "
                    "embedding_dimensions=?, embedding=?, embedding_status='ready', "
                    "embedding_attempts=0, next_retry_at=0, last_error_code='' "
                    "WHERE chunk_id=? AND content_hash=?",
                    (model_id, dimensions, embedding, chunk_id, content_hash),
                )
                if int(cursor.rowcount) != 1:
                    raise ValueError(
                        "chunk embedding batch no longer matches current content"
                    )
            self._increment_chunks_revision(connection)
        return len(validated)

    def ready_embedding_records(
        self,
        *,
        source_tag: str = "",
    ) -> tuple[dict[str, object], ...]:
        """Return compact vectors for staging activation; never includes text."""
        try:
            with self._connection() as connection:
                source_clause = ""
                parameters: tuple[object, ...] = ()
                if source_tag:
                    if not source_tag.startswith("source:"):
                        raise ValueError("source_tag must start with source:")
                    source_clause = (
                        " AND EXISTS (SELECT 1 FROM json_each(entries.tags) tag "
                        "WHERE tag.value=?)"
                    )
                    parameters = (source_tag,)
                rows = connection.execute(
                    "SELECT knowledge_chunks.chunk_id, knowledge_chunks.content_hash, "
                    "knowledge_chunks.embedding_model_id, "
                    "knowledge_chunks.embedding_policy, "
                    "knowledge_chunks.embedding_dimensions, "
                    "knowledge_chunks.embedding FROM knowledge_chunks JOIN entries "
                    "ON entries.rowid=knowledge_chunks.entry_rowid "
                    "WHERE knowledge_chunks.embedding_status='ready'" + source_clause,
                    parameters,
                ).fetchall()
                return tuple(
                    {
                        "chunk_id": str(row["chunk_id"]),
                        "content_hash": str(row["content_hash"]),
                        "model_id": str(row["embedding_model_id"] or ""),
                        "embedding_policy": str(row["embedding_policy"]),
                        "dimensions": int(row["embedding_dimensions"] or 0),
                        "embedding": bytes(row["embedding"] or b""),
                    }
                    for row in rows
                )
        except KnowledgeStoreError:
            return ()

    def mark_chunk_embedding_failed(
        self,
        *,
        chunk_id: str,
        content_hash: str,
        error_code: str,
        embedding_policy: str = "local",
    ) -> bool:
        embedding_policy = _validate_embedding_policy(embedding_policy)
        with self._connection(writable=True) as connection:
            row = connection.execute(
                "SELECT embedding_attempts FROM knowledge_chunks "
                "WHERE embedding_policy=? AND chunk_id=? AND content_hash=?",
                (embedding_policy, chunk_id, content_hash),
            ).fetchone()
            if row is None:
                return False
            attempts = min(
                int(row["embedding_attempts"]) + 1,
                MAX_EMBEDDING_ATTEMPTS,
            )
            retry_at = int(time.time()) + min(3_600, 10 * (2 ** (attempts - 1)))
            connection.execute(
                "UPDATE knowledge_chunks SET embedding_status='failed', embedding_attempts=?, "
                "next_retry_at=?, last_error_code=? WHERE embedding_policy=? "
                "AND chunk_id=? AND content_hash=?",
                (
                    attempts,
                    retry_at,
                    str(error_code or "embedding_failed")[:80],
                    embedding_policy,
                    chunk_id,
                    content_hash,
                ),
            )
            return True

    def reset_chunk_index(
        self,
        *,
        full: bool = False,
        embedding_policy: str = "local",
    ) -> int:
        embedding_policy = _validate_embedding_policy(embedding_policy)
        with self._connection(writable=True) as connection:
            if full:
                connection.execute(
                    "DELETE FROM knowledge_chunks WHERE embedding_policy=?",
                    (embedding_policy,),
                )
                changed = int(connection.execute("SELECT changes()").fetchone()[0])
            else:
                cursor = connection.execute(
                    "UPDATE knowledge_chunks SET embedding_status='pending', embedding_model_id=NULL, "
                    "embedding_dimensions=NULL, embedding=NULL, embedding_attempts=0, "
                    "next_retry_at=0, last_error_code='' WHERE embedding_policy=?",
                    (embedding_policy,),
                )
                changed = max(int(cursor.rowcount), 0)
            if changed:
                self._increment_chunks_revision(connection)
            return changed

    def load_ready_chunks(
        self, *, model_id: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        if not model_id:
            return self.chunks_revision(), ()
        try:
            with self._connection() as connection:
                revision_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='chunks_revision'"
                ).fetchone()
                revision = int(revision_row["value"]) if revision_row else 0
                rows = connection.execute(
                    "SELECT knowledge_chunks.*, entries.title, entries.terms, entries.tags, "
                    "entries.summary, entries.content FROM knowledge_chunks JOIN entries "
                    "ON entries.rowid=knowledge_chunks.entry_rowid "
                    "WHERE embedding_status='ready' AND embedding_model_id=? "
                    "ORDER BY knowledge_chunks.entry_rowid, knowledge_chunks.chunk_index",
                    (model_id,),
                ).fetchall()
                result: list[dict[str, object]] = []
                for row in rows:
                    try:
                        entry = _entry_from_row(row)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    result.append(
                        {
                            "chunk_id": str(row["chunk_id"]),
                            "chunk_index": int(row["chunk_index"]),
                            "content_hash": str(row["content_hash"]),
                            "dimensions": int(row["embedding_dimensions"] or 0),
                            "embedding": bytes(row["embedding"] or b""),
                            "entry": entry,
                        }
                    )
                return revision, tuple(result)
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0, ()

    def load_ready_chunk_vectors(
        self,
        *,
        model_id: str,
        limit: int,
    ) -> tuple[int, tuple[dict[str, object], ...], bool]:
        """Load compact vector rows without duplicating entry text per chunk."""
        if not model_id or limit <= 0:
            return self.chunks_revision(), (), False
        limit = max(int(limit), 1)
        try:
            with self._connection() as connection:
                revision_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='chunks_revision'"
                ).fetchone()
                rows = connection.execute(
                    "SELECT entry_rowid, chunk_index, embedding_dimensions, embedding "
                    "FROM knowledge_chunks WHERE embedding_status='ready' "
                    "AND embedding_model_id=? ORDER BY entry_rowid, chunk_index LIMIT ?",
                    (model_id, limit + 1),
                ).fetchall()
                truncated = len(rows) > limit
                return (
                    int(revision_row["value"]) if revision_row else 0,
                    tuple(
                        {
                            "entry_rowid": int(row["entry_rowid"]),
                            "chunk_index": int(row["chunk_index"]),
                            "dimensions": int(row["embedding_dimensions"] or 0),
                            "embedding": bytes(row["embedding"] or b""),
                        }
                        for row in rows[:limit]
                    ),
                    truncated,
                )
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0, (), False

    def load_entries_by_rowids(
        self,
        rowids: Sequence[int],
    ) -> dict[int, KnowledgeEntry]:
        unique = tuple(dict.fromkeys(int(rowid) for rowid in rowids if int(rowid) > 0))
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT rowid, * FROM entries WHERE rowid IN ({placeholders})",
                    unique,
                ).fetchall()
                return {int(row["rowid"]): _entry_from_row(row) for row in rows}
        except (KnowledgeStoreError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def load_routing_entries(self) -> tuple[int, tuple[KnowledgeEntry, ...]]:
        """Read the database revision and routeable cards in one transaction."""
        try:
            with self._connection() as connection:
                revision_row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'entries_revision'"
                ).fetchone()
                revision = int(revision_row["value"]) if revision_row else 0
                entries: list[KnowledgeEntry] = []
                for row in connection.execute(
                    "SELECT rowid, * FROM entries ORDER BY rowid"
                ).fetchall():
                    try:
                        entries.append(_entry_from_row(row))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                return revision, tuple(entries)
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0, ()

    def integrity_ok(self) -> bool:
        try:
            with self._connection() as connection:
                return (
                    connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                )
        except KnowledgeStoreError:
            return False

    def list_active_entries(self) -> tuple[KnowledgeEntry, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT rowid, * FROM entries ORDER BY rowid"
                ).fetchall()
                return tuple(_entry_from_row(row) for row in rows)
        except (KnowledgeStoreError, TypeError, ValueError, json.JSONDecodeError):
            return ()

    def get_entry(self, source_tag: str, title: str) -> KnowledgeEntry | None:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT rowid, * FROM entries WHERE title = ? AND EXISTS "
                    "(SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value = ?) LIMIT 1",
                    (title, source_tag),
                ).fetchone()
                return _entry_from_row(row) if row is not None else None
        except (KnowledgeStoreError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def list_entries(
        self,
        *,
        source_tag: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[KnowledgeEntry, ...]:
        limit = min(max(int(limit), 1), 100)
        offset = max(int(offset), 0)
        try:
            with self._connection() as connection:
                if source_tag:
                    rows = connection.execute(
                        "SELECT rowid, * FROM entries WHERE EXISTS "
                        "(SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value = ?) "
                        "ORDER BY title LIMIT ? OFFSET ?",
                        (source_tag, limit, offset),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT rowid, * FROM entries ORDER BY title LIMIT ? OFFSET ?",
                        (limit, offset),
                    ).fetchall()
                return tuple(_entry_from_row(row) for row in rows)
        except (KnowledgeStoreError, TypeError, ValueError, json.JSONDecodeError):
            return ()

    def query_fts(
        self,
        fts_query: str,
        *,
        limit: int,
        allowed_source_tags: tuple[str, ...] | None = None,
    ):
        if allowed_source_tags is not None and not allowed_source_tags:
            return ()
        source_clause = ""
        source_parameters: tuple[str, ...] = ()
        if allowed_source_tags is not None:
            placeholders = ", ".join("?" for _ in allowed_source_tags)
            source_clause = (
                " AND EXISTS (SELECT 1 FROM json_each(entries.tags) source_filter "
                f"WHERE source_filter.value IN ({placeholders}))"
            )
            source_parameters = allowed_source_tags
        try:
            with self._connection() as connection:
                return connection.execute(
                    "SELECT entries.rowid, entries.*, bm25(entries_fts) rank "
                    "FROM entries_fts JOIN entries "
                    "ON entries.rowid = entries_fts.entry_rowid "
                    "WHERE entries_fts MATCH ?"
                    f"{source_clause} ORDER BY rank LIMIT ?",
                    (fts_query, *source_parameters, limit),
                ).fetchall()
        except (KnowledgeStoreError, sqlite3.OperationalError):
            return ()

    def query_like(
        self,
        normalized_query: str,
        *,
        limit: int,
        allowed_source_tags: tuple[str, ...] | None = None,
    ):
        if not normalized_query:
            return ()
        if allowed_source_tags is not None and not allowed_source_tags:
            return ()
        source_clause = ""
        source_parameters: tuple[str, ...] = ()
        if allowed_source_tags is not None:
            placeholders = ", ".join("?" for _ in allowed_source_tags)
            source_clause = (
                " AND EXISTS (SELECT 1 FROM json_each(entries.tags) source_filter "
                f"WHERE source_filter.value IN ({placeholders}))"
            )
            source_parameters = allowed_source_tags
        pattern = f"%{normalized_query}%"
        try:
            with self._connection() as connection:
                return connection.execute(
                    "SELECT rowid, * FROM entries WHERE ("
                    "lower(replace(replace(title, ' ', ''), '-', '')) LIKE ? "
                    "OR lower(replace(replace(terms, ' ', ''), '-', '')) LIKE ? "
                    "OR lower(replace(replace(tags, ' ', ''), '-', '')) LIKE ? "
                    "OR lower(replace(replace(content, ' ', ''), '-', '')) LIKE ? "
                    "OR lower(replace(replace(summary, ' ', ''), '-', '')) LIKE ?)"
                    f"{source_clause} LIMIT ?",
                    (
                        pattern,
                        pattern,
                        pattern,
                        pattern,
                        pattern,
                        *source_parameters,
                        limit,
                    ),
                ).fetchall()
        except (KnowledgeStoreError, sqlite3.OperationalError):
            return ()


def _terms_json(entry: KnowledgeEntry) -> str:
    return json.dumps(
        {role: list(entry.terms[role]) for role in entry.terms}, ensure_ascii=False
    )


def _validate_embedding_policy(value: str) -> str:
    policy = str(value or "").strip()
    if policy not in EMBEDDING_POLICIES:
        raise ValueError(f"unsupported embedding policy: {policy or '<empty>'}")
    return policy


def _values_json(values: Sequence[str]) -> str:
    return json.dumps(tuple(values), ensure_ascii=False)


def _terms_search_text(entry: KnowledgeEntry) -> str:
    return " ".join(value for role in entry.terms.values() for value in role)


def _json_values(value: str) -> tuple[str, ...]:
    raw = json.loads(value or "[]")
    return (
        tuple(item for item in raw if isinstance(item, str))
        if isinstance(raw, list)
        else ()
    )


def _entry_from_row(row: sqlite3.Row) -> KnowledgeEntry:
    raw_terms = json.loads(row["terms"])
    return KnowledgeEntry(
        title=row["title"],
        terms=raw_terms if isinstance(raw_terms, dict) else {},
        tags=_json_values(row["tags"]),
        summary=row["summary"],
        content=row["content"],
    )


def _database_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_dev), int(stat.st_ino)
