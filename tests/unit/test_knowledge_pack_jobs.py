from __future__ import annotations

import hashlib
import json
import threading

import pytest

from knowledge.store import KnowledgeStore
from knowledge.pack_jobs import _pack_payload, _prepare_job
from knowledge.pack_jobs import (
    KnowledgeJobRegistryError,
    cancel_pack_job,
    discard_degraded_pack_job,
    process_pack_jobs,
)
from knowledge.packs import validate_pack
from knowledge.prebuilt_index import (
    PREBUILT_DIMENSIONS,
    PREBUILT_MODEL_ID,
    build_prebuilt_index_artifacts,
)
from knowledge.chunking import derive_knowledge_chunks
from knowledge.service import KnowledgeService
from knowledge.subscriptions import canonical_pack_bytes


@pytest.mark.asyncio
async def test_process_pack_jobs_lists_state_off_the_event_loop(tmp_path, monkeypatch):
    import knowledge.pack_jobs as module

    service = KnowledgeService.from_root(tmp_path)
    event_loop_thread = threading.get_ident()
    list_threads: list[int] = []
    cleanup_threads: list[int] = []

    def tracked_list(_root):
        list_threads.append(threading.get_ident())
        return ({"job_id": "finished-job", "state": "active", "created_at": 1},)

    def tracked_cleanup(_job_dir):
        cleanup_threads.append(threading.get_ident())

    monkeypatch.setattr(module, "list_pack_jobs", tracked_list)
    monkeypatch.setattr(module, "_cleanup_payload", tracked_cleanup)

    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert result["state"] == "no_work"
    assert list_threads and all(thread_id != event_loop_thread for thread_id in list_threads)
    assert cleanup_threads and all(
        thread_id != event_loop_thread for thread_id in cleanup_threads
    )


