from __future__ import annotations

import numpy as np
import pytest

import knowledge.vector_index as vector_index
from knowledge.moegirl_knowledge.models import MoegirlKnowledgeEntry, MoegirlKnowledgeHit
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

    assert _score_snapshot(
        snapshot,
        [float("nan"), 0.0],
        database_path=tmp_path / "knowledge.db",
        limit=3,
        allowed_source_tags=None,
    ) == []


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
        matrix=np.asarray([[0.29, np.sqrt(1.0 - 0.29**2)]], dtype=np.float32),
        rows=({"entry": _entry("Weak"), "chunk_index": 0},),
    )

    assert _score_snapshot(
        snapshot,
        [1.0, 0.0],
        database_path=tmp_path / "knowledge.db",
        limit=3,
        allowed_source_tags=None,
    ) == []
    assert _score_snapshot(
        snapshot,
        [1.0],
        database_path=tmp_path / "knowledge.db",
        limit=3,
        allowed_source_tags=None,
    ) == []
