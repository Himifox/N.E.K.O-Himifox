"""Persistent, bounded staging jobs for user-supplied knowledge packs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from utils.file_utils import atomic_write_bytes, atomic_write_json

from ._mutation_lock import mutation_lock
from .store import KnowledgeStore
from .packs import (
    KnowledgePack,
    ensure_install_capacity,
    install_pack,
    pack_payload,
    preflight_pack,
    validate_pack,
)
from .subscriptions import canonical_pack_bytes


STAGING_DIRECTORY = ".staging"
MAX_READY_VECTOR_CHUNKS = 20_000
MAX_COMMUNITY_ENTRIES = 20_000
MAX_COMMUNITY_CHUNKS = 20_000
MAX_COMMUNITY_CONTENT_BYTES = 64 * 1024 * 1024
TERMINAL_STATES = frozenset(("active", "cancelled", "failed"))
PACK_ARTIFACT_NAME = "pack.neko-knowledge.json"
INDEX_MANIFEST_NAME = "pack.neko-knowledge.index.json"
VECTOR_ARTIFACT_NAME = "pack.neko-knowledge.vectors.f16"
logger = logging.getLogger(__name__)


def _jobs_root(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / STAGING_DIRECTORY


def _state_path(job_dir: Path) -> Path:
    return job_dir / "state.json"


def pack_operation_lock(knowledge_root: str | Path, pack_id: str):
    """Serialize staging, activation, and removal for one pack identity."""
    digest = hashlib.sha256(str(pack_id).encode("utf-8")).hexdigest()
    return mutation_lock(_jobs_root(knowledge_root) / f".pack-operation-{digest}")


def _pack_payload(pack: KnowledgePack) -> dict[str, object]:
    """Compatibility wrapper for older internal callers and tests."""
    return pack_payload(pack)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(
    job_dir: Path,
    current: dict[str, Any],
    **changes: object,
) -> dict[str, Any]:
    updated = {**current, **changes, "updated_at": int(time.time())}
    atomic_write_json(_state_path(job_dir), updated, ensure_ascii=False, indent=2)
    return updated


async def _write_state_async(
    job_dir: Path,
    current: dict[str, Any],
    **changes: object,
) -> dict[str, Any]:
    """Persist job state without blocking the coordinator event loop."""
    return await asyncio.to_thread(_write_state, job_dir, current, **changes)


def stage_pack(
    service,
    pack: KnowledgePack,
    *,
    subscription: dict[str, str] | None = None,
    index_manifest: bytes | None = None,
    vectors: bytes | None = None,
    index_fallback_reason: str = "",
) -> dict[str, object]:
    """Persist validated source data without making it searchable yet."""
    root = Path(service.knowledge_root)
    preflight = preflight_pack(pack)
    ensure_install_capacity(root, preflight)
    jobs_root = _jobs_root(root)
    with pack_operation_lock(root, pack.pack_id), mutation_lock(jobs_root):
        _ensure_community_capacity(service, pack, preflight)
        jobs_root.mkdir(parents=True, exist_ok=True)
        job_id = f"{pack.pack_id}-{uuid.uuid4().hex[:12]}"
        job_dir = jobs_root / job_id
        job_dir.mkdir()
        now = int(time.time())
        has_prebuilt = index_manifest is not None and vectors is not None
        state: dict[str, object] = {
            "job_id": job_id,
            "pack_id": pack.pack_id,
            "material_type": pack.material_type,
            "state": "queued",
            "retrieval_mode": "pending",
            "entries_total": preflight.entries,
            "chunks_total": preflight.projected_chunks,
            "content_bytes": preflight.content_bytes,
            "chunks_ready": 0,
            "indexed_percent": 0.0,
            "reason": "",
            "index_origin": "prebuilt" if has_prebuilt else "none",
            "index_trust": "trusted_market" if has_prebuilt else "none",
            "index_validation": "pending" if has_prebuilt else "absent",
            "index_fallback_reason": str(index_fallback_reason or "")[:80],
            "local_embedding_enabled": False,
            "prebuilt_chunks_ready": 0,
            "prebuilt_chunks_missing": preflight.projected_chunks,
            "created_at": now,
            "updated_at": now,
        }
        try:
            atomic_write_bytes(
                job_dir / PACK_ARTIFACT_NAME,
                canonical_pack_bytes(_pack_payload(pack)),
            )
            if has_prebuilt:
                atomic_write_bytes(job_dir / INDEX_MANIFEST_NAME, index_manifest)
                atomic_write_bytes(job_dir / VECTOR_ARTIFACT_NAME, vectors)
            if subscription is not None:
                atomic_write_json(
                    job_dir / "subscription.json",
                    subscription,
                    ensure_ascii=False,
                )
            atomic_write_json(_state_path(job_dir), state, ensure_ascii=False, indent=2)
        except Exception:
            _cleanup_payload(job_dir)
            try:
                job_dir.rmdir()
            except OSError:
                pass
            raise
    from .indexer import notify_knowledge_index_changed

    notify_knowledge_index_changed()
    return state


def _ensure_community_capacity(service, pack: KnowledgePack, preflight) -> None:
    pending = [
        job
        for job in list_pack_jobs(service.knowledge_root)
        if job.get("state") not in TERMINAL_STATES
    ]
    if any(
        job.get("pack_id") == pack.pack_id
        for job in pending
    ):
        raise ValueError("knowledge pack already has a pending import")

    totals = {"entries_total": 0, "chunks_total": 0, "content_bytes": 0}
    database_path = service.database_path()
    store = KnowledgeStore(database_path)
    if database_path.is_file():
        usage = store.community_usage()
        for key in totals:
            totals[key] += int(usage[key])

    replacement_keys = {str(job.get("pack_id") or "") for job in pending}
    replacement_keys.add(pack.pack_id)
    replacement = {"entries_total": 0, "chunks_total": 0, "content_bytes": 0}
    for pack_id in replacement_keys:
        if not pack_id:
            continue
        usage = store.community_usage(source_tag=f"source:community.{pack_id}")
        for key in replacement:
            replacement[key] += int(usage[key])

    entries = (
        totals["entries_total"]
        - replacement["entries_total"]
        + sum(int(job.get("entries_total") or 0) for job in pending)
        + preflight.entries
    )
    chunks = (
        totals["chunks_total"]
        - replacement["chunks_total"]
        + sum(int(job.get("chunks_total") or 0) for job in pending)
        + preflight.projected_chunks
    )
    content_bytes = (
        totals["content_bytes"]
        - replacement["content_bytes"]
        + sum(int(job.get("content_bytes") or 0) for job in pending)
        + preflight.content_bytes
    )
    if entries > MAX_COMMUNITY_ENTRIES:
        raise ValueError("community knowledge contains too many entries")
    if chunks > MAX_COMMUNITY_CHUNKS:
        raise ValueError("community knowledge would create too many chunks")
    if content_bytes > MAX_COMMUNITY_CONTENT_BYTES:
        raise ValueError("community knowledge exceeds the total content limit")


def list_pack_jobs(
    knowledge_root: str | Path,
) -> tuple[dict[str, object], ...]:
    jobs_root = _jobs_root(knowledge_root)
    if not jobs_root.is_dir():
        return ()
    items: list[dict[str, object]] = []
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir():
            continue
        state = _read_json(_state_path(job_dir))
        if not state:
            continue
        items.append(state)
    return tuple(
        sorted(
            items,
            key=lambda item: (
                -int(item.get("created_at") or 0),
                str(item.get("job_id") or ""),
            ),
        )
    )


def cancel_pack_job(knowledge_root: str | Path, job_id: str) -> bool:
    if not job_id or Path(job_id).name != job_id:
        return False
    job_dir = _jobs_root(knowledge_root) / job_id
    with mutation_lock(_state_path(job_dir)):
        state = _read_json(_state_path(job_dir))
        if not state or state.get("state") in TERMINAL_STATES:
            return False
        _write_state(
            job_dir,
            state,
            state="cancelled",
            retrieval_mode="none",
            reason="cancelled_by_user",
        )
        if state.get("state") != "embedding":
            _cleanup_payload(job_dir)
    return True


def _load_job_pack(job_dir: Path) -> KnowledgePack:
    artifact_path = job_dir / PACK_ARTIFACT_NAME
    if artifact_path.is_file():
        try:
            return validate_pack(json.loads(artifact_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("staged knowledge pack is invalid") from exc
    return validate_pack(_read_json(job_dir / "pack.json"))


def _subscription(job_dir: Path) -> dict[str, str] | None:
    payload = _read_json(job_dir / "subscription.json")
    if not payload:
        return None
    return {str(key): str(value) for key, value in payload.items()}


def _cleanup_payload(job_dir: Path) -> None:
    for name in (
        "pack.json",
        PACK_ARTIFACT_NAME,
        INDEX_MANIFEST_NAME,
        VECTOR_ARTIFACT_NAME,
        "subscription.json",
        "knowledge.db",
        "knowledge.db-wal",
        "knowledge.db-shm",
    ):
        try:
            (job_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def _prepare_job(job_dir: Path) -> dict[str, Any]:
    """Build the staging FTS/chunks off the event loop and resume idempotently."""
    with mutation_lock(_state_path(job_dir)):
        state = _read_json(_state_path(job_dir))
        if not state or state.get("state") in TERMINAL_STATES:
            return state
        staging_store: KnowledgeStore | None = None
        if state.get("state") in {"queued", "validating", "building_fts"}:
            state = _write_state(job_dir, state, state="building_fts")
            pack = _load_job_pack(job_dir)
            staging_store = KnowledgeStore(job_dir / "knowledge.db")
            staging_store.replace_source(
                pack.source_tag,
                pack.entries,
                embedding_policy="prebuilt_only",
            )
            status = staging_store.chunk_status()
            state = _write_state(
                job_dir,
                state,
                state="verifying_index",
                chunks_total=int(status["chunks_total"]),
            )
        if state.get("state") == "verifying_index":
            if staging_store is None:
                # ``verifying_index`` is a durable restart boundary.  Reopen and
                # reconcile the staging database instead of relying on locals
                # created by the preceding in-process state transition.
                pack = _load_job_pack(job_dir)
                staging_store = KnowledgeStore(job_dir / "knowledge.db")
                staging_store.replace_source(
                    pack.source_tag,
                    pack.entries,
                    embedding_policy="prebuilt_only",
                )
                status = staging_store.chunk_status()
                state = _write_state(
                    job_dir,
                    state,
                    chunks_total=int(status["chunks_total"]),
                )
            manifest_path = job_dir / INDEX_MANIFEST_NAME
            vectors_path = job_dir / VECTOR_ARTIFACT_NAME
            has_manifest = manifest_path.is_file()
            has_vectors = vectors_path.is_file()
            if has_manifest and has_vectors:
                from .prebuilt_index import validate_prebuilt_index

                subscription = _subscription(job_dir) or {}
                try:
                    validated = validate_prebuilt_index(
                        (job_dir / PACK_ARTIFACT_NAME).read_bytes(),
                        manifest_path.read_bytes(),
                        vectors_path.read_bytes(),
                        expected_pack_sha256=str(
                            subscription.get("artifact_sha256") or ""
                        ),
                        expected_manifest_sha256=str(
                            subscription.get("index_manifest_sha256") or ""
                        ),
                        expected_vectors_sha256=str(
                            subscription.get("vectors_sha256") or ""
                        ),
                    )
                    stored = staging_store.store_chunk_embeddings_strict(
                        validated.prepared_embeddings()
                    )
                    total = len(validated.chunks)
                    if stored != total:
                        raise ValueError("prebuilt index import was incomplete")
                    state = _write_state(
                        job_dir,
                        state,
                        index_origin="prebuilt",
                        index_trust="trusted_market",
                        index_validation="accepted",
                        index_fallback_reason="",
                        prebuilt_chunks_ready=total,
                        prebuilt_chunks_missing=0,
                    )
                except (OSError, ValueError):
                    manifest_path.unlink(missing_ok=True)
                    vectors_path.unlink(missing_ok=True)
                    state = _write_state(
                        job_dir,
                        state,
                        index_origin="none",
                        index_trust="none",
                        index_validation="rejected",
                        index_fallback_reason="prebuilt_index_rejected",
                        prebuilt_chunks_ready=0,
                        prebuilt_chunks_missing=int(state.get("chunks_total") or 0),
                    )
            else:
                state = _write_state(
                    job_dir,
                    state,
                    index_origin="none",
                    index_trust="none",
                    index_validation="absent",
                    prebuilt_chunks_ready=0,
                    prebuilt_chunks_missing=int(state.get("chunks_total") or 0),
                )
        return state


def _activate_job(
    service, job_dir: Path, state: dict[str, Any], *, mode: str
) -> dict[str, Any]:
    pack_id = str(state.get("pack_id") or "")
    with pack_operation_lock(service.knowledge_root, pack_id), mutation_lock(
        _state_path(job_dir)
    ):
        current = _read_json(_state_path(job_dir)) or state
        if current.get("state") == "cancelled":
            _cleanup_payload(job_dir)
            return current
        pack = _load_job_pack(job_dir)
        staging_store = KnowledgeStore(job_dir / "knowledge.db")
        embeddings = staging_store.ready_embedding_records() if mode != "bm25" else ()
        result = install_pack(
            service.database_path(),
            pack,
            subscription=_subscription(job_dir),
            prepared_embeddings=embeddings,
            retrieval_mode=mode,
            embedding_policy="prebuilt_only",
            index_metadata={
                "index_origin": current.get("index_origin", "none"),
                "index_trust": current.get("index_trust", "none"),
                "index_validation": current.get("index_validation", "absent"),
                "index_fallback_reason": current.get("index_fallback_reason", ""),
                "prebuilt_chunks_ready": current.get("prebuilt_chunks_ready", 0),
                "prebuilt_chunks_missing": current.get("prebuilt_chunks_missing", 0),
            },
        )
        try:
            state = _write_state(
                job_dir,
                current,
                state="active",
                retrieval_mode=result.retrieval_mode,
                chunks_ready=len(embeddings),
                indexed_percent=(
                    100.0
                    if mode == "hybrid"
                    else float(current.get("indexed_percent") or 0.0)
                ),
                reason="",
            )
        except Exception:
            # install_pack() is the durable commit point.  Keep the staging
            # payload so a later pass can reconcile the journal, but never let
            # an auxiliary state-file failure relabel the live pack as failed.
            logger.exception("knowledge pack committed but active state was not persisted")
            return {
                **current,
                "state": "active",
                "retrieval_mode": result.retrieval_mode,
                "_activation_committed": True,
                "_state_persisted": False,
            }
        try:
            service.refresh_routing_index(background=True)
        except Exception:
            logger.exception("knowledge pack activated but routing refresh failed")
    _cleanup_payload(job_dir)
    return state


def _ready_chunks_replaced_by_job(service, pack_id: str) -> int:
    source_tag = f"source:community.{pack_id}"
    status = KnowledgeStore(service.database_path()).source_chunk_status(source_tag)
    return int(status["chunks_ready"])


async def process_pack_jobs(
    service,
    *,
    batch_size: int,
    ready_vector_chunks: int,
) -> dict[str, object]:
    """Verify and activate at most one staged community pack."""

    all_jobs = list_pack_jobs(service.knowledge_root)
    for item in all_jobs:
        item_job_id = str(item.get("job_id") or "")
        if (
            item.get("state") in TERMINAL_STATES
            and item_job_id
            and Path(item_job_id).name == item_job_id
        ):
            _cleanup_payload(_jobs_root(service.knowledge_root) / item_job_id)
    jobs = [
        item for item in reversed(all_jobs) if item.get("state") not in TERMINAL_STATES
    ]
    if not jobs:
        return {"state": "no_work", "selected": 0, "stored": 0}
    state = jobs[0]
    job_dir = _jobs_root(service.knowledge_root) / str(state["job_id"])
    try:
        state = await asyncio.to_thread(_prepare_job, job_dir)
        if not state or state.get("state") in TERMINAL_STATES:
            return {"state": "no_work", "selected": 0, "stored": 0}

        total, ready, replaced_ready = await asyncio.to_thread(
            _activation_capacity_snapshot,
            service,
            job_dir,
            str(state.get("pack_id") or ""),
        )
        has_prebuilt = state.get("index_validation") == "accepted" and ready == total
        projected_ready = max(ready_vector_chunks - replaced_ready, 0) + total
        if has_prebuilt and projected_ready > MAX_READY_VECTOR_CHUNKS:
            state = await _write_state_async(
                job_dir,
                state,
                index_origin="none",
                index_trust="none",
                index_validation="rejected",
                index_fallback_reason="vector_budget_exceeded",
                prebuilt_chunks_ready=0,
                prebuilt_chunks_missing=total,
            )
            activated = await asyncio.to_thread(
                _activate_job,
                service,
                job_dir,
                state,
                mode="bm25",
            )
            if activated.get("state") == "cancelled":
                return {"state": "cancelled", "selected": 0, "stored": 0}
            return {"state": "ready_bm25", "selected": 0, "stored": 0}

        if has_prebuilt:
            state = await _write_state_async(
                job_dir,
                state,
                chunks_ready=ready,
                indexed_percent=100.0,
            )
            activated = await asyncio.to_thread(
                _activate_job,
                service,
                job_dir,
                state,
                mode="hybrid",
            )
            activation_state = (
                "cancelled" if activated.get("state") == "cancelled" else "ready_hybrid"
            )
            return {
                "state": activation_state,
                "selected": ready,
                "stored": ready,
            }
        activated = await asyncio.to_thread(
            _activate_job,
            service,
            job_dir,
            state,
            mode="bm25",
        )
        if activated.get("state") == "cancelled":
            return {"state": "cancelled", "selected": 0, "stored": 0}
        return {"state": "ready_bm25", "selected": 0, "stored": 0}
    except Exception as exc:
        current = _read_json(_state_path(job_dir)) or state
        if current.get("state") == "cancelled":
            _cleanup_payload(job_dir)
            return {"state": "cancelled", "selected": 0, "stored": 0}
        await _write_state_async(
            job_dir,
            current,
            state="failed",
            retrieval_mode="none",
            reason=type(exc).__name__,
        )
        _cleanup_payload(job_dir)
        return {"state": "failed", "selected": 0, "stored": 0}


def _activation_capacity_snapshot(
    service, job_dir: Path, pack_id: str
) -> tuple[int, int, int]:
    """Read activation capacity inputs off the event-loop thread."""
    status = KnowledgeStore(job_dir / "knowledge.db").chunk_status()
    return (
        int(status["chunks_total"]),
        int(status["chunks_ready"]),
        _ready_chunks_replaced_by_job(service, pack_id),
    )
