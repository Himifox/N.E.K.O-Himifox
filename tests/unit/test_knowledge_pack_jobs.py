from __future__ import annotations

import hashlib

import pytest

from knowledge.moegirl_knowledge.store import MoegirlKnowledgeStore
from knowledge.pack_jobs import cancel_pack_job, process_pack_jobs
from knowledge.pack_jobs import _pack_payload
from knowledge.packs import validate_pack
from knowledge.prebuilt_index import (
    PREBUILT_DIMENSIONS,
    PREBUILT_MODEL_ID,
    build_prebuilt_index_artifacts,
)
from knowledge.chunking import derive_knowledge_chunks
from knowledge.service import KnowledgeService
from knowledge.subscriptions import canonical_pack_bytes


def _pack(*, title: str = "Staged phrase", pack_id: str = "staged-fixture"):
    return validate_pack(
        {
            "schema_version": 3,
            "pack_id": pack_id,
            "material_type": "knowledge",
            "source": {"name": "Fixture", "homepage": "", "license": "CC0"},
            "entries": [
                {
                    "title": title,
                    "terms": {"alias": [], "recognition": []},
                    "tags": [],
                    "summary": "A staged entry",
                    "content": "A staged entry body.",
                }
            ],
        }
    )


def _prebuilt(pack):
    raw = canonical_pack_bytes(_pack_payload(pack))
    chunks = tuple(
        chunk
        for entry in pack.entries
        for chunk in derive_knowledge_chunks(
            entry,
            entry_key=f"{entry.source_tag}:{entry.title}",
        )
    )
    row = bytes.fromhex("003c") + b"\0" * ((PREBUILT_DIMENSIONS - 1) * 2)
    artifacts = build_prebuilt_index_artifacts(
        raw,
        [
            {
                "chunk_id": chunk.chunk_id,
                "content_hash": chunk.content_hash,
                "model_id": PREBUILT_MODEL_ID,
                "dimensions": PREBUILT_DIMENSIONS,
                "embedding": row,
            }
            for chunk in chunks
        ],
    )
    subscription = {
        "provider": "plugin-market",
        "remote_id": f"knowledge/{pack.pack_id}",
        "version": "1.0.0",
        "channel": "stable",
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "index_manifest_sha256": artifacts.manifest_sha256,
        "vectors_sha256": artifacts.vectors_sha256,
        "trust": "trusted_market",
    }
    return artifacts, subscription


@pytest.mark.asyncio
async def test_staged_pack_is_hidden_until_bm25_activation(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())

    assert job["state"] == "queued"
    assert job["material_type"] == "knowledge"
    assert service.search("Staged phrase", limit=1) == []
    assert service.list_packs() == ()

    result = await process_pack_jobs(
        service,
        batch_size=4,
        ready_vector_chunks=0,
    )

    assert result["state"] == "ready_bm25"
    assert service.search("Staged phrase", limit=1)
    assert service.list_packs()[0]["retrieval_mode"] == "bm25"
    assert service.list_pack_jobs()[0]["state"] == "active"


@pytest.mark.asyncio
async def test_pack_update_keeps_old_source_until_new_job_activates(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    service.install_pack(_pack(title="Old phrase"))
    service.stage_pack(_pack(title="New phrase"))

    assert service.search("Old phrase", limit=1)
    assert service.search("New phrase", limit=1) == []

    await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert service.search("Old phrase", limit=1) == []
    assert service.search("New phrase", limit=1)


@pytest.mark.asyncio
async def test_ready_vectors_are_transferred_during_hybrid_activation(
    tmp_path,
):
    service = KnowledgeService.from_root(tmp_path)
    pack = _pack()
    artifacts, subscription = _prebuilt(pack)
    service.stage_pack(
        pack,
        subscription=subscription,
        index_manifest=artifacts.manifest,
        vectors=artifacts.vectors,
    )

    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)
    status = MoegirlKnowledgeStore(service.database_path()).chunk_status()

    assert result["state"] == "ready_hybrid"
    assert status["chunks_ready"] == status["chunks_total"] == 1
    assert service.list_packs()[0]["retrieval_mode"] == "hybrid"


def test_cancelled_job_never_becomes_visible(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())

    assert cancel_pack_job(tmp_path, str(job["job_id"])) is True
    assert cancel_pack_job(tmp_path, str(job["job_id"])) is False
    assert service.list_pack_jobs()[0]["state"] == "cancelled"
    assert not (tmp_path / ".staging" / str(job["job_id"]) / "pack.json").exists()
    assert service.search("Staged phrase", limit=1) == []


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
    pack = _pack()
    artifacts, subscription = _prebuilt(pack)
    service.stage_pack(
        pack,
        subscription=subscription,
        index_manifest=artifacts.manifest,
        vectors=artifacts.vectors,
    )

    result = await process_pack_jobs(
        service,
        batch_size=4,
        ready_vector_chunks=10_000,
    )
    job = service.list_pack_jobs()[0]

    assert result["state"] == "ready_bm25"
    assert job["state"] == "active"
    assert job["retrieval_mode"] == "bm25"
    assert job["index_fallback_reason"] == "vector_budget_exceeded"


@pytest.mark.asyncio
async def test_raw_pack_activation_never_loads_the_embedding_model(
    tmp_path,
    monkeypatch,
):
    service = KnowledgeService.from_root(tmp_path)
    service.stage_pack(_pack())
    monkeypatch.setattr(
        "utils.local_embedding_runtime.get_local_embedding_service",
        lambda: pytest.fail("raw-only packs must not load the embedding model"),
    )
    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)
    assert result["state"] == "ready_bm25"
    assert service.list_packs()[0]["local_embedding_enabled"] is False
