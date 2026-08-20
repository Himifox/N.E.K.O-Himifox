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
# Calibrated with input contract v2 against the local 256d int8 model:
# Recall@3=80% and unrelated-query rejection=90% on the grounded release set.
SEMANTIC_THRESHOLD = 0.57
VECTOR_CANDIDATE_LIMIT = 12
QUERY_EMBEDDING_TIMEOUT_SECONDS = 1.0
SLOW_BATCH_SECONDS = 15.0
DEFAULT_EMBEDDING_MICROBATCH_SIZE = 4
MAX_EMBEDDING_MICROBATCH_SIZE = 8
MAX_VECTOR_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_VECTOR_SNAPSHOT_CHUNKS = 10_000

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class EmbeddingBatchResult:
    selected: int = 0
    stored: int = 0
    failed: int = 0
    stale_writebacks: int = 0
    elapsed_ms: int = 0
    state: str = "no_work"


@dataclass(frozen=True, slots=True)
class SemanticQueryEmbedding:
    """One request-scoped query vector reusable across public-knowledge scans."""

    vector: list[float] | None
    status: LocalEmbeddingStatus
    state: str


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

    async def ensure_loaded(
        self,
        service: LocalEmbeddingService,
        *,
        timeout: float,
    ) -> str:
        task = self._start(service.request_load, kind="load")
        if task is None:
            return "inference_busy"
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if not done:
            return "model_load_timeout"
        try:
            return "ready" if bool(task.result()) else "not_ready"
        except Exception:
            return "embedding_unavailable"

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
    entry_rowids: np.ndarray
    chunk_indices: np.ndarray
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
    revision = store.chunks_revision()
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

    revision, rows, truncated = store.load_ready_chunk_vectors(
        model_id=status.model_id,
        limit=MAX_VECTOR_SNAPSHOT_CHUNKS,
    )

    entry_rowids: list[int] = []
    chunk_indices: list[int] = []
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
        entry_rowids.append(int(row["entry_rowid"]))
        chunk_indices.append(int(row["chunk_index"]))
    matrix = (
        np.stack(vectors).astype(np.float32, copy=False)
        if vectors
        else np.empty((0, status.dimensions), dtype=np.float32)
    )
    snapshot = VectorIndexSnapshot(
        revision,
        status.model_id,
        matrix,
        np.asarray(entry_rowids, dtype=np.int64),
        np.asarray(chunk_indices, dtype=np.int32),
        identity,
    )
    if truncated or matrix.nbytes > MAX_VECTOR_SNAPSHOT_BYTES:
        raise MemoryError("knowledge vector snapshot exceeds the local budget")
    with _CACHE_LOCK:
        _CACHE[key] = snapshot
    return snapshot


def _score_snapshot(
    snapshot: VectorIndexSnapshot,
    query_vector: list[float],
    *,
    store: MoegirlKnowledgeStore,
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
    candidate_count = min(
        len(scores),
        max(int(limit) * 8, 64),
    )
    if candidate_count < len(scores):
        candidate_indices = np.argpartition(scores, -candidate_count)[-candidate_count:]
        candidate_indices = candidate_indices[np.argsort(-scores[candidate_indices])]
    else:
        candidate_indices = np.argsort(-scores)
    rowids = [int(snapshot.entry_rowids[int(index)]) for index in candidate_indices]
    entries = store.load_entries_by_rowids(rowids)
    disabled = load_disabled_entries(get_catalog_override_path(store.database_path))
    best: dict[tuple[str, str], MoegirlKnowledgeHit] = {}
    for index in candidate_indices:
        score = float(scores[index])
        if score < SEMANTIC_THRESHOLD:
            break
        entry = entries.get(int(snapshot.entry_rowids[int(index)]))
        if entry is None:
            continue
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
            best_chunk_index=int(snapshot.chunk_indices[int(index)]),
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
    prepared = await prepare_semantic_query(query, stores=(store,))
    return await semantic_search_prepared(
        store,
        prepared,
        limit=limit,
        allowed_source_tags=allowed_source_tags,
    )


async def prepare_semantic_query(
    query: str,
    *,
    stores: tuple[MoegirlKnowledgeStore, ...],
) -> SemanticQueryEmbedding:
    """Encode one query at most once for the requested public-knowledge scan."""
    empty_status = LocalEmbeddingStatus(state="not_ready")
    if not str(query or "").strip():
        return SemanticQueryEmbedding(None, empty_status, "empty_query")
    if knowledge_inference_state():
        return SemanticQueryEmbedding(None, empty_status, "inference_busy")
    try:
        status = get_local_embedding_status()
    except Exception:
        return SemanticQueryEmbedding(None, empty_status, "status_unavailable")
    if status.state == "disabled":
        return SemanticQueryEmbedding(None, status, "disabled")
    if status.state == "not_ready":
        statuses = await asyncio.gather(
            *(asyncio.to_thread(store.chunk_status) for store in stores)
        )
        if not any(int(value.get("chunks_ready", 0)) > 0 for value in statuses):
            return SemanticQueryEmbedding(None, status, "index_not_ready")
    service = get_local_embedding_service()
    if status.state == "not_ready":
        load_state = await _INFERENCE_COORDINATOR.ensure_loaded(
            service,
            timeout=QUERY_EMBEDDING_TIMEOUT_SECONDS,
        )
        if load_state != "ready":
            return SemanticQueryEmbedding(None, status, load_state)
        status = get_local_embedding_status()
    if not status.ready:
        return SemanticQueryEmbedding(None, status, status.state)
    vector, query_state = await _INFERENCE_COORDINATOR.run_query(
        service,
        knowledge_query_embedding_text(query),
        timeout=QUERY_EMBEDDING_TIMEOUT_SECONDS,
    )
    if query_state != "ready":
        return SemanticQueryEmbedding(None, status, query_state)
    if vector is None:
        return SemanticQueryEmbedding(None, status, "query_embedding_unavailable")
    return SemanticQueryEmbedding(vector, status, "ready")


async def semantic_search_prepared(
    store: MoegirlKnowledgeStore,
    prepared: SemanticQueryEmbedding,
    *,
    limit: int = VECTOR_CANDIDATE_LIMIT,
    allowed_source_tags: tuple[str, ...] | None = None,
) -> tuple[list[MoegirlKnowledgeHit], str]:
    """Scan the public-knowledge index with an encoded request-scoped query."""
    if prepared.state != "ready" or prepared.vector is None:
        return [], prepared.state
    try:
        snapshot = await asyncio.to_thread(_load_snapshot, store, prepared.status)
        hits = await asyncio.to_thread(
            _score_snapshot,
            snapshot,
            prepared.vector,
            store=store,
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
    work = store.chunk_status()
    if not any(
        int(work.get(key, 0)) > 0
        for key in (
            "entries_missing_chunks",
            "chunks_pending",
            "chunks_stale",
            "chunks_failed_retryable_now",
        )
    ):
        return EmbeddingBatchResult(state="no_work")
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
