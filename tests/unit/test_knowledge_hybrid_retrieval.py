from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

import knowledge.vector_index as vector_index
import knowledge.service as service_module
from knowledge.models import (
    KnowledgeEntry,
    KnowledgeHit,
)
from knowledge.store import KnowledgeStore
from knowledge.service import KnowledgeService, _rrf_knowledge_hits
from knowledge.packs import validate_pack
from knowledge.vector_index import (
    SemanticQueryEmbedding,
    VectorIndexSnapshot,
    _score_snapshot,
)
from utils.local_embedding_runtime import LocalEmbeddingStatus


def _entry(title: str, *, source: str = "source:test") -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": (f"{title} alias",), "recognition": ()},
        tags=(source,),
        summary=f"Summary for {title}",
        content=f"Content for {title}",
    )


def _hit(
    title: str,
    score: float,
    *,
    semantic: bool = False,
    source: str = "source:test",
    chunk_index: int | None = None,
) -> KnowledgeHit:
    return KnowledgeHit(
        entry=_entry(title, source=source),
        score=score,
        retrieval_modes=("semantic",) if semantic else (),
        semantic_score=score if semantic else None,
        best_chunk_index=chunk_index,
    )


def _set_ready_runtime(monkeypatch, service, *, dimensions: int = 2) -> None:
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(
            state="ready",
            model_id="fixture",
            dimensions=dimensions,
        ),
    )
    monkeypatch.setattr(vector_index, "get_local_embedding_service", lambda: service)


def _fresh_coordinator(monkeypatch):
    coordinator = vector_index._KnowledgeInferenceCoordinator()
    monkeypatch.setattr(vector_index, "_INFERENCE_COORDINATOR", coordinator)
    return coordinator


def test_rrf_keeps_lexical_only_order():
    lexical = [_hit("Exact", 10.0), _hit("Second", 5.0)]

    result = _rrf_knowledge_hits(lexical, [], limit=2)

    assert [hit.entry.title for hit in result] == ["Exact", "Second"]
    assert all(hit.retrieval_modes == ("lexical",) for hit in result)
    assert [hit.lexical_score for hit in result] == [10.0, 5.0]


def test_rrf_returns_semantic_only_candidates():
    result = _rrf_knowledge_hits(
        [],
        [_hit("Paraphrase", 0.91, semantic=True, chunk_index=2)],
        limit=3,
    )

    assert [hit.entry.title for hit in result] == ["Paraphrase"]
    assert result[0].retrieval_modes == ("semantic",)
    assert result[0].semantic_score == pytest.approx(0.91)
    assert result[0].best_chunk_index == 2


def test_rrf_promotes_candidates_present_in_both_rankings():
    lexical = [_hit("Lexical only", 10.0), _hit("Both", 9.0)]
    semantic = [
        _hit("Semantic only", 0.95, semantic=True),
        _hit("Both", 0.90, semantic=True, chunk_index=3),
    ]

    result = _rrf_knowledge_hits(lexical, semantic, limit=3)

    assert result[0].entry.title == "Both"
    assert result[0].retrieval_modes == ("lexical", "semantic")
    assert result[0].lexical_score == pytest.approx(9.0)
    assert result[0].semantic_score == pytest.approx(0.90)
    assert result[0].best_chunk_index == 3


