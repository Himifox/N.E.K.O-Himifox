"""Knowledge-owned progressive vector index and exact cosine search."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import numpy as np

from utils.local_embedding_runtime import (
    LocalEmbeddingService,
    LocalEmbeddingStatus,
    get_local_embedding_service,
    get_local_embedding_status,
)

from ._mutation_lock import mutation_lock
from .chunking import knowledge_query_embedding_text
from .moegirl_knowledge.catalog_overrides import (
    entry_key,
    get_catalog_override_path,
    load_disabled_entries,
)
from .moegirl_knowledge.models import MoegirlKnowledgeHit
from .moegirl_knowledge.store import MoegirlKnowledgeStore


logger = logging.getLogger("N.E.K.O.Knowledge.VectorIndex")
SEMANTIC_THRESHOLD = 0.30
VECTOR_CANDIDATE_LIMIT = 12
QUERY_EMBEDDING_TIMEOUT_SECONDS = 1.0
SLOW_BATCH_SECONDS = 15.0
DEFAULT_EMBEDDING_MICROBATCH_SIZE = 4
MAX_EMBEDDING_MICROBATCH_SIZE = 8

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class EmbeddingBatchResult:
    selected: int = 0
    stored: int = 0
    failed: int = 0
    stale_writebacks: int = 0
    elapsed_ms: int = 0
    state: str = "no_work"


class _KnowledgeInferenceCoordinator:
    """Serialize knowledge-owned native inference without cancelling timed-out work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: asyncio.Task[object] | None = None
        self._kind = ""

    def active_kind(self) -> str:
        with self._lock:
            task = self._task
            return self._kind if task is not None and not task.done() else ""

    def _start(
        self,
        factory: Callable[[], Awaitable[_T]],
        *,
        kind: str,
    ) -> asyncio.Task[_T] | None:
        loop = asyncio.get_running_loop()
        with self._lock:
            active = self._task
            if active is not None and not active.done():
                return None
            task = loop.create_task(factory(), name=f"knowledge-embedding-{kind}")
            self._task = task
            self._kind = kind
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[object]) -> None:
        # Retrieve exceptions even when a query has already returned after its
        # soft timeout, then make the coordinator available for later work.
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        with self._lock:
            if self._task is task:
                self._task = None
                self._kind = ""

    async def run_query(
        self,
        service: LocalEmbeddingService,
        text: str,
        *,
        timeout: float,
    ) -> tuple[list[float] | None, str]:
        task = self._start(lambda: service.embed(text), kind="query")
        if task is None:
            return None, "inference_busy"
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if not done:
            return None, "query_timeout"
        try:
            return task.result(), "ready"
        except Exception:
            return None, "query_embedding_failed"

    async def run_background(
        self,
        service: LocalEmbeddingService,
        texts: list[str],
    ) -> tuple[list[list[float] | None] | None, str, Exception | None]:
        task = self._start(lambda: service.embed_batch(texts), kind="background")
        if task is None:
            return None, "inference_busy", None
        try:
            vectors = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Native inference cannot be cancelled safely. Keep the task tracked;
            # shutdown drains it before releasing the model runtime.
            raise
        except Exception as exc:
            return None, "inference_failed", exc
        return vectors, "ready", None

    async def drain(self) -> None:
        with self._lock:
            task = self._task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            pass


_INFERENCE_COORDINATOR = _KnowledgeInferenceCoordinator()


def knowledge_inference_state() -> str:
    return _INFERENCE_COORDINATOR.active_kind()


async def drain_knowledge_embedding_inference() -> None:
    await _INFERENCE_COORDINATOR.drain()


@dataclass(frozen=True, slots=True)
class VectorIndexSnapshot:
    revision: int
    model_id: str
    matrix: np.ndarray
    rows: tuple[dict[str, object], ...]
    database_identity: tuple[int, int, int, int] | tuple[()] = ()


_CACHE: dict[str, VectorIndexSnapshot] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(path: Path) -> str:
    return str(path.resolve()).casefold()


def _database_identity(path: Path) -> tuple[int, int, int, int] | tuple[()]:
    try:
        stat = path.stat()
    except OSError:
        return ()
    return int(stat.st_dev), int(stat.st_ino), int(stat.st_mtime_ns), int(stat.st_size)