def _pack(
    *,
    title: str = "Staged phrase",
    pack_id: str = "staged-fixture",
    content: str = "A staged entry body.",
):
    return validate_pack(
        {
            "schema_version": 1,
            "pack_id": pack_id,
            "material_type": "knowledge",
            "source": {"name": "Fixture", "homepage": "", "license": "CC0"},
            "entries": [
                {
                    "title": title,
                    "terms": {"alias": [], "recognition": []},
                    "tags": [],
                    "summary": "A staged entry",
                    "content": content,
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
        "provider_package_id": "7",
        "remote_id": f"knowledge/{pack.pack_id}",
        "version": "1.0.0",
        "channel": "stable",
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "material_type": pack.material_type,
        "index_manifest_sha256": artifacts.manifest_sha256,
        "vectors_sha256": artifacts.vectors_sha256,
        "trust": "trusted_market",
    }
    return artifacts, subscription


def test_prebuilt_verification_resumes_from_persisted_state(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    pack = _pack()
    artifacts, subscription = _prebuilt(pack)
    job = service.stage_pack(
        pack,
        subscription=subscription,
        index_manifest=artifacts.manifest,
        vectors=artifacts.vectors,
    )
    job_dir = tmp_path / ".staging" / str(job["job_id"])

    first = _prepare_job(job_dir)
    resumed = _prepare_job(job_dir)

    assert first["state"] == "verifying_index"
    assert resumed["state"] == "verifying_index"
    assert resumed["index_validation"] == "accepted"
    assert KnowledgeStore(job_dir / "knowledge.db").chunk_status()["chunks_ready"] == 1


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
    status = KnowledgeStore(service.database_path()).chunk_status()

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


def test_cancel_and_remove_reports_staged_only_success(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    service.stage_pack(_pack())

    result = service.cancel_and_remove_pack("staged-fixture")

    assert result == {
        "removed_pack": False,
        "removed_entries": 0,
        "cancelled_jobs": 1,
    }
    assert service.list_pack_jobs()[0]["state"] == "cancelled"


def test_market_cancel_and_remove_preserves_same_named_local_pack(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    pack = _pack()
    service.install_pack(pack)
    service.stage_pack(pack)

    with pytest.raises(PermissionError, match="identity"):
        service.cancel_and_remove_pack(
            "staged-fixture",
            expected_provider="plugin-market",
            expected_provider_package_id="7",
            expected_remote_id="knowledge/staged-fixture",
        )

    assert service.list_packs()[0]["pack_id"] == "staged-fixture"
    assert service.list_pack_jobs()[0]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_remove_pack_cancels_its_staged_replacement_before_activation(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    service.install_pack(_pack(title="Installed phrase"))
    job = service.stage_pack(_pack(title="Replacement phrase"))

    assert service.remove_pack("staged-fixture") == 1
    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert result["state"] == "no_work"
    assert service.list_pack_jobs()[0]["state"] == "cancelled"
    assert service.list_packs() == ()
    assert service.search("Replacement phrase") == []


def test_pack_chunk_budget_is_enforced(monkeypatch):
    import knowledge.packs as packs

    monkeypatch.setattr(packs, "MAX_PACK_PROJECTED_CHUNKS", 0)
    with pytest.raises(ValueError, match="too many chunks"):
        _pack()


def test_community_entry_budget_counts_pending_packs(tmp_path, monkeypatch):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    monkeypatch.setattr(pack_jobs, "MAX_COMMUNITY_ENTRIES", 1)
    job = service.stage_pack(_pack(pack_id="first-pack"))
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    persisted = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))

    assert not set(pack_jobs.IDENTITY_CAPACITY_FIELDS).intersection(persisted)
    assert service.list_pack_jobs()[0]["entries_total"] == 1

    with pytest.raises(ValueError, match="too many entries"):
        service.stage_pack(_pack(pack_id="second-pack"))


def test_corrupt_job_state_is_quarantined_and_still_counts_capacity(
    tmp_path,
    monkeypatch,
):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack(pack_id="first-pack"))
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    assert (job_dir / "identity.json").is_file()
    assert not tuple((tmp_path / ".staging").glob(".creating-*"))
    (job_dir / "state.json").write_text("{", encoding="utf-8")

    listed = service.list_pack_jobs()[0]
    assert listed["state"] == "degraded"
    assert listed["reason"] == "invalid_job_state"
    assert listed["entries_total"] == 1
    assert service.get_status()["pack_job_registry_state"] == "invalid"
    monkeypatch.setattr(pack_jobs, "MAX_COMMUNITY_ENTRIES", 1)

    with pytest.raises(ValueError, match="too many entries"):
        service.stage_pack(_pack(pack_id="second-pack"))


def test_staged_chunk_total_must_match_identity(tmp_path, monkeypatch):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    original_chunk_status = KnowledgeStore.chunk_status

    def mismatched_chunk_status(store):
        status = original_chunk_status(store)
        if store.database_path.parent == job_dir:
            status["chunks_total"] = int(status["chunks_total"]) + 1
        return status

    monkeypatch.setattr(KnowledgeStore, "chunk_status", mismatched_chunk_status)

    state = _prepare_job(job_dir)

    assert state["state"] == "degraded"
    assert state["reason"] == "job_capacity_identity_mismatch"
    persisted = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert "chunks_total" not in persisted


@pytest.mark.parametrize("field", ("created_at", "updated_at"))
@pytest.mark.parametrize("value", ("not-a-time", -1, 1.5, True))
def test_invalid_job_timestamps_are_quarantined(tmp_path, field, value):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    state_path = tmp_path / ".staging" / str(job["job_id"]) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[field] = value
    state_path.write_text(json.dumps(state), encoding="utf-8")

    listed = service.list_pack_jobs()[0]

    assert listed["state"] == "degraded"
    assert listed["reason"] == "invalid_job_timestamps"
    assert service.get_status()["pack_job_registry_state"] == "invalid"


@pytest.mark.parametrize("value", (True, 1.5))
def test_invalid_identity_timestamp_cannot_supply_state_fallback(tmp_path, value):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    identity_path = job_dir / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["created_at"] = value
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    state_path = job_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("created_at")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    listed = service.list_pack_jobs()[0]

    assert listed["state"] == "degraded"
    assert listed["reason"] == "invalid_job_identity"
    assert service.get_status()["pack_job_registry_state"] == "invalid"


@pytest.mark.asyncio
async def test_quarantined_job_is_not_processed_or_cleaned(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    (job_dir / "state.json").unlink()

    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert result["state"] == "no_work"
    assert job_dir.is_dir()
    assert service.list_pack_jobs()[0]["reason"] == "missing_job_state"
    assert service.list_packs() == ()


def test_orphan_creation_directory_blocks_staging_until_explicit_discard(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    orphan = tmp_path / ".staging" / ".creating-crashed"
    orphan.mkdir(parents=True)
    (orphan / "partial").write_bytes(b"partial")

    with pytest.raises(
        KnowledgeJobRegistryError,
        match="knowledge_job_registry_invalid",
    ):
        service.stage_pack(_pack())

    listed = service.list_pack_jobs()[0]
    assert listed["state"] == "degraded"
    assert listed["orphan"] is True
    assert discard_degraded_pack_job(tmp_path, listed["job_id"]) is True
    assert service.stage_pack(_pack())["state"] == "queued"


def test_discard_only_removes_degraded_jobs(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    job_id = str(job["job_id"])
    job_dir = tmp_path / ".staging" / job_id

    assert discard_degraded_pack_job(tmp_path, job_id) is False
    (job_dir / "state.json").write_text("[]", encoding="utf-8")
    assert discard_degraded_pack_job(tmp_path, "../outside") is False
    assert discard_degraded_pack_job(tmp_path, job_id) is True
    assert not job_dir.exists()


def test_terminal_job_history_is_pruned_by_count_without_deleting_degraded(
    tmp_path,
    monkeypatch,
):
    import knowledge.pack_jobs as pack_jobs

    jobs_root = tmp_path / ".staging"
    jobs_root.mkdir()
    for index in range(3):
        job_id = f"terminal-{index}"
        job_dir = jobs_root / job_id
        job_dir.mkdir()
        identity = {
            "job_id": job_id,
            "pack_id": f"pack-{index}",
            "created_at": index + 1,
            "entries_total": 1,
            "chunks_total": 1,
            "content_bytes": 1,
        }
        (job_dir / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
        (job_dir / "state.json").write_text(
            json.dumps({**identity, "state": "cancelled", "updated_at": index + 1}),
            encoding="utf-8",
        )
    degraded = jobs_root / "degraded-job"
    degraded.mkdir()
    (degraded / "state.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(pack_jobs, "MAX_TERMINAL_JOB_DIRECTORIES", 2)
    monkeypatch.setattr(pack_jobs, "TERMINAL_JOB_TTL_SECONDS", 10**12)

    listed = pack_jobs.list_pack_jobs(tmp_path)

    assert not (jobs_root / "terminal-0").exists()
    assert (jobs_root / "terminal-1").is_dir()
    assert (jobs_root / "terminal-2").is_dir()
    assert degraded.is_dir()
    assert {item["job_id"] for item in listed} == {
        "terminal-1",
        "terminal-2",
        "degraded-job",
    }


def test_job_is_only_listed_after_atomic_publication(tmp_path, monkeypatch):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    listed = []
    original_write = pack_jobs.atomic_write_bytes

    def paused_write(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(pack_jobs, "atomic_write_bytes", paused_write)
    stage_thread = threading.Thread(target=lambda: service.stage_pack(_pack()))
    list_thread = threading.Thread(target=lambda: listed.extend(service.list_pack_jobs()))
    stage_thread.start()
    assert entered.wait(timeout=3)
    list_thread.start()
    assert list_thread.is_alive()
    release.set()
    stage_thread.join(timeout=3)
    list_thread.join(timeout=3)

    assert not stage_thread.is_alive()
    assert not list_thread.is_alive()
    assert [job["state"] for job in listed] == ["queued"]


@pytest.mark.asyncio
async def test_job_without_identity_is_only_quarantined_and_discarded(tmp_path):
    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack())
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    (job_dir / "identity.json").unlink()

    listed = service.list_pack_jobs()[0]
    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert listed["state"] == "degraded"
    assert listed["reason"] == "invalid_job_identity"
    assert listed["orphan"] is True
    assert result["state"] == "no_work"
    assert cancel_pack_job(tmp_path, str(job["job_id"])) is False
    assert service.list_packs() == ()
    assert discard_degraded_pack_job(tmp_path, str(job["job_id"])) is True


@pytest.mark.parametrize(
    ("field", "limit_name"),
    (
        ("entries_total", "MAX_COMMUNITY_ENTRIES"),
        ("chunks_total", "MAX_COMMUNITY_CHUNKS"),
        ("content_bytes", "MAX_COMMUNITY_CONTENT_BYTES"),
    ),
)
@pytest.mark.parametrize("value", (0, -1, True, 1.5, 2))
def test_state_capacity_cannot_override_identity(
    tmp_path,
    monkeypatch,
    field,
    limit_name,
    value,
):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    job = service.stage_pack(_pack(pack_id="first-pack"))
    job_dir = tmp_path / ".staging" / str(job["job_id"])
    identity = json.loads((job_dir / "identity.json").read_text(encoding="utf-8"))
    state_path = job_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[field] = value
    state_path.write_text(json.dumps(state), encoding="utf-8")

    listed = service.list_pack_jobs()[0]

    assert listed["state"] == "degraded"
    assert listed["reason"] == "job_capacity_identity_mismatch"
    assert listed[field] == identity[field]
    monkeypatch.setattr(pack_jobs, limit_name, identity[field])
    with pytest.raises(ValueError):
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
        ready_vector_chunks=20_000,
    )
    job = service.list_pack_jobs()[0]

    assert result["state"] == "ready_bm25"
    assert job["state"] == "active"
    assert job["retrieval_mode"] == "bm25"
    assert job["index_fallback_reason"] == "vector_budget_exceeded"


@pytest.mark.asyncio
async def test_vector_budget_subtracts_replaced_pack_vectors(tmp_path, monkeypatch):
    import knowledge.pack_jobs as pack_jobs

    service = KnowledgeService.from_root(tmp_path)
    pack = _pack(title="Old phrase")
    artifacts, subscription = _prebuilt(pack)
    service.stage_pack(
        pack,
        subscription=subscription,
        index_manifest=artifacts.manifest,
        vectors=artifacts.vectors,
    )
    assert (
        await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)
    )["state"] == "ready_hybrid"

    replacement = _pack(title="New phrase")
    artifacts, subscription = _prebuilt(replacement)
    service.stage_pack(
        replacement,
        subscription=subscription,
        index_manifest=artifacts.manifest,
        vectors=artifacts.vectors,
    )
    monkeypatch.setattr(pack_jobs, "MAX_READY_VECTOR_CHUNKS", 1)

    result = await process_pack_jobs(
        service,
        batch_size=4,
        ready_vector_chunks=1,
    )

    assert result["state"] == "ready_hybrid"
    assert service.search("Old phrase", limit=1) == []
    assert service.search("New phrase", limit=1)


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


@pytest.mark.asyncio
async def test_routing_refresh_failure_does_not_relabel_committed_pack(
    tmp_path, monkeypatch
):
    service = KnowledgeService.from_root(tmp_path)
    service.stage_pack(_pack())

    def fail_refresh(*_args, **_kwargs):
        raise OSError("refresh unavailable")

    monkeypatch.setattr(service, "refresh_routing_index", fail_refresh)
    result = await process_pack_jobs(service, batch_size=4, ready_vector_chunks=0)

    assert result["state"] == "ready_bm25"
    assert service.list_pack_jobs()[0]["state"] == "active"
    assert service.list_packs()[0]["pack_id"] == "staged-fixture"
