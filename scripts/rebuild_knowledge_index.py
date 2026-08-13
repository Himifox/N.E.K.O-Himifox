"""Inspect or rebuild the local public-knowledge vector index.

Status and dry-run modes open SQLite databases read-only and never migrate
them.  Rebuild modes use the knowledge-owned local embedding runtime; they do
not call or modify Memory Server APIs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


COLLECTION_DATABASES = {
    "meme": Path("moegirl-knowledge") / "knowledge.db",
    "corpora": Path("corpora") / "knowledge.db",
}
DEFAULT_BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class CollectionTarget:
    collection_id: str
    database_path: Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or rebuild the local hybrid knowledge index.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status",
        action="store_true",
        help="show read-only index status (the default action)",
    )
    action.add_argument(
        "--rebuild",
        action="store_true",
        help="process missing, pending, stale, and retryable failed chunks",
    )
    action.add_argument(
        "--full",
        action="store_true",
        help="discard all derived chunks and rebuild them from source entries",
    )
    parser.add_argument(
        "--collection",
        choices=("meme", "corpora", "all"),
        default="all",
        help="limit work to one collection (default: all)",
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help="embedding batch size from 1 to 128 (default: 32)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate the affected entries and chunks without writing or embedding",
    )
    parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="override the application's knowledge directory",
    )
    parser.add_argument(
        "--local-model",
        action="store_true",
        help=(
            "explicitly select the local shared embedding runtime; retained for "
            "clarity because it is the only v1 rebuild backend"
        ),
    )
    return parser


def _batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be an integer") from exc
    if not 1 <= parsed <= 128:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 128")
    return parsed


def _default_knowledge_root() -> Path:
    from utils.config_manager import get_config_manager

    return Path(get_config_manager(migrate=False).knowledge_dir)


def _targets(root: Path, collection: str) -> tuple[CollectionTarget, ...]:
    collection_ids = COLLECTION_DATABASES if collection == "all" else (collection,)
    return tuple(
        CollectionTarget(collection_id, root / COLLECTION_DATABASES[collection_id])
        for collection_id in collection_ids
    )


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    )


def inspect_database(database_path: Path) -> dict[str, Any]:
    """Return index counts without creating, migrating, or writing the database."""
    result: dict[str, Any] = {
        "database": str(database_path),
        "database_exists": database_path.is_file(),
        "schema_version": 0,
        "entries_total": 0,
        "entries_missing_chunks": 0,
        "chunks_total": 0,
        "chunks_pending": 0,
        "chunks_ready": 0,
        "chunks_stale": 0,
        "chunks_failed": 0,
        "chunks_failed_retryable": 0,
        "indexed_percent": 0.0,
        "embedding_model_id": "",
    }
    if not database_path.is_file():
        return result
    try:
        with _open_read_only(database_path) as connection:
            tables = _table_names(connection)
            if "metadata" in tables:
                metadata = {
                    str(row["key"]): str(row["value"])
                    for row in connection.execute("SELECT key, value FROM metadata")
                }
                try:
                    result["schema_version"] = int(metadata.get("schema_version", 0))
                except ValueError:
                    result["schema_version"] = 0
                result["embedding_model_id"] = metadata.get("embedding_model_id", "")
            if "entries" in tables:
                result["entries_total"] = int(
                    connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                )
            if "knowledge_chunks" not in tables:
                result["entries_missing_chunks"] = result["entries_total"]
                return result

            counts = {
                str(row["embedding_status"]): int(row["entry_count"])
                for row in connection.execute(
                    "SELECT embedding_status, COUNT(*) entry_count "
                    "FROM knowledge_chunks GROUP BY embedding_status"
                )
            }
            chunks_total = sum(counts.values())
            result.update({
                "chunks_total": chunks_total,
                "chunks_pending": counts.get("pending", 0),
                "chunks_ready": counts.get("ready", 0),
                "chunks_stale": counts.get("stale", 0),
                "chunks_failed": counts.get("failed", 0),
                "indexed_percent": (
                    round(100.0 * counts.get("ready", 0) / chunks_total, 1)
                    if chunks_total
                    else 0.0
                ),
            })
            result["chunks_failed_retryable"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks "
                    "WHERE embedding_status='failed' AND next_retry_at<=?",
                    (int(time.time()),),
                ).fetchone()[0]
            )
            if "entries" in tables:
                result["entries_missing_chunks"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entries WHERE NOT EXISTS ("
                        "SELECT 1 FROM knowledge_chunks "
                        "WHERE knowledge_chunks.entry_rowid=entries.rowid)"
                    ).fetchone()[0]
                )
    except sqlite3.DatabaseError as exc:
        result["error_type"] = type(exc).__name__
    return result


def _count_derived_chunks(database_path: Path) -> tuple[int, int]:
    """Count valid entries and deterministic v1 chunks using read-only data."""
    from knowledge.chunking import derive_knowledge_chunks
    from knowledge.moegirl_knowledge.models import MoegirlKnowledgeEntry

    if not database_path.is_file():
        return 0, 0
    entries = 0
    chunks = 0
    try:
        with _open_read_only(database_path) as connection:
            if "entries" not in _table_names(connection):
                return 0, 0
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(entries)").fetchall()
            }
            if not {"title", "terms", "tags", "summary", "content"}.issubset(columns):
                return 0, 0
            for row in connection.execute(
                "SELECT rowid, title, terms, tags, summary, content FROM entries "
                "ORDER BY rowid"
            ):
                try:
                    entry = MoegirlKnowledgeEntry(
                        title=str(row["title"]),
                        terms=json.loads(str(row["terms"])),
                        tags=tuple(json.loads(str(row["tags"]))),
                        summary=str(row["summary"]),
                        content=str(row["content"]),
                    )
                    derived = derive_knowledge_chunks(
                        entry,
                        entry_key=f"{entry.source_tag}\0{entry.title}",
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                entries += 1
                chunks += len(derived)
    except sqlite3.DatabaseError:
        return 0, 0
    return entries, chunks


def dry_run_plan(target: CollectionTarget, *, full: bool) -> dict[str, Any]:
    status = inspect_database(target.database_path)
    valid_entries, derived_chunks = _count_derived_chunks(target.database_path)
    if full:
        affected_chunks = derived_chunks
        affected_entries = valid_entries
    else:
        affected_chunks = (
            int(status["chunks_pending"])
            + int(status["chunks_stale"])
            + int(status["chunks_failed_retryable"])
        )
        affected_entries = int(status["entries_missing_chunks"])
        if affected_entries:
            # Missing rows are part of the deterministic derived total, but the
            # exact per-row chunk count would require duplicating Store queries.
            affected_chunks += max(derived_chunks - int(status["chunks_total"]), 0)
    return {
        **status,
        "valid_entries": valid_entries,
        "derived_chunks_after_rebuild": derived_chunks,
        "affected_entries": affected_entries,
        "affected_chunks": affected_chunks,
        "action": "full" if full else "rebuild",
        "dry_run": True,
    }


def _backfill_all(store: Any, *, batch_size: int) -> int:
    processed = 0
    while True:
        count = store.backfill_missing_chunks(limit=max(batch_size, 64))
        processed += count
        if count == 0:
            return processed


def _eligible_chunk_count(status: dict[str, Any]) -> int:
    return (
        int(status["chunks_pending"])
        + int(status["chunks_stale"])
        + int(status["chunks_failed_retryable"])
    )


async def rebuild_target(
    target: CollectionTarget,
    *,
    full: bool,
    batch_size: int,
) -> tuple[dict[str, Any], bool]:
    """Reconcile chunks and generate all currently eligible embeddings."""
    from knowledge.moegirl_knowledge.store import MoegirlKnowledgeStore
    from knowledge.vector_index import index_embedding_batch
    from utils.local_embedding_runtime import get_local_embedding_status

    before = inspect_database(target.database_path)
    if not target.database_path.is_file():
        return {**before, "action": "skipped", "reason": "database_missing"}, True

    store = MoegirlKnowledgeStore(target.database_path)
    reset_chunks = store.reset_chunk_index(full=True) if full else 0
    backfilled_entries = await asyncio.to_thread(
        _backfill_all,
        store,
        batch_size=batch_size,
    )

    embedded_chunks = 0
    while True:
        eligible_before = _eligible_chunk_count(inspect_database(target.database_path))
        if eligible_before == 0:
            break
        stored = await index_embedding_batch(
            store,
            batch_size=batch_size,
            load_model=True,
        )
        embedded_chunks += stored
        eligible_after = _eligible_chunk_count(inspect_database(target.database_path))
        if stored == 0 and eligible_after >= eligible_before:
            break
        await asyncio.sleep(0)

    after = inspect_database(target.database_path)
    embedding_status = get_local_embedding_status()
    eligible_remaining = _eligible_chunk_count(after)
    complete = eligible_remaining == 0
    return {
        **after,
        "action": "full" if full else "rebuild",
        "reset_chunks": reset_chunks,
        "backfilled_entries": backfilled_entries,
        "embedded_chunks": embedded_chunks,
        "eligible_chunks_remaining": eligible_remaining,
        "embedding_service_state": embedding_status.state,
        "runtime_model_id": embedding_status.model_id,
        "runtime_dimensions": embedding_status.dimensions,
        "complete": complete,
    }, complete


async def _run(args: argparse.Namespace) -> int:
    root = (args.knowledge_root or _default_knowledge_root()).expanduser().resolve()
    targets = _targets(root, args.collection)
    requested_action = "full" if args.full else "rebuild" if args.rebuild else "status"
    payload: dict[str, Any] = {
        "action": requested_action,
        "knowledge_root": str(root),
        "embedding_backend": "local_shared_runtime",
        "collections": [],
    }
    if requested_action == "status":
        payload["collections"] = [
            {"collection_id": target.collection_id, **inspect_database(target.database_path)}
            for target in targets
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        payload["collections"] = [
            {"collection_id": target.collection_id, **dry_run_plan(target, full=args.full)}
            for target in targets
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    all_complete = True
    try:
        for target in targets:
            result, complete = await rebuild_target(
                target,
                full=args.full,
                batch_size=args.batch_size,
            )
            payload["collections"].append({
                "collection_id": target.collection_id,
                **result,
            })
            all_complete = all_complete and complete
    finally:
        from utils.local_embedding_runtime import release_local_embedding_service

        await release_local_embedding_service()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all_complete else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
