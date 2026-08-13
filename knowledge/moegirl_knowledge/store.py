"""SQLite persistence for five-field public knowledge cards."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from knowledge.chunking import (
    CHUNKER_VERSION,
    derive_knowledge_chunks,
    knowledge_embedding_text,
)

from .models import MoegirlKnowledgeEntry, UpsertResult


SCHEMA_VERSION = 6
_INITIALIZED_DATABASES: dict[str, tuple[int, int] | None] = {}
_INITIALIZE_LOCK = threading.Lock()


class KnowledgeStoreError(RuntimeError):
    pass


class MoegirlKnowledgeStore:
    """Own a small rebuildable database without touching character memory."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

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
            raise KnowledgeStoreError(f"knowledge database is unavailable: {exc}") from exc
        finally:
            if "connection" in locals():
                connection.close()

    def _open_connection(self, *, writable: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        if writable:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
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
            connection.execute("UPDATE entries SET tags=? WHERE rowid=?", (_values_json(tags), row["rowid"]))
            connection.execute("UPDATE entries_fts SET tags=? WHERE entry_rowid=?", (" ".join(tags), row["rowid"]))
            changed = True
        if changed:
            MoegirlKnowledgeStore._increment_entries_revision(connection)

    def _migrate_legacy_entries(self, connection: sqlite3.Connection) -> None:
        """Copy the old attributed schema into a five-field table once.

        The external backup is intentionally retained.  The guide importer later
        replaces its source slice from the original export, removing historical
        tag-as-alias pollution.
        """
        backup = self.database_path.with_suffix(self.database_path.suffix + ".legacy.bak")
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
            aliases = () if "source:geng-guide" in tags else _json_values(row["aliases"])
            terms = {"alias": list(aliases), "recognition": []}
            connection.execute(
                "INSERT INTO entries(title, terms, tags, summary, content) VALUES (?, ?, ?, ?, ?)",
                (
                    row["title"], json.dumps(terms, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False), row["summary"], row["content"],
                ),
            )
        connection.execute("DROP TABLE entries_legacy")

    def upsert(self, entry: MoegirlKnowledgeEntry) -> UpsertResult:
        with self._connection(writable=True) as connection:
            result = self._upsert_with_connection(connection, entry)
            if not result.unchanged:
                self._increment_entries_revision(connection)
        if not result.unchanged:
            self._notify_routing_changed()
        return result

    def upsert_many(self, entries: Sequence[MoegirlKnowledgeEntry]) -> tuple[UpsertResult, ...]:
        with self._connection(writable=True) as connection:
            results = tuple(self._upsert_with_connection(connection, entry) for entry in entries)
            if any(not result.unchanged for result in results):
                self._increment_entries_revision(connection)
        if any(not result.unchanged for result in results):
            self._notify_routing_changed()
        return results

    def replace_source(self, source_tag: str, entries: Sequence[MoegirlKnowledgeEntry]) -> tuple[UpsertResult, ...]:
        """Atomically replace a fixed bundled/imported source namespace."""
        if not source_tag.startswith("source:") or any(entry.source_tag != source_tag for entry in entries):
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
                self._upsert_with_connection(connection, entry) for entry in entries
            )
            changed = bool(removed_rowids) or any(not result.unchanged for result in results)
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
    def _entry_key(entry: MoegirlKnowledgeEntry) -> str:
        return f"{entry.source_tag}:{entry.title}"

    def _upsert_with_connection(self, connection: sqlite3.Connection, entry: MoegirlKnowledgeEntry) -> UpsertResult:
        rows = connection.execute(
            "SELECT rowid, * FROM entries WHERE title = ? AND EXISTS (SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value = ?)",
            (entry.title, entry.source_tag),
        ).fetchall()
        if len(rows) == 1:
            existing = _entry_from_row(rows[0])
            if existing.content_hash == entry.content_hash:
                return UpsertResult(self._entry_key(entry), unchanged=True)
            rowid = rows[0]["rowid"]
            connection.execute(
                "UPDATE entries SET terms=?, tags=?, summary=?, content=? WHERE rowid=?",
                (_terms_json(entry), _values_json(entry.tags), entry.summary, entry.content, rowid),
            )
            self._replace_fts(connection, rowid, entry)
            self._reconcile_chunks(connection, rowid, entry)
            return UpsertResult(self._entry_key(entry), updated=True)
        return self._insert_with_connection(connection, entry)

    def _insert_with_connection(self, connection: sqlite3.Connection, entry: MoegirlKnowledgeEntry) -> UpsertResult:
        cursor = connection.execute(
            "INSERT INTO entries(title, terms, tags, summary, content) VALUES (?, ?, ?, ?, ?)",
            (entry.title, _terms_json(entry), _values_json(entry.tags), entry.summary, entry.content),
        )
        rowid = int(cursor.lastrowid)
        self._replace_fts(connection, rowid, entry)
        self._reconcile_chunks(connection, rowid, entry)
        return UpsertResult(self._entry_key(entry), created=True)

    @staticmethod
    def _replace_fts(connection: sqlite3.Connection, rowid: int, entry: MoegirlKnowledgeEntry) -> None:
        connection.execute("DELETE FROM entries_fts WHERE entry_rowid = ?", (rowid,))
        connection.execute(
            "INSERT INTO entries_fts(entry_rowid, title, terms, tags, summary, content) VALUES (?, ?, ?, ?, ?, ?)",
            (rowid, entry.title, _terms_search_text(entry), " ".join(entry.tags), entry.summary, entry.content),
        )

    @classmethod
    def _reconcile_chunks(
        cls,
        connection: sqlite3.Connection,
        rowid: int,
        entry: MoegirlKnowledgeEntry,
    ) -> bool:
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
                "embedding_model_id, embedding_dimensions, embedding, embedding_status, "
                "embedding_attempts, next_retry_at, last_error_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.chunk_id,
                    rowid,
                    chunk.chunk_index,
                    chunk.heading,
                    chunk.chunk_text,
                    chunk.content_hash,
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
        connection.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('entries_revision', '0')")
        connection.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'entries_revision'")

    @staticmethod
    def _increment_chunks_revision(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('chunks_revision', '0')")
        connection.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'chunks_revision'")

    def count(self) -> int:
        try:
            with self._connection() as connection:
                return int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        except KnowledgeStoreError:
            return 0

    def count_by_source_tag(self, source_tag: str) -> int:
        try:
            with self._connection() as connection:
                return int(connection.execute(
                    "SELECT COUNT(*) FROM entries WHERE EXISTS (SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value = ?)",
                    (source_tag,),
                ).fetchone()[0])
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
                return tuple({
                    "tag": str(row["source_tag"]),
                    "entries": int(row["entry_count"]),
                } for row in rows)
        except KnowledgeStoreError:
            return ()

    def entries_revision(self) -> int:
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT value FROM metadata WHERE key = 'entries_revision'").fetchone()
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

    def backfill_missing_chunks(self, *, limit: int = 64) -> int:
        """Derive chunks for legacy rows without doing model inference."""
        limit = min(max(int(limit), 1), 256)
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
                self._reconcile_chunks(connection, int(row["rowid"]), entry)
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
                missing = int(connection.execute(
                    "SELECT COUNT(*) FROM entries WHERE NOT EXISTS ("
                    "SELECT 1 FROM knowledge_chunks WHERE entry_rowid=entries.rowid)"
                ).fetchone()[0])
                revision_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='chunks_revision'"
                ).fetchone()
                chunks_total = sum(counts.values())
                return {
                    "entries_total": int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]),
                    "entries_missing_chunks": missing,
                    "chunks_total": chunks_total,
                    "chunks_pending": counts.get("pending", 0),
                    "chunks_ready": counts.get("ready", 0),
                    "chunks_stale": counts.get("stale", 0),
                    "chunks_failed": counts.get("failed", 0),
                    "indexed_percent": round(100.0 * counts.get("ready", 0) / chunks_total, 1)
                    if chunks_total else 0.0,
                    "chunks_revision": int(revision_row["value"]) if revision_row else 0,
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
                "indexed_percent": 0.0,
                "chunks_revision": 0,
            }

    def mark_other_models_stale(self, model_id: str) -> int:
        if not model_id:
            return 0
        with self._connection(writable=True) as connection:
            cursor = connection.execute(
                "UPDATE knowledge_chunks SET embedding_status='stale', next_retry_at=0 "
                "WHERE embedding_status='ready' AND embedding_model_id<>?",
                (model_id,),
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
    ) -> tuple[dict[str, object], ...]:
        limit = min(max(int(limit), 1), 128)
        statuses = ("pending", "stale", "failed") if include_failed else ("pending", "stale")
        placeholders = ",".join("?" for _ in statuses)
        now = int(time.time())
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT knowledge_chunks.*, entries.title, entries.terms, entries.tags, "
                    "entries.summary, entries.content FROM knowledge_chunks JOIN entries "
                    "ON entries.rowid=knowledge_chunks.entry_rowid "
                    f"WHERE knowledge_chunks.embedding_status IN ({placeholders}) "
                    "AND knowledge_chunks.next_retry_at<=? "
                    "ORDER BY knowledge_chunks.entry_rowid, knowledge_chunks.chunk_index LIMIT ?",
                    (*statuses, now, limit),
                ).fetchall()
                result: list[dict[str, object]] = []
                for row in rows:
                    try:
                        entry = _entry_from_row(row)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    result.append({
                        "chunk_id": str(row["chunk_id"]),
                        "content_hash": str(row["content_hash"]),
                        "text": knowledge_embedding_text(
                            entry,
                            heading=str(row["heading"]),
                            chunk_text=str(row["chunk_text"]),
                        ),
                        "model_id": model_id,
                    })
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
    ) -> bool:
        if not chunk_id or not content_hash or not model_id or dimensions <= 0 or not embedding:
            return False
        with self._connection(writable=True) as connection:
            cursor = connection.execute(
                "UPDATE knowledge_chunks SET embedding_model_id=?, embedding_dimensions=?, "
                "embedding=?, embedding_status='ready', embedding_attempts=0, next_retry_at=0, "
                "last_error_code='' WHERE chunk_id=? AND content_hash=?",
                (model_id, dimensions, embedding, chunk_id, content_hash),
            )
            changed = int(cursor.rowcount) == 1
            if changed:
                self._increment_chunks_revision(connection)
            return changed

    def mark_chunk_embedding_failed(
        self,
        *,
        chunk_id: str,
        content_hash: str,
        error_code: str,
    ) -> bool:
        with self._connection(writable=True) as connection:
            row = connection.execute(
                "SELECT embedding_attempts FROM knowledge_chunks "
                "WHERE chunk_id=? AND content_hash=?",
                (chunk_id, content_hash),
            ).fetchone()
            if row is None:
                return False
            attempts = min(int(row["embedding_attempts"]) + 1, 8)
            retry_at = int(time.time()) + min(3_600, 10 * (2 ** (attempts - 1)))
            connection.execute(
                "UPDATE knowledge_chunks SET embedding_status='failed', embedding_attempts=?, "
                "next_retry_at=?, last_error_code=? WHERE chunk_id=? AND content_hash=?",
                (attempts, retry_at, str(error_code or "embedding_failed")[:80], chunk_id, content_hash),
            )
            return True

    def reset_chunk_index(self, *, full: bool = False) -> int:
        with self._connection(writable=True) as connection:
            if full:
                connection.execute("DELETE FROM knowledge_chunks")
                changed = int(connection.execute("SELECT changes()").fetchone()[0])
            else:
                cursor = connection.execute(
                    "UPDATE knowledge_chunks SET embedding_status='pending', embedding_model_id=NULL, "
                    "embedding_dimensions=NULL, embedding=NULL, embedding_attempts=0, "
                    "next_retry_at=0, last_error_code=''"
                )
                changed = max(int(cursor.rowcount), 0)
            if changed:
                self._increment_chunks_revision(connection)
            return changed

    def load_ready_chunks(self, *, model_id: str) -> tuple[int, tuple[dict[str, object], ...]]:
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
                    result.append({
                        "chunk_id": str(row["chunk_id"]),
                        "chunk_index": int(row["chunk_index"]),
                        "content_hash": str(row["content_hash"]),
                        "dimensions": int(row["embedding_dimensions"] or 0),
                        "embedding": bytes(row["embedding"] or b""),
                        "entry": entry,
                    })
                return revision, tuple(result)
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0, ()

    def load_routing_entries(self) -> tuple[int, tuple[MoegirlKnowledgeEntry, ...]]:
        """Read one collection revision and its routeable cards in one transaction."""
        try:
            with self._connection() as connection:
                revision_row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'entries_revision'"
                ).fetchone()
                revision = int(revision_row["value"]) if revision_row else 0
                entries: list[MoegirlKnowledgeEntry] = []
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
                return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except KnowledgeStoreError:
            return False

    def list_active_entries(self) -> tuple[MoegirlKnowledgeEntry, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute("SELECT rowid, * FROM entries ORDER BY rowid").fetchall()
                return tuple(_entry_from_row(row) for row in rows)
        except (KnowledgeStoreError, TypeError, ValueError, json.JSONDecodeError):
            return ()

    def get_entry(self, source_tag: str, title: str) -> MoegirlKnowledgeEntry | None:
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
    ) -> tuple[MoegirlKnowledgeEntry, ...]:
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


def _terms_json(entry: MoegirlKnowledgeEntry) -> str:
    return json.dumps({role: list(entry.terms[role]) for role in entry.terms}, ensure_ascii=False)


def _values_json(values: Sequence[str]) -> str:
    return json.dumps(tuple(values), ensure_ascii=False)


def _terms_search_text(entry: MoegirlKnowledgeEntry) -> str:
    return " ".join(value for role in entry.terms.values() for value in role)


def _json_values(value: str) -> tuple[str, ...]:
    raw = json.loads(value or "[]")
    return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()


def _entry_from_row(row: sqlite3.Row) -> MoegirlKnowledgeEntry:
    raw_terms = json.loads(row["terms"])
    return MoegirlKnowledgeEntry(
        title=row["title"], terms=raw_terms if isinstance(raw_terms, dict) else {},
        tags=_json_values(row["tags"]), summary=row["summary"], content=row["content"],
    )


def _database_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_dev), int(stat.st_ino)