def _load_snapshot(
    store: MoegirlKnowledgeStore, status: LocalEmbeddingStatus
) -> VectorIndexSnapshot:
    revision, rows = store.load_ready_chunks(model_id=status.model_id)
    key = _cache_key(store.database_path)
    identity = _database_identity(store.database_path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if (
            cached is not None
            and cached.revision == revision
            and cached.model_id == status.model_id
            and cached.database_identity == identity
        ):
            return cached

    valid_rows: list[dict[str, object]] = []
    vectors: list[np.ndarray] = []
    for row in rows:
        dimensions = int(row.get("dimensions") or 0)
        raw = row.get("embedding")
        if (
            dimensions != status.dimensions
            or not isinstance(raw, bytes)
            or len(raw) != dimensions * 2
        ):
            continue
        vector = np.frombuffer(raw, dtype="<f2").astype(np.float32)
        norm = float(np.linalg.norm(vector))
        if norm <= 0 or not np.isfinite(vector).all():
            continue
        vectors.append(vector / norm)
        valid_rows.append(row)
    matrix = (
        np.stack(vectors).astype(np.float32, copy=False)
        if vectors
        else np.empty((0, status.dimensions), dtype=np.float32)
    )
    snapshot = VectorIndexSnapshot(
        revision,
        status.model_id,
        matrix,
        tuple(valid_rows),
        identity,
    )
    with _CACHE_LOCK:
        _CACHE[key] = snapshot
    return snapshot


def _score_snapshot(
    snapshot: VectorIndexSnapshot,
    query_vector: list[float],
    *,
    database_path: Path,
    limit: int,
    allowed_source_tags: tuple[str, ...] | None,
) -> list[MoegirlKnowledgeHit]:
    if snapshot.matrix.size == 0:
        return []
    query = np.asarray(query_vector, dtype=np.float32).ravel()
    if query.size != snapshot.matrix.shape[1] or not np.isfinite(query).all():
        return []
    norm = float(np.linalg.norm(query))
    if norm <= 0:
        return []
    scores = snapshot.matrix @ (query / norm)
    disabled = load_disabled_entries(get_catalog_override_path(database_path))
    best: dict[tuple[str, str], MoegirlKnowledgeHit] = {}
    for index in np.argsort(-scores):
        score = float(scores[index])
        if score < SEMANTIC_THRESHOLD:
            break
        row = snapshot.rows[int(index)]
        entry = row["entry"]
        if entry_key(entry) in disabled:
            continue
        if (
            allowed_source_tags is not None
            and entry.source_tag not in allowed_source_tags
        ):
            continue
        key = entry_key(entry)
        candidate = MoegirlKnowledgeHit(
            entry=entry,
            score=score,
            retrieval_modes=("semantic",),
            semantic_score=score,
            best_chunk_index=int(row["chunk_index"]),
        )
        previous = best.get(key)
        if previous is None or score > float(previous.semantic_score or 0.0):
            best[key] = candidate
        if len(best) >= limit:
            break
    return sorted(
        best.values(),
        key=lambda hit: (-hit.score, hit.entry.title, hit.entry.source_tag),
    )[:limit]


async def semantic_search(
    store: MoegirlKnowledgeStore,
    query: str,
    *,
    limit: int = VECTOR_CANDIDATE_LIMIT,
    allowed_source_tags: tuple[str, ...] | None = None,
) -> tuple[list[MoegirlKnowledgeHit], str]:
    if not str(query or "").strip():
        return [], "empty_query"
    if knowledge_inference_state():
        return [], "inference_busy"
    try:
        status = get_local_embedding_status()
    except Exception:
        return [], "status_unavailable"
    if not status.ready:
        return [], status.state
    service = get_local_embedding_service()
    vector, query_state = await _INFERENCE_COORDINATOR.run_query(
        service,
        knowledge_query_embedding_text(query),
        timeout=QUERY_EMBEDDING_TIMEOUT_SECONDS,
    )
    if query_state != "ready":
        return [], query_state
    if vector is None:
        return [], "query_embedding_unavailable"
    try:
        snapshot = await asyncio.to_thread(_load_snapshot, store, status)
        hits = await asyncio.to_thread(
            _score_snapshot,
            snapshot,
            vector,
            database_path=store.database_path,
            limit=limit,
            allowed_source_tags=allowed_source_tags,
        )
    except Exception:
        return [], "invalid_response"
    return hits, "ready"


async def index_embedding_batch(
    store: MoegirlKnowledgeStore,
    *,
    batch_size: int = DEFAULT_EMBEDDING_MICROBATCH_SIZE,
    load_model: bool = False,
) -> EmbeddingBatchResult:
    safe_batch_size = max(1, min(int(batch_size), MAX_EMBEDDING_MICROBATCH_SIZE))
    try:
        service = get_local_embedding_service()
    except Exception:
        return EmbeddingBatchResult(state="embedding_unavailable")
    if load_model and not service.is_available() and not service.is_disabled():
        try:
            await service.request_load()
        except Exception:
            return EmbeddingBatchResult(state="embedding_unavailable")
    try:
        status = get_local_embedding_status()
    except Exception:
        return EmbeddingBatchResult(state="embedding_unavailable")
    if not status.ready:
        return EmbeddingBatchResult(state=status.state)
    with mutation_lock(store.database_path):
        store.mark_other_models_stale(status.model_id)
    chunks = store.pending_embedding_chunks(
        model_id=status.model_id, limit=safe_batch_size
    )
    if not chunks:
        return EmbeddingBatchResult(state="no_work")
    texts = [str(chunk["text"]) for chunk in chunks]
    started = time.perf_counter()
    (
        vectors,
        inference_state,
        inference_error,
    ) = await _INFERENCE_COORDINATOR.run_background(
        service,
        texts,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if inference_state == "inference_busy":
        return EmbeddingBatchResult(elapsed_ms=elapsed_ms, state=inference_state)
    if inference_error is not None:
        with mutation_lock(store.database_path):
            for chunk in chunks:
                store.mark_chunk_embedding_failed(
                    chunk_id=str(chunk["chunk_id"]),
                    content_hash=str(chunk["content_hash"]),
                    error_code=type(inference_error).__name__,
                )
        return EmbeddingBatchResult(
            selected=len(chunks),
            failed=len(chunks),
            elapsed_ms=elapsed_ms,
            state="failed",
        )

    if not isinstance(vectors, (list, tuple)):
        with mutation_lock(store.database_path):
            for chunk in chunks:
                store.mark_chunk_embedding_failed(
                    chunk_id=str(chunk["chunk_id"]),
                    content_hash=str(chunk["content_hash"]),
                    error_code="invalid_response",
                )
        return EmbeddingBatchResult(
            selected=len(chunks),
            failed=len(chunks),
            elapsed_ms=elapsed_ms,
            state="failed",
        )

    stored = 0
    failed = 0
    stale_writebacks = 0
    with mutation_lock(store.database_path):
        for index, chunk in enumerate(chunks):
            vector = vectors[index] if index < len(vectors) else None
            if vector is None:
                store.mark_chunk_embedding_failed(
                    chunk_id=str(chunk["chunk_id"]),
                    content_hash=str(chunk["content_hash"]),
                    error_code="empty_embedding",
                )
                failed += 1
                continue
            try:
                array = np.asarray(vector, dtype=np.float32).ravel()
            except (TypeError, ValueError):
                array = np.empty(0, dtype=np.float32)
            if array.size != status.dimensions or not np.isfinite(array).all():
                store.mark_chunk_embedding_failed(
                    chunk_id=str(chunk["chunk_id"]),
                    content_hash=str(chunk["content_hash"]),
                    error_code="invalid_embedding",
                )
                failed += 1
                continue
            payload = array.astype("<f2").tobytes()
            did_store = store.store_chunk_embedding(
                chunk_id=str(chunk["chunk_id"]),
                content_hash=str(chunk["content_hash"]),
                model_id=status.model_id,
                dimensions=status.dimensions,
                embedding=payload,
            )
            stored += int(did_store)
            stale_writebacks += int(not did_store)
    state = "failed" if failed else "ready"
    if not failed and elapsed_ms > round(SLOW_BATCH_SECONDS * 1000):
        state = "slow_batch"
        logger.warning(
            "Knowledge embedding microbatch was slow: selected=%d elapsed_ms=%d",
            len(chunks),
            elapsed_ms,
        )
    return EmbeddingBatchResult(
        selected=len(chunks),
        stored=stored,
        failed=failed,
        stale_writebacks=stale_writebacks,
        elapsed_ms=elapsed_ms,
        state=state,
    )