def test_semantic_scan_collapses_chunks_and_applies_filters(tmp_path, monkeypatch):
    kept = _entry("Kept", source="source:allowed")
    disabled = _entry("Disabled", source="source:allowed")
    wrong_source = _entry("Wrong source", source="source:other")
    store = KnowledgeStore(tmp_path / "knowledge.db")
    for entry in (kept, disabled, wrong_source):
        store.upsert(entry)
    snapshot = VectorIndexSnapshot(
        revision=1,
        model_id="fixture",
        matrix=np.asarray(
            [
                [1.0, 0.0],
                [0.8, 0.6],
                [0.95, 0.3122499],
                [0.9, 0.4358899],
            ],
            dtype=np.float32,
        ),
        entry_rowids=np.asarray([1, 1, 2, 3], dtype=np.int64),
        chunk_indices=np.asarray([4, 1, 0, 0], dtype=np.int32),
    )
    monkeypatch.setattr(
        "knowledge.vector_index.load_disabled_entries",
        lambda _path: frozenset({("source:allowed", "Disabled")}),
    )

    result = _score_snapshot(
        snapshot,
        [1.0, 0.0],
        store=store,
        limit=12,
        allowed_source_tags=("source:allowed",),
    )

    assert [hit.entry.title for hit in result] == ["Kept"]
    assert result[0].best_chunk_index == 4
    assert result[0].semantic_score == pytest.approx(1.0)


def test_semantic_scan_filters_sources_before_top_k(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Crowding source", source="source:other"))
    store.upsert(_entry("Allowed result", source="source:allowed"))
    snapshot = VectorIndexSnapshot(
        revision=1,
        model_id="fixture",
        matrix=np.asarray([[1.0, 0.0]] * 65 + [[0.8, 0.6]], dtype=np.float32),
        entry_rowids=np.asarray([1] * 65 + [2], dtype=np.int64),
        chunk_indices=np.arange(66, dtype=np.int32),
    )
    monkeypatch.setattr(
        "knowledge.vector_index.load_disabled_entries",
        lambda _path: frozenset(),
    )

    result = _score_snapshot(
        snapshot,
        [1.0, 0.0],
        store=store,
        limit=1,
        allowed_source_tags=("source:allowed",),
    )

    assert [hit.entry.title for hit in result] == ["Allowed result"]
    assert result[0].semantic_score == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_semantic_search_uses_versioned_query_input(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    received: list[str] = []

    class _EmbeddingService:
        async def embed(self, text):
            received.append(text)
            return [1.0, 0.0]

    _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, _EmbeddingService())

    _hits, state = await vector_index.semantic_search(store, "  用户问题  ")

    assert state == "ready"
    assert received == ["Query: 用户问题"]


@pytest.mark.asyncio
async def test_semantic_search_does_not_overlap_background_inference(
    tmp_path,
    monkeypatch,
):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    started = asyncio.Event()
    release = asyncio.Event()

    class _EmbeddingService:
        query_calls = 0

        async def embed(self, _text):
            self.query_calls += 1
            return [1.0, 0.0]

        async def embed_batch(self, _texts):
            started.set()
            await release.wait()
            return [[1.0, 0.0]]

    service = _EmbeddingService()
    coordinator = _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, service)
    background = asyncio.create_task(
        coordinator.run_background(service, ["Document:\nContent: test"])
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    hits, state = await vector_index.semantic_search(store, "question")

    assert hits == []
    assert state == "inference_busy"
    assert service.query_calls == 0
    release.set()
    await background


@pytest.mark.asyncio
async def test_query_soft_timeout_tracks_native_work_and_prevents_stacking(
    tmp_path,
    monkeypatch,
):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    started = asyncio.Event()
    release = asyncio.Event()

    class _EmbeddingService:
        calls = 0
        cancelled = False

        async def embed(self, _text):
            self.calls += 1
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return [1.0, 0.0]

    service = _EmbeddingService()
    _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, service)
    monkeypatch.setattr(vector_index, "QUERY_EMBEDDING_TIMEOUT_SECONDS", 0.01)

    hits, state = await vector_index.semantic_search(store, "first")
    assert hits == []
    assert state == "query_timeout"
    await asyncio.wait_for(started.wait(), timeout=1.0)

    hits, state = await vector_index.semantic_search(store, "second")
    assert hits == []
    assert state == "inference_busy"
    assert service.calls == 1
    assert service.cancelled is False

    release.set()
    await vector_index.drain_knowledge_embedding_inference()
    await asyncio.sleep(0)
    _hits, state = await vector_index.semantic_search(store, "third")
    assert state == "ready"
    assert service.calls == 2


