from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from knowledge.moegirl_knowledge.store import MoegirlKnowledgeStore
from knowledge.pack_jobs import cancel_pack_job, process_pack_jobs
from knowledge.packs import validate_pack
from knowledge.service import KnowledgeService
from utils.local_embedding_runtime import LocalEmbeddingStatus


def _pack(*, title: str = "Staged phrase", pack_id: str = "staged-fixture"):
    return validate_pack({
        "schema_version": 1,
        "pack_id": pack_id,
        "collection_id": "meme",
        "source": {"name": "Fixture", "homepage": "", "license": "CC0"},
        "entries": [{
            "title": title,
            "terms": {"alias": [], "recognition": []},
            "tags": [],
            "summary": "A staged entry",
            "content": "A staged entry body.",
        }],
    })


@pytest.mark.asyncio
async def test_staged_pack_is_hidden_until_bm25_activation(tmp_path, monkeypatch):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())

    assert job["state"] == "queued"
    assert service.search("meme", "Staged phrase", limit=1) == []
    assert service.list_packs("meme") == ()

    monkeypatch.setattr(
        "utils.local_embedding_runtime.get_local_embedding_status",
        lambda: LocalEmbeddingStatus(state="disabled"),
    )
    result = await process_pack_jobs(
        service,
        batch_size=4,
        ready_vector_chunks=0,
    )

    assert result["state"] == "ready_bm25"
    assert service.search("meme", "Staged phrase", limit=1)
    assert service.list_packs("meme")[0]["retrieval_mode"] == "bm25"
    assert service.list_pack_jobs("meme")[0]["state"] == "active"


@pytest.mark.asyncio
async def test_pack_update_keeps_old_source_until_new_job_activates(tmp_path, monkeypatch):
    service = KnowledgeService.from_root(tmp_path)
    service.install_pack(_pack(title="Old phrase"))
    service.stage_pack(_pack(title="New phrase"))

    assert service.search("meme", "Old phrase", limit=1)
    assert service.search("meme", "New phrase", limit=1) == []

    monkeypatch.setattr(
        "utils.local_embedding_runtime.get_local_embedding_status",
        lambda: LocalEmbeddingStatus(state="disabled"),
    )
    await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert service.search("meme", "Old phrase", limit=1) == []
    assert service.search("meme", "New phrase", limit=1)


@pytest.mark.asyncio
async def test_ready_vectors_are_transferred_during_hybrid_activation(
    tmp_path,
    monkeypatch,
):
    import knowledge.vector_index as vector_index

    service = KnowledgeService.from_root(tmp_path)
    service.stage_pack(_pack())

    class _EmbeddingService:
        def is_available(self):
            return True

        def is_disabled(self):
            return False

        async def embed_batch(self, texts):
            return [[1.0, 0.0] for _text in texts]

    ready = LocalEmbeddingStatus(
        state="ready",
        model_id="fixture",
        dimensions=2,
    )
    monkeypatch.setattr(
        "utils.local_embedding_runtime.get_local_embedding_status",
        lambda: ready,
    )
    monkeypatch.setattr(vector_index, "get_local_embedding_status", lambda: ready)
    monkeypatch.setattr(
        vector_index,
        "get_local_embedding_service",
        lambda: _EmbeddingService(),
    )
    monkeypatch.setattr(
        vector_index,
        "_INFERENCE_COORDINATOR",
        vector_index._KnowledgeInferenceCoordinator(),
    )

    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)
    status = MoegirlKnowledgeStore(service.database_path("meme")).chunk_status()

    assert result["state"] == "ready_hybrid"
    assert status["chunks_ready"] == status["chunks_total"] == 1
    assert service.list_packs("meme")[0]["retrieval_mode"] == "hybrid"


def test_cancelled_job_never_becomes_visible(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())

    assert cancel_pack_job(tmp_path, str(job["job_id"])) is True
    assert cancel_pack_job(tmp_path, str(job["job_id"])) is False
    assert service.list_pack_jobs("meme")[0]["state"] == "cancelled"
    assert not (tmp_path / ".staging" / str(job["job_id"]) / "pack.json").exists()
    assert service.search("meme", "Staged phrase", limit=1) == []


def test_pack_chunk_budget_is_enforced(monkeypatch):
    import knowledge.packs as packs

    monkeypatch.setattr(packs, "MAX_PACK_PROJECTED_CHUNKS", 0)
    with pytest.raises(ValueError, match="too many chunks"):
        _pack()


def test_community_entry_budget_counts_pending_packs(tmp_path, monkeypatch):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    monkeypatch.setattr(pack_jobs, "MAX_COMMUNITY_ENTRIES", 1)
    service.stage_pack(_pack(pack_id="first-pack"))

    with pytest.raises(ValueError, match="too many entries"):
        service.stage_pack(_pack(pack_id="second-pack"))


def test_community_budget_allows_replacing_the_active_pack(tmp_path, monkeypatch):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    pack = _pack()
    service.install_pack(pack)
    monkeypatch.setattr(pack_jobs, "MAX_COMMUNITY_ENTRIES", 1)

    job = service.stage_pack(pack)

    assert job["state"] == "queued"


def test_pending_replacement_does_not_double_count_active_pack(tmp_path, monkeypatch):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    service.install_pack(_pack(pack_id="first-pack"))
    service.stage_pack(_pack(title="Updated phrase", pack_id="first-pack"))
    monkeypatch.setattr(pack_jobs, "MAX_COMMUNITY_ENTRIES", 2)

    job = service.stage_pack(_pack(pack_id="second-pack"))

    assert job["state"] == "queued"


@pytest.mark.asyncio
async def test_vector_budget_activates_pack_as_bm25_without_loading_model(
    tmp_path,
    monkeypatch,
):
    service = KnowledgeService.from_root(tmp_path)
    service.stage_pack(_pack())
    monkeypatch.setattr(
        "utils.local_embedding_runtime.get_local_embedding_status",
        lambda: pytest.fail("vector budget fallback must not load the model"),
    )

    result = await process_pack_jobs(
        service,
        batch_size=4,
        ready_vector_chunks=10_000,
    )
    job = service.list_pack_jobs("meme")[0]

    assert result["state"] == "ready_bm25"
    assert job["state"] == "active"
    assert job["retrieval_mode"] == "bm25"
    assert job["reason"] == "vector_budget_exceeded"


@pytest.mark.asyncio
async def test_cancel_during_embedding_never_activates_partial_pack(
    tmp_path,
    monkeypatch,
):
    import knowledge.vector_index as vector_index

    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_batch(_store, **_kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(selected=1, stored=0, state="ready")

    monkeypatch.setattr(vector_index, "index_embedding_batch", _slow_batch)
    monkeypatch.setattr(
        "utils.local_embedding_runtime.get_local_embedding_status",
        lambda: LocalEmbeddingStatus(
            state="ready",
            model_id="fixture",
            dimensions=2,
        ),
    )

    task = asyncio.create_task(
        process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert cancel_pack_job(tmp_path, str(job["job_id"])) is True
    release.set()
    result = await task

    assert result["state"] == "cancelled"
    assert service.list_pack_jobs("meme")[0]["state"] == "cancelled"
    assert service.search("meme", "Staged phrase", limit=1) == []
