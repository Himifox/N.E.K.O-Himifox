from __future__ import annotations

import asyncio

import numpy as np
import pytest

import knowledge.vector_index as vector_index
from knowledge.moegirl_knowledge.models import (
    MoegirlKnowledgeEntry,
    MoegirlKnowledgeHit,
)
from knowledge.moegirl_knowledge.store import MoegirlKnowledgeStore
from knowledge.service import KnowledgeService, _rrf_knowledge_hits
from knowledge.vector_index import VectorIndexSnapshot, _score_snapshot
from utils.local_embedding_runtime import LocalEmbeddingStatus


def _entry(title: str, *, source: str = "source:test") -> MoegirlKnowledgeEntry:
    return MoegirlKnowledgeEntry(
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
) -> MoegirlKnowledgeHit:
    return MoegirlKnowledgeHit(
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
        rows=(
            {"entry": kept, "chunk_index": 4},
            {"entry": kept, "chunk_index": 1},
            {"entry": disabled, "chunk_index": 0},
            {"entry": wrong_source, "chunk_index": 0},
        ),
    )
    monkeypatch.setattr(
        "knowledge.vector_index.load_disabled_entries",
        lambda _path: frozenset({("source:allowed", "Disabled")}),
    )

    result = _score_snapshot(
        snapshot,
        [1.0, 0.0],
        database_path=tmp_path / "knowledge.db",
        limit=12,
        allowed_source_tags=("source:allowed",),
    )

    assert [hit.entry.title for hit in result] == ["Kept"]
    assert result[0].best_chunk_index == 4
    assert result[0].semantic_score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_semantic_search_uses_versioned_query_input(tmp_path, monkeypatch):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
async def test_slow_embedding_batch_is_stored_without_failure(tmp_path, monkeypatch):
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")
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
    updated = MoegirlKnowledgeEntry(
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
@pytest.mark.parametrize("semantic_state", ["disabled", "not_ready"])
async def test_asearch_falls_back_to_bm25_for_embedding_failures(
    tmp_path,
    monkeypatch,
    semantic_state,
):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    service = KnowledgeService.for_collection("meme", database_path)

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

    result = await service.asearch("meme", "Fallback target", limit=3)

    assert [hit.entry.title for hit in result] == ["Fallback target"]


@pytest.mark.asyncio
async def test_asearch_falls_back_to_bm25_for_corrupt_embedding(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    service = KnowledgeService.for_collection("meme", database_path)

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

    result = await service.asearch("meme", "Fallback target", limit=3)

    assert [hit.entry.title for hit in result] == ["Fallback target"]


@pytest.mark.asyncio
async def test_asearch_falls_back_to_bm25_when_embedding_raises(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    store.upsert(_entry("Fallback target"))
    service = KnowledgeService.for_collection("meme", database_path)

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

    result = await service.asearch("meme", "Fallback target", limit=3)

    assert [hit.entry.title for hit in result] == ["Fallback target"]


def test_invalid_query_vector_is_safely_ignored(tmp_path):
    snapshot = VectorIndexSnapshot(
        revision=1,
        model_id="fixture",
        matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
        rows=({"entry": _entry("Target"), "chunk_index": 0},),
    )

    assert (
        _score_snapshot(
            snapshot,
            [float("nan"), 0.0],
            database_path=tmp_path / "knowledge.db",
            limit=3,
            allowed_source_tags=None,
        )
        == []
    )


@pytest.mark.asyncio
async def test_non_numeric_embedding_response_falls_back_to_bm25(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    MoegirlKnowledgeStore(database_path).upsert(_entry("Fallback target"))
    service = KnowledgeService.for_collection("meme", database_path)

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

    result = await service.asearch("meme", "Fallback target", limit=3)

    assert [hit.entry.title for hit in result] == ["Fallback target"]


def test_semantic_threshold_rejects_weak_candidates(tmp_path):
    snapshot = VectorIndexSnapshot(
        revision=1,
        model_id="fixture",
        matrix=np.asarray([[0.56, np.sqrt(1.0 - 0.56**2)]], dtype=np.float32),
        rows=({"entry": _entry("Weak"), "chunk_index": 0},),
    )

    assert (
        _score_snapshot(
            snapshot,
            [1.0, 0.0],
            database_path=tmp_path / "knowledge.db",
            limit=3,
            allowed_source_tags=None,
        )
        == []
    )
    assert (
        _score_snapshot(
            snapshot,
            [1.0],
            database_path=tmp_path / "knowledge.db",
            limit=3,
            allowed_source_tags=None,
        )
        == []
    )