@pytest.mark.asyncio
async def test_embedding_batch_defaults_to_four_and_caps_at_eight(
    tmp_path, monkeypatch
):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    for index in range(12):
        store.upsert(_entry(f"Entry {index}"))

    class _EmbeddingService:
        batch_sizes: list[int] = []

        async def embed_batch(self, texts):
            self.batch_sizes.append(len(texts))
            return [[1.0, 0.0] for _text in texts]

    service = _EmbeddingService()
    _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, service)

    default_result = await vector_index.index_embedding_batch(store)
    capped_result = await vector_index.index_embedding_batch(store, batch_size=128)

    assert default_result.selected == 4
    assert default_result.stored == 4
    assert capped_result.selected == 8
    assert capped_result.stored == 8
    assert service.batch_sizes == [4, 8]


@pytest.mark.asyncio
async def test_embedding_batch_prewarms_query_runtime_without_pending_work(
    tmp_path,
    monkeypatch,
):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    class _PrewarmService:
        load_requests = 0

        def is_available(self):
            return False

        def is_disabled(self):
            return False

        async def request_load(self):
            self.load_requests += 1

    service = _PrewarmService()
    monkeypatch.setattr(vector_index, "get_local_embedding_service", lambda: service)

    result = await vector_index.index_embedding_batch(store, load_model=True)

    assert result.state == "no_work"
    assert service.load_requests == 1


