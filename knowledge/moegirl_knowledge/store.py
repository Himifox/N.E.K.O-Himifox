"""SQLite persistence for the independently versioned Moegirl knowledge base."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .models import MoegirlKnowledgeEntry, UpsertResult


SCHEMA_VERSION = 2


class KnowledgeStoreError(RuntimeError):
    """Raised for write-side knowledge-store failures."""


class MoegirlKnowledgeStore:
    """Own one SQLite/FTS5 database without touching any character memory store."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connection(self, *, writable: bool = False) -> Iterator[sqlite3.Connection]:
        if writable:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if writable:
                connection.execute("PRAGMA journal_mode=WAL")
            self._initialize(connection)
            yield connection
            if writable:
                connection.commit()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeStoreError(f"knowledge database is unavailable: {exc}") from exc
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL,
                source_page_id INTEGER,
                source_license TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                synced_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS entries_source_page_id_index
                ON entries(source_page_id) WHERE source_page_id IS NOT NULL;
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                entry_id UNINDEXED,
                title,
                aliases,
                tags,
                content,
                summary,
                tokenize='unicode61'
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # Page IDs are only unique inside one wiki.  Keep the old index migration
        # here so existing local databases can accept Moegirl and Wikipedia pages
        # with the same numeric page ID.
        connection.execute("DROP INDEX IF EXISTS entries_source_page_id_unique")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS entries_source_page_id_index "
            "ON entries(source_page_id) WHERE source_page_id IS NOT NULL"
        )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )

    def upsert(self, entry: MoegirlKnowledgeEntry) -> UpsertResult:
        with self._connection(writable=True) as connection:
            result = self._upsert_with_connection(connection, entry)
            if not result.unchanged:
                self._increment_entries_revision(connection)
            return result

    def upsert_many(self, entries: Sequence[MoegirlKnowledgeEntry]) -> tuple[UpsertResult, ...]:
        """Atomically update a trusted, bundled dataset and its FTS rows."""
        with self._connection(writable=True) as connection:
            results = tuple(self._upsert_with_connection(connection, entry) for entry in entries)
            if any(not result.unchanged for result in results):
                self._increment_entries_revision(connection)
            return results

    @staticmethod
    def _increment_entries_revision(connection: sqlite3.Connection) -> None:
        """Invalidate process-local mention indexes after a committed write batch."""
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('entries_revision', '0')"
        )
        connection.execute(
            "UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'entries_revision'"
        )

    @staticmethod
    def _upsert_with_connection(
        connection: sqlite3.Connection, entry: MoegirlKnowledgeEntry
    ) -> UpsertResult:
        existing = connection.execute(
            "SELECT content_hash FROM entries WHERE id = ?", (entry.id,)
        ).fetchone()
        if existing is not None and existing["content_hash"] == entry.content_hash:
            return UpsertResult(entry.id, unchanged=True)
        aliases = json.dumps(entry.aliases, ensure_ascii=False)
        tags = json.dumps(entry.tags, ensure_ascii=False)
        connection.execute(
            """
            INSERT INTO entries(
                id, title, aliases, tags, content, summary, source_url, source_page_id,
                source_license, content_hash, synced_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, aliases=excluded.aliases, tags=excluded.tags,
                content=excluded.content, summary=excluded.summary,
                source_url=excluded.source_url, source_page_id=excluded.source_page_id,
                source_license=excluded.source_license, content_hash=excluded.content_hash,
                synced_at=excluded.synced_at, status=excluded.status
            """,
            (
                entry.id, entry.title, aliases, tags, entry.content, entry.summary,
                entry.source_url, entry.source_page_id, entry.source_license,
                entry.content_hash, entry.synced_at, entry.status,
            ),
        )
        connection.execute("DELETE FROM entries_fts WHERE entry_id = ?", (entry.id,))
        connection.execute(
            """
            INSERT INTO entries_fts(entry_id, title, aliases, tags, content, summary)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entry.id, entry.title, " ".join(entry.aliases), " ".join(entry.tags), entry.content, entry.summary),
        )
        return UpsertResult(entry.id, created=existing is None, updated=existing is not None)

    def get(self, entry_id: str) -> MoegirlKnowledgeEntry | None:
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
                return _entry_from_row(row) if row is not None else None
        except (KnowledgeStoreError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def count(self) -> int:
        try:
            with self._connection() as connection:
                return int(connection.execute("SELECT COUNT(*) FROM entries WHERE status = 'active'").fetchone()[0])
        except KnowledgeStoreError:
            return 0

    def entries_revision(self) -> int:
        """Return a cheap revision token for process-local read caches."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'entries_revision'"
                ).fetchone()
                return int(row["value"]) if row is not None else 0
        except (KnowledgeStoreError, TypeError, ValueError):
            return 0

    def list_active_entries(self) -> tuple[MoegirlKnowledgeEntry, ...]:
        """Load active records for a rebuildable in-process phrase index."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM entries WHERE status = 'active' ORDER BY id"
                ).fetchall()
        except KnowledgeStoreError:
            return ()
        entries: list[MoegirlKnowledgeEntry] = []
        for row in rows:
            try:
                entries.append(_entry_from_row(row))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(entries)

    def count_by_id_prefix(self, prefix: str) -> int:
        """Count active entries for one trusted source namespace."""
        if not prefix:
            return 0
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            with self._connection() as connection:
                return int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entries WHERE status = 'active' AND id LIKE ? ESCAPE '\\'",
                        (f"{escaped_prefix}%",),
                    ).fetchone()[0]
                )
        except KnowledgeStoreError:
            return 0

    def integrity_ok(self) -> bool:
        try:
            with self._connection() as connection:
                return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except KnowledgeStoreError:
            return False

    def query_fts(self, fts_query: str, *, limit: int) -> Sequence[sqlite3.Row]:
        try:
            with self._connection() as connection:
                return connection.execute(
                    """
                    SELECT entries.*, bm25(entries_fts) AS rank
                    FROM entries_fts
                    JOIN entries ON entries.id = entries_fts.entry_id
                    WHERE entries_fts MATCH ? AND entries.status = 'active'
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        except (KnowledgeStoreError, sqlite3.OperationalError):
            return ()

    def query_like(self, normalized_query: str, *, limit: int) -> Sequence[sqlite3.Row]:
        if not normalized_query:
            return ()
        pattern = f"%{normalized_query}%"
        try:
            with self._connection() as connection:
                return connection.execute(
                    """
                    SELECT * FROM entries
                    WHERE status = 'active'
                      AND (
                        lower(replace(replace(title, ' ', ''), '-', '')) LIKE ?
                        OR lower(replace(replace(aliases, ' ', ''), '-', '')) LIKE ?
                        OR lower(replace(replace(tags, ' ', ''), '-', '')) LIKE ?
                        OR lower(replace(replace(content, ' ', ''), '-', '')) LIKE ?
                        OR lower(replace(replace(summary, ' ', ''), '-', '')) LIKE ?
                      )
                    LIMIT ?
                    """,
                    (pattern, pattern, pattern, pattern, pattern, limit),
                ).fetchall()
        except KnowledgeStoreError:
            return ()

    def query_title_mentions(self, normalized_text: str, *, limit: int) -> Sequence[sqlite3.Row]:
        """Find source titles explicitly contained in one user utterance."""
        if len(normalized_text) < 2 or limit <= 0:
            return ()
        try:
            with self._connection() as connection:
                return connection.execute(
                    """
                    SELECT * FROM entries
                    WHERE status = 'active'
                      AND length(lower(replace(replace(title, ' ', ''), '-', ''))) >= 2
                      AND instr(?, lower(replace(replace(title, ' ', ''), '-', ''))) > 0
                    ORDER BY length(lower(replace(replace(title, ' ', ''), '-', ''))) DESC, title
                    LIMIT ?
                    """,
                    (normalized_text, limit),
                ).fetchall()
        except KnowledgeStoreError:
            return ()

    def query_alias_mentions(self, normalized_text: str, *, limit: int) -> Sequence[sqlite3.Row]:
        """Find a precomputed, source-scoped phrase alias in a user utterance."""
        if len(normalized_text) < 2 or limit <= 0:
            return ()
        try:
            with self._connection() as connection:
                return connection.execute(
                    """
                    SELECT DISTINCT entries.* FROM entries
                    JOIN json_each(entries.aliases) AS alias
                    WHERE status = 'active'
                      AND instr(?, lower(replace(replace(alias.value, ' ', ''), '-', ''))) > 0
                    ORDER BY length(lower(replace(replace(alias.value, ' ', ''), '-', ''))) DESC, title
                    LIMIT ?
                    """,
                    (normalized_text, limit),
                ).fetchall()
        except (KnowledgeStoreError, sqlite3.OperationalError):
            return ()


def _entry_from_row(row: sqlite3.Row) -> MoegirlKnowledgeEntry:
    return MoegirlKnowledgeEntry(
        id=row["id"], title=row["title"], content=row["content"], source_url=row["source_url"],
        source_page_id=row["source_page_id"], aliases=tuple(json.loads(row["aliases"])),
        tags=tuple(json.loads(row["tags"])), summary=row["summary"],
        source_license=row["source_license"], content_hash=row["content_hash"],
        synced_at=row["synced_at"], status=row["status"],
    )
