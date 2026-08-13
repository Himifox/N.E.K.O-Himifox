"""Progressive background indexing owned by the public-knowledge domain."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path


logger = logging.getLogger("N.E.K.O.Knowledge.Indexer")

STARTUP_DELAY_SECONDS = 45.0
BACKLOG_DELAY_SECONDS = 10.0
IDLE_DELAY_SECONDS = 60.0
EMBEDDING_BATCH_SIZE = 32
MAX_CHUNKS_PER_ROUND = 64

_STATE_LOCK = threading.Lock()
_TASK: asyncio.Task[None] | None = None
_WAKE_EVENT: asyncio.Event | None = None
_EVENT_LOOP: asyncio.AbstractEventLoop | None = None


def _rss_bytes() -> int | None:
    """Return aggregate process RSS without making diagnostics a dependency."""
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def _backfill_store(store: object, *, limit: int) -> int:
    from ._mutation_lock import mutation_lock

    with mutation_lock(store.database_path):
        return int(store.backfill_missing_chunks(limit=limit))


async def _wait_for_wake(event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        event.clear()


async def _run_indexer(knowledge_root: Path, wake_event: asyncio.Event) -> None:
    """Backfill chunks, then infer vectors outside every SQLite transaction."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)

    from .moegirl_knowledge.store import MoegirlKnowledgeStore
    from .service import BUILTIN_COLLECTIONS, KnowledgeService
    from .vector_index import index_embedding_batch

    service = KnowledgeService.from_root(knowledge_root)
    memory_baseline = _rss_bytes()
    memory_delta_reported = False

    while True:
        stores = [
            MoegirlKnowledgeStore(database_path)
            for spec in BUILTIN_COLLECTIONS
            if (database_path := service.database_path(spec.collection_id)).is_file()
        ]
        backlog = False
        try:
            remaining = MAX_CHUNKS_PER_ROUND
            for store in stores:
                if remaining < 32:
                    break
                while remaining >= 32:
                    # One entry creates at most 32 chunks. SQLite work is short and
                    # inference never runs in this worker thread or transaction.
                    before = await asyncio.to_thread(store.chunk_status)
                    derived_entries = await asyncio.to_thread(
                        _backfill_store,
                        store,
                        limit=1,
                    )
                    if derived_entries == 0:
                        break
                    after = await asyncio.to_thread(store.chunk_status)
                    derived_chunks = max(
                        int(after.get("chunks_total", 0))
                        - int(before.get("chunks_total", 0)),
                        0,
                    )
                    remaining = max(remaining - derived_chunks, 0)
                    backlog = True
                    await asyncio.sleep(0)

            while stores and remaining > 0:
                pass_progress = False
                for store in stores:
                    if remaining <= 0:
                        break
                    batch_size = min(EMBEDDING_BATCH_SIZE, remaining)
                    stored = await index_embedding_batch(
                        store,
                        batch_size=batch_size,
                        load_model=True,
                    )
                    # A batch invocation can inspect/infer at most batch_size chunks.
                    remaining -= batch_size
                    pass_progress = pass_progress or stored > 0
                    backlog = backlog or stored >= batch_size
                    await asyncio.sleep(0)
                if not pass_progress:
                    break

            statuses = await asyncio.gather(
                *(asyncio.to_thread(store.chunk_status) for store in stores)
            )
            backlog = backlog or any(
                int(status.get("entries_missing_chunks", 0)) > 0
                or int(status.get("chunks_pending", 0)) > 0
                or int(status.get("chunks_stale", 0)) > 0
                or int(status.get("chunks_failed", 0)) > 0
                for status in statuses
            )

            if stores and not memory_delta_reported:
                current_rss = _rss_bytes()
                if memory_baseline is not None and current_rss is not None:
                    logger.info(
                        "Knowledge embedding runtime RSS delta after first index round: %.1f MiB",
                        (current_rss - memory_baseline) / (1024 * 1024),
                    )
                memory_delta_reported = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Indexing is best-effort: BM25 remains the complete fallback path.
            logger.warning(
                "Knowledge background indexing round failed (%s); BM25 remains available",
                type(exc).__name__,
            )
            backlog = False

        await _wait_for_wake(
            wake_event,
            BACKLOG_DELAY_SECONDS if backlog else IDLE_DELAY_SECONDS,
        )


def start_knowledge_indexer(knowledge_root: str | Path) -> bool:
    """Start the one process-local coordinator; repeated calls are harmless."""
    global _TASK, _WAKE_EVENT, _EVENT_LOOP

    loop = asyncio.get_running_loop()
    with _STATE_LOCK:
        if _TASK is not None and not _TASK.done():
            return False
        wake_event = asyncio.Event()
        _WAKE_EVENT = wake_event
        _EVENT_LOOP = loop
        _TASK = loop.create_task(
            _run_indexer(Path(knowledge_root), wake_event),
            name="knowledge-vector-indexer",
        )
    return True


def notify_knowledge_index_changed() -> None:
    """Wake the coordinator safely from async or synchronous mutation threads."""
    with _STATE_LOCK:
        loop = _EVENT_LOOP
        event = _WAKE_EVENT
    if loop is None or event is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        return


async def stop_knowledge_indexer() -> None:
    """Cancel the coordinator and release its process-local model runtime."""
    global _TASK, _WAKE_EVENT, _EVENT_LOOP

    with _STATE_LOCK:
        task = _TASK
        _TASK = None
        _WAKE_EVENT = None
        _EVENT_LOOP = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # This model instance belongs to main_server's knowledge runtime.  The
    # separately running memory-server process has its own lifecycle.
    if task is None:
        return
    try:
        from utils.local_embedding_runtime import release_local_embedding_service

        await release_local_embedding_service()
    except Exception as exc:
        logger.debug("Knowledge embedding runtime cleanup failed: %s", exc)
