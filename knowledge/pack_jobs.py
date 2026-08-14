"""Persistent, bounded staging jobs for user-supplied knowledge packs."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from utils.file_utils import atomic_write_json

from ._mutation_lock import mutation_lock
from .moegirl_knowledge.store import MoegirlKnowledgeStore
from .packs import (
    KnowledgePack,
    ensure_install_capacity,
    install_pack,
    preflight_pack,
    validate_pack,
)


STAGING_DIRECTORY = ".staging"
MAX_READY_VECTOR_CHUNKS = 10_000
MAX_COMMUNITY_ENTRIES = 10_000
MAX_COMMUNITY_CHUNKS = 10_000
MAX_COMMUNITY_CONTENT_BYTES = 64 * 1024 * 1024
TERMINAL_STATES = frozenset(("active", "cancelled", "failed"))


def _jobs_root(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / STAGING_DIRECTORY


def _state_path(job_dir: Path) -> Path:
    return job_dir / "state.json"


def _pack_payload(pack: KnowledgePack) -> dict[str, object]:
    return {
        "schema_version": pack.schema_version,
        "pack_id": pack.pack_id,
        "collection_id": pack.collection_id,
        "source": {
            "name": pack.source.name,
            "homepage": pack.source.homepage,
            "license": pack.source.license,
        },
        "entries": [
            {
                "title": entry.title,
                "terms": {
                    role: list(values) for role, values in entry.terms.items()
                },
                "tags": [tag for tag in entry.tags if not tag.startswith("source:")],
                "summary": entry.summary,
                "content": entry.content,
            }
            for entry in pack.entries
        ],
    }


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


def stage_pack(
    service,
    pack: KnowledgePack,
    *,
    subscription: dict[str, str] | None = None,
) -> dict[str, object]:
    """Persist validated source data without making it searchable yet."""
    root = Path(service.knowledge_root)
    preflight = preflight_pack(pack)
    ensure_install_capacity(root, preflight)
    jobs_root = _jobs_root(root)
    with mutation_lock(jobs_root):
        _ensure_community_capacity(service, pack, preflight)
        jobs_root.mkdir(parents=True, exist_ok=True)
        job_id = f"{pack.pack_id}-{uuid.uuid4().hex[:12]}"
        job_dir = jobs_root / job_id
        job_dir.mkdir()
        now = int(time.time())
        state: dict[str, object] = {
            "job_id": job_id,
            "pack_id": pack.pack_id,
            "collection_id": pack.collection_id,
            "state": "queued",
            "retrieval_mode": "pending",
            "entries_total": preflight.entries,
            "chunks_total": preflight.projected_chunks,
            "content_bytes": preflight.content_bytes,
            "chunks_ready": 0,
            "indexed_percent": 0.0,
            "reason": "",
            "created_at": now,
            "updated_at": now,
        }
        try:
            atomic_write_json(
                job_dir / "pack.json",
                _pack_payload(pack),
                ensure_ascii=False,
            )
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
        and job.get("collection_id") == pack.collection_id
        for job in pending
    ):
        raise ValueError("knowledge pack already has a pending import")

    totals = {"entries_total": 0, "chunks_total": 0, "content_bytes": 0}
    stores: dict[str, MoegirlKnowledgeStore] = {}
    for collection_id in service.collection_ids():
        database_path = service.database_path(collection_id)
        if not database_path.is_file():
            continue
        store = MoegirlKnowledgeStore(database_path)
        stores[collection_id] = store
        usage = store.community_usage()
        for key in totals:
            totals[key] += int(usage[key])

    replacement_keys = {
        (str(job.get("collection_id") or ""), str(job.get("pack_id") or ""))
        for job in pending
    }
    replacement_keys.add((pack.collection_id, pack.pack_id))
    replacement = {"entries_total": 0, "chunks_total": 0, "content_bytes": 0}
    for collection_id, pack_id in replacement_keys:
        store = stores.get(collection_id)
        if store is None or not pack_id:
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
    *,
    collection_id: str = "",
) -> tuple[dict[str, object], ...]:
    jobs_root = _jobs_root(knowledge_root)
    if not jobs_root.is_dir():
        return ()
    items: list[dict[str, object]] = []
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir():
            continue
        state = _read_json(_state_path(job_dir))
        if not state or (collection_id and state.get("collection_id") != collection_id):
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
    return validate_pack(_read_json(job_dir / "pack.json"))


def _subscription(job_dir: Path) -> dict[str, str] | None:
    payload = _read_json(job_dir / "subscription.json")
    if not payload:
        return None
    return {str(key): str(value) for key, value in payload.items()}


def _cleanup_payload(job_dir: Path) -> None:
    for name in ("pack.json", "subscription.json", "knowledge.db", "knowledge.db-wal", "knowledge.db-shm"):
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
        if state.get("state") in {"queued", "validating", "building_fts"}:
            state = _write_state(job_dir, state, state="building_fts")
            pack = _load_job_pack(job_dir)
            staging_store = MoegirlKnowledgeStore(job_dir / "knowledge.db")
            staging_store.replace_source(pack.source_tag, pack.entries)
            status = staging_store.chunk_status()
            state = _write_state(
                job_dir,
                state,
                state="embedding",
                chunks_total=int(status["chunks_total"]),
            )
        return state


def _activate_job(service, job_dir: Path, state: dict[str, Any], *, mode: str) -> dict[str, Any]:
    with mutation_lock(_state_path(job_dir)):
        current = _read_json(_state_path(job_dir)) or state
        if current.get("state") == "cancelled":
            _cleanup_payload(job_dir)
            return current
        pack = _load_job_pack(job_dir)
        staging_store = MoegirlKnowledgeStore(job_dir / "knowledge.db")
        embeddings = (
            staging_store.ready_embedding_records() if mode != "bm25" else ()
        )
        result = install_pack(
            service.database_path(pack.collection_id),
            pack,
            subscription=_subscription(job_dir),
            prepared_embeddings=embeddings,
            retrieval_mode=mode,
        )
        service.refresh_routing_index(background=True)
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
    _cleanup_payload(job_dir)
    return state


async def process_pack_jobs(
    service,
    *,
    batch_size: int,
    ready_vector_chunks: int,
) -> dict[str, object]:
    """Advance at most one job and one safe embedding microbatch."""
    from .vector_index import index_embedding_batch
    from utils.local_embedding_runtime import get_local_embedding_status

    all_jobs = list_pack_jobs(service.knowledge_root)
    for item in all_jobs:
        item_job_id = str(item.get("job_id") or "")
        if (
            item.get("state") in TERMINAL_STATES
            and item_job_id
            and Path(item_job_id).name == item_job_id
        ):
            _cleanup_payload(
                _jobs_root(service.knowledge_root) / item_job_id
            )
    jobs = [
        item
        for item in reversed(all_jobs)
        if item.get("state") not in TERMINAL_STATES
    ]
    if not jobs:
        return {"state": "no_work", "selected": 0, "stored": 0}
    state = jobs[0]
    job_dir = _jobs_root(service.knowledge_root) / str(state["job_id"])
    try:
        state = await asyncio.to_thread(_prepare_job, job_dir)
        if not state or state.get("state") in TERMINAL_STATES:
            return {"state": "no_work", "selected": 0, "stored": 0}

        staging_store = MoegirlKnowledgeStore(job_dir / "knowledge.db")
        status = staging_store.chunk_status()
        total = int(status["chunks_total"])
        ready = int(status["chunks_ready"])
        if ready_vector_chunks + total > MAX_READY_VECTOR_CHUNKS:
            activated = await asyncio.to_thread(
                _activate_job,
                service,
                job_dir,
                state,
                mode="bm25",
            )
            if activated.get("state") == "cancelled":
                return {"state": "cancelled", "selected": 0, "stored": 0}
            activated = _write_state(
                job_dir,
                activated,
                reason="vector_budget_exceeded",
            )
            return {"state": "ready_bm25", "selected": 0, "stored": 0}

        embedding_status = get_local_embedding_status()
        if embedding_status.state == "disabled":
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

        result = await index_embedding_batch(
            staging_store,
            batch_size=batch_size,
            load_model=True,
        )
        current = _read_json(_state_path(job_dir))
        if current.get("state") == "cancelled":
            _cleanup_payload(job_dir)
            return {"state": "cancelled", "selected": 0, "stored": 0}
        status = staging_store.chunk_status()
        total = int(status["chunks_total"])
        ready = int(status["chunks_ready"])
        percent = round(100.0 * ready / total, 1) if total else 0.0
        state = _write_state(
            job_dir,
            _read_json(_state_path(job_dir)),
            state="embedding",
            chunks_ready=ready,
            indexed_percent=percent,
        )
        unfinished = (
            int(status["chunks_pending"])
            + int(status["chunks_stale"])
            + int(status["chunks_failed_retryable_now"])
            + int(status["chunks_failed_waiting"])
        )
        if total and ready == total:
            activated = await asyncio.to_thread(
                _activate_job,
                service,
                job_dir,
                state,
                mode="hybrid",
            )
            activation_state = (
                "cancelled"
                if activated.get("state") == "cancelled"
                else "ready_hybrid"
            )
            return {
                "state": activation_state,
                "selected": result.selected,
                "stored": result.stored,
            }
        if unfinished == 0 and int(status["chunks_failed_exhausted"]) > 0:
            mode = "mixed" if ready else "bm25"
            activated = await asyncio.to_thread(
                _activate_job,
                service,
                job_dir,
                state,
                mode=mode,
            )
            activation_state = (
                "cancelled"
                if activated.get("state") == "cancelled"
                else f"ready_{mode}"
            )
            return {
                "state": activation_state,
                "selected": result.selected,
                "stored": result.stored,
            }
        return {"state": result.state, "selected": result.selected, "stored": result.stored}
    except Exception as exc:
        current = _read_json(_state_path(job_dir)) or state
        if current.get("state") == "cancelled":
            _cleanup_payload(job_dir)
            return {"state": "cancelled", "selected": 0, "stored": 0}
        _write_state(
            job_dir,
            current,
            state="failed",
            retrieval_mode="none",
            reason=type(exc).__name__,
        )
        _cleanup_payload(job_dir)
        return {"state": "failed", "selected": 0, "stored": 0}