@pytest.mark.asyncio
async def test_embedding_batch_reads_sqlite_off_the_event_loop(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    entered = threading.Event()
    release = threading.Event()
    original_status = store.chunk_status

    def slow_status():
        entered.set()
        release.wait(timeout=0.5)
        return original_status()

    monkeypatch.setattr(store, "chunk_status", slow_status)
    task = asyncio.create_task(vector_index.index_embedding_batch(store))

    assert await asyncio.to_thread(entered.wait, 1.0)
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    result = await task
    assert result.state == "no_work"


@pytest.mark.asyncio
async def test_model_reconciliation_releases_old_vectors_from_ready_budget(
    tmp_path,
    monkeypatch,
):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Old model vector"))
    chunk = store.pending_embedding_chunks(model_id="old-model", limit=1)[0]
    assert store.store_chunk_embedding(
        chunk_id=str(chunk["chunk_id"]),
        content_hash=str(chunk["content_hash"]),
        model_id="old-model",
        dimensions=2,
        embedding=np.asarray([1.0, 0.0], dtype="<f2").tobytes(),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(
            state="not_ready",
            model_id="new-model",
            dimensions=2,
        ),
    )

    stale = await vector_index.reconcile_embedding_models([store])
    status = store.chunk_status()

    assert stale == 1
    assert status["chunks_ready"] == 0
    assert status["chunks_stale"] == 1


@pytest.mark.asyncio
async def test_slow_embedding_batch_is_stored_without_failure(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Slow but valid"))

    class _EmbeddingService:
        async def embed_batch(self, texts):
            return [[1.0, 0.0] for _text in texts]

    _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, _EmbeddingService())
    monkeypatch.setattr(vector_index, "SLOW_BATCH_SECONDS", -1.0)

    result = await vector_index.index_embedding_batch(store)
    status = store.chunk_status()

    assert result.state == "slow_batch"
    assert result.selected == result.stored == 1
    assert result.failed == 0
    assert status["chunks_ready"] == 1
    assert status["chunks_failed"] == 0


@pytest.mark.asyncio
async def test_embedding_exception_marks_selected_chunks_failed(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Broken inference"))

    class _EmbeddingService:
        async def embed_batch(self, _texts):
            raise RuntimeError("native inference failed")

    _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, _EmbeddingService())

    result = await vector_index.index_embedding_batch(store)
    status = store.chunk_status()

    assert result.state == "failed"
    assert result.selected == result.failed == 1
    assert result.stored == 0
    assert status["chunks_failed"] == 1


@pytest.mark.asyncio
async def test_embedding_result_reports_stale_writeback(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Changing entry"))
    started = asyncio.Event()
    release = asyncio.Event()

    class _EmbeddingService:
        async def embed_batch(self, texts):
            started.set()
            await release.wait()
            return [[1.0, 0.0] for _text in texts]

    _fresh_coordinator(monkeypatch)
    _set_ready_runtime(monkeypatch, _EmbeddingService())
    task = asyncio.create_task(vector_index.index_embedding_batch(store))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    updated = _entry("Changing entry")
    updated = KnowledgeEntry(
        title=updated.title,
        terms=updated.terms,
        tags=updated.tags,
        summary=updated.summary,
        content="New content invalidates the in-flight chunk.",
    )
    store.upsert(updated)
    release.set()

    result = await task

    assert result.state == "ready"
    assert result.selected == 1
    assert result.stored == 0
    assert result.failed == 0
    assert result.stale_writebacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic_state", ["disabled"])
async def test_asearch_falls_back_to_bm25_for_embedding_failures(
    tmp_path,
    monkeypatch,
    semantic_state,
):
    database_path = tmp_path / "knowledge.db"
    store = KnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    service = KnowledgeService.for_database(database_path)

    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(state=semantic_state),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: pytest.fail("an unavailable model must not be queried"),
    )

    result = await service.asearch("Fallback target", limit=3)

    assert [item.hit.entry.title for item in result] == ["Fallback target"]


@pytest.mark.asyncio
async def test_asearch_soft_loads_not_ready_model_then_falls_back(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "knowledge.db"
    store = KnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    chunk = store.pending_embedding_chunks(model_id="fixture", limit=1)[0]
    store.store_chunk_embedding(
        chunk_id=str(chunk["chunk_id"]),
        content_hash=str(chunk["content_hash"]),
        model_id="fixture",
        dimensions=2,
        embedding=np.asarray([1.0, 0.0], dtype="<f2").tobytes(),
    )
    service = KnowledgeService.for_database(database_path)

    class _NotReadyService:
        async def request_load(self):
            return False

    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(state="not_ready"),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: _NotReadyService(),
    )

    result = await service.asearch("Fallback target", limit=3)

    assert [item.hit.entry.title for item in result] == ["Fallback target"]


@pytest.mark.asyncio
async def test_asearch_falls_back_to_bm25_for_corrupt_embedding(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    store = KnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    service = KnowledgeService.for_database(database_path)

    class _CorruptEmbeddingService:
        async def embed(self, _query):
            return [float("nan"), 0.0]

    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(
            state="ready",
            model_id="fixture",
            dimensions=2,
        ),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: _CorruptEmbeddingService(),
    )

    result = await service.asearch("Fallback target", limit=3)

    assert [item.hit.entry.title for item in result] == ["Fallback target"]


@pytest.mark.asyncio
async def test_asearch_falls_back_to_bm25_when_embedding_raises(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    store = KnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    service = KnowledgeService.for_database(database_path)

    class _FailingEmbeddingService:
        async def embed(self, _query):
            raise TimeoutError

    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(
            state="ready",
            model_id="fixture",
            dimensions=2,
        ),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: _FailingEmbeddingService(),
    )

    result = await service.asearch("Fallback target", limit=3)

    assert [item.hit.entry.title for item in result] == ["Fallback target"]


def test_invalid_query_vector_is_safely_ignored(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Target"))
    snapshot = VectorIndexSnapshot(
        revision=1,
        model_id="fixture",
        matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
        entry_rowids=np.asarray([1], dtype=np.int64),
        chunk_indices=np.asarray([0], dtype=np.int32),
    )

    assert (
        _score_snapshot(
            snapshot,
            [float("nan"), 0.0],
            store=store,
            limit=3,
            allowed_source_tags=None,
        )
        == []
    )
    assert (
        _score_snapshot(
            snapshot,
            [1.0],
            store=store,
            limit=3,
            allowed_source_tags=None,
        )
        == []
    )


@pytest.mark.asyncio
async def test_non_numeric_embedding_response_falls_back_to_bm25(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    KnowledgeStore(database_path).upsert(_entry("Fallback target"))
    service = KnowledgeService.for_database(database_path)

    class _MalformedEmbeddingService:
        async def embed(self, _query):
            return {"not": "a vector"}

    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(
            state="ready",
            model_id="fixture",
            dimensions=2,
        ),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: _MalformedEmbeddingService(),
    )

    result = await service.asearch("Fallback target", limit=3)

    assert [item.hit.entry.title for item in result] == ["Fallback target"]


def test_semantic_threshold_rejects_weak_candidates(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Weak"))
    snapshot = VectorIndexSnapshot(
        revision=1,
        model_id="fixture",
        matrix=np.asarray([[0.56, np.sqrt(1.0 - 0.56**2)]], dtype=np.float32),
        entry_rowids=np.asarray([1], dtype=np.int64),
        chunk_indices=np.asarray([0], dtype=np.int32),
    )

    assert (
        _score_snapshot(
            snapshot,
            [1.0, 0.0],
            store=store,
            limit=3,
            allowed_source_tags=None,
        )
        == []
    )


def test_vector_snapshot_is_compact_and_reused(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Compact"))
    chunk = store.pending_embedding_chunks(model_id="fixture", limit=1)[0]
    store.store_chunk_embedding(
        chunk_id=str(chunk["chunk_id"]),
        content_hash=str(chunk["content_hash"]),
        model_id="fixture",
        dimensions=2,
        embedding=np.asarray([1.0, 0.0], dtype="<f2").tobytes(),
    )
    status = LocalEmbeddingStatus(state="ready", model_id="fixture", dimensions=2)
    calls = 0
    original = store.load_ready_chunk_vectors

    def _load(*, model_id, limit):
        nonlocal calls
        calls += 1
        return original(model_id=model_id, limit=limit)

    monkeypatch.setattr(store, "load_ready_chunk_vectors", _load)
    vector_index._CACHE.clear()

    first = vector_index._load_snapshot(store, status)
    second = vector_index._load_snapshot(store, status)

    assert first is second
    assert calls == 1
    assert first.matrix.shape == (1, 2)
    assert first.entry_rowids.tolist() == [1]


def test_vector_snapshot_fails_closed_above_memory_budget(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("Oversized"))
    chunk = store.pending_embedding_chunks(model_id="fixture", limit=1)[0]
    store.store_chunk_embedding(
        chunk_id=str(chunk["chunk_id"]),
        content_hash=str(chunk["content_hash"]),
        model_id="fixture",
        dimensions=2,
        embedding=np.asarray([1.0, 0.0], dtype="<f2").tobytes(),
    )
    vector_index._CACHE.clear()
    monkeypatch.setattr(vector_index, "MAX_VECTOR_SNAPSHOT_BYTES", 1)

    calls = 0
    original = store.load_ready_chunk_vectors

    def counted_load(*, model_id, limit):
        nonlocal calls
        calls += 1
        return original(model_id=model_id, limit=limit)

    monkeypatch.setattr(store, "load_ready_chunk_vectors", counted_load)
    with pytest.raises(MemoryError):
        vector_index._load_snapshot(
            store,
            LocalEmbeddingStatus(
                state="ready",
                model_id="fixture",
                dimensions=2,
            ),
        )
    with pytest.raises(MemoryError):
        vector_index._load_snapshot(
            store,
            LocalEmbeddingStatus(state="ready", model_id="fixture", dimensions=2),
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_single_store_search_reuses_one_query_embedding_across_material_types(
    tmp_path,
    monkeypatch,
):
    service = KnowledgeService(tmp_path)
    KnowledgeStore(service.database_path()).upsert(
        _entry("shared phrase knowledge", source="source:chime")
    )
    service.install_pack(
        validate_pack(
            {
                "schema_version": 1,
                "pack_id": "reply-samples",
                "material_type": "corpus",
                "source": {
                    "name": "Reply Samples",
                    "homepage": "",
                    "license": "CC0-1.0",
                },
                "entries": [
                    {
                        "title": "Corpus answer",
                        "terms": {"alias": [], "recognition": []},
                        "tags": [],
                        "summary": "shared phrase response sample",
                        "content": "shared phrase response sample",
                    }
                ],
            }
        )
    )
    preparation_calls = 0
    scan_calls: list[SemanticQueryEmbedding] = []
    prepared = SemanticQueryEmbedding(
        vector=None,
        status=None,
        state="not_ready",
    )

    async def _prepare(
        _query,
        *,
        stores,
        load_model=True,
        deadline_monotonic=None,
    ):
        del load_model, deadline_monotonic
        nonlocal preparation_calls
        preparation_calls += 1
        assert len(stores) == 1
        return prepared

    async def _scan(_store, query_embedding, **_kwargs):
        scan_calls.append(query_embedding)
        return [], query_embedding.state

    monkeypatch.setattr(service_module, "prepare_semantic_query", _prepare)
    monkeypatch.setattr(service_module, "semantic_search_prepared", _scan)

    result = await service.asearch(
        "shared phrase",
        allowed_material_types=("corpus", "knowledge"),
        target_material_type="corpus",
        limit=2,
    )

    assert preparation_calls == 1
    assert scan_calls == [prepared]
    assert [item.material_type for item in result] == ["corpus", "knowledge"]
    assert [item.hit.entry.title for item in result] == [
        "Corpus answer",
        "shared phrase knowledge",
    ]


@pytest.mark.asyncio
async def test_automatic_search_does_not_cold_load_embedding_model(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "knowledge.db"
    store = KnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    chunk = store.pending_embedding_chunks(model_id="fixture", limit=1)[0]
    store.store_chunk_embedding(
        chunk_id=str(chunk["chunk_id"]),
        content_hash=str(chunk["content_hash"]),
        model_id="fixture",
        dimensions=2,
        embedding=np.asarray([1.0, 0.0], dtype="<f2").tobytes(),
    )
    service = KnowledgeService.for_database(database_path)

    class _NotReadyService:
        load_calls = 0

        async def request_load(self):
            self.load_calls += 1
            return True

    runtime = _NotReadyService()
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_status",
        lambda: LocalEmbeddingStatus(state="not_ready"),
    )
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: runtime,
    )

    result = await service.asearch(
        "Fallback target",
        limit=3,
        load_model=False,
    )

    assert [item.hit.entry.title for item in result] == ["Fallback target"]
    assert runtime.load_calls == 0


@pytest.mark.asyncio
async def test_asearch_returns_completed_bm25_when_semantic_budget_expires(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "knowledge.db"
    KnowledgeStore(database_path).upsert(_entry("Fallback target"))
    service = KnowledgeService.for_database(database_path)
    release = asyncio.Event()

    async def _prepare(_query, **_kwargs):
        await release.wait()
        return SemanticQueryEmbedding(
            vector=None,
            status=LocalEmbeddingStatus(state="not_ready"),
            state="not_ready",
        )

    monkeypatch.setattr(service_module, "prepare_semantic_query", _prepare)
    started_at = asyncio.get_running_loop().time()
    try:
        result = await service.asearch(
            "Fallback target",
            limit=3,
            deadline_monotonic=started_at + 0.2,
        )
    finally:
        release.set()
        await asyncio.sleep(0)

    assert [item.hit.entry.title for item in result] == ["Fallback target"]
    assert asyncio.get_running_loop().time() - started_at < 0.5
