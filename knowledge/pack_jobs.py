"""Persistent, bounded staging jobs for user-supplied knowledge packs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
DEGRADED_STATE = "degraded"
TERMINAL_JOB_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TERMINAL_JOB_DIRECTORIES = 100
PACK_ARTIFACT_NAME = "pack.neko-knowledge.json"
INDEX_MANIFEST_NAME = "pack.neko-knowledge.index.json"
VECTOR_ARTIFACT_NAME = "pack.neko-knowledge.vectors.f16"
IDENTITY_NAME = "identity.json"
logger = logging.getLogger(__name__)


class KnowledgeJobRegistryError(ValueError):
    """Raised when staging state cannot be trusted for capacity decisions."""


@dataclass(frozen=True, slots=True)
class _JsonReadResult:
    state: Literal["valid", "missing", "invalid", "unreadable"]
    payload: dict[str, Any]


def _jobs_root(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / STAGING_DIRECTORY


def _state_path(job_dir: Path) -> Path:
    return job_dir / "state.json"


def _identity_path(job_dir: Path) -> Path:
    return job_dir / IDENTITY_NAME


def pack_operation_lock(knowledge_root: str | Path, pack_id: str):
    """Serialize staging, activation, and removal for one pack identity."""
    digest = hashlib.sha256(str(pack_id).encode("utf-8")).hexdigest()
    return mutation_lock(_jobs_root(knowledge_root) / f".pack-operation-{digest}")


def _pack_payload(pack: KnowledgePack) -> dict[str, object]:
    """Compatibility wrapper for older internal callers and tests."""
    return pack_payload(pack)


def _read_json_result(path: Path) -> _JsonReadResult:
    for attempt in range(3):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return _JsonReadResult("missing", {})
        except OSError:
            if attempt == 2:
                return _JsonReadResult("unreadable", {})
            time.sleep(0.01)
        except UnicodeError:
            return _JsonReadResult("invalid", {})
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _JsonReadResult("invalid", {})
    if not isinstance(payload, dict):
        return _JsonReadResult("invalid", {})
    return _JsonReadResult("valid", payload)


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_result(path).payload


def _validated_identity_payload(
    job_dir: Path,
    payload: dict[str, Any],
) -> _JsonReadResult:
    job_id = str(payload.get("job_id") or "")
    pack_id = str(payload.get("pack_id") or "")
    counters = {
        key: _normalized_job_timestamp(payload.get(key))
        for key in ("created_at", "entries_total", "chunks_total", "content_bytes")
    }
    if any(value is None for value in counters.values()):
        return _JsonReadResult("invalid", {})
    if (
        job_id != job_dir.name
        or Path(job_id).name != job_id
        or not pack_id
        or Path(pack_id).name != pack_id
    ):
        return _JsonReadResult("invalid", {})
    return _JsonReadResult(
        "valid",
        {"job_id": job_id, "pack_id": pack_id, **counters},
    )


def _validated_identity(job_dir: Path) -> _JsonReadResult:
    result = _read_json_result(_identity_path(job_dir))
    if result.state != "valid":
        return result
    return _validated_identity_payload(job_dir, result.payload)


def _degraded_job(
    job_dir: Path,
    *,
    reason: str,
    identity: dict[str, Any] | None = None,
    orphan: bool = False,
) -> dict[str, object]:
    try:
        created_at = int(job_dir.stat().st_mtime)
    except OSError:
        created_at = 0
    return {
        **(identity or {}),
        "job_id": str((identity or {}).get("job_id") or job_dir.name),
        "state": DEGRADED_STATE,
        "retrieval_mode": "none",
        "reason": reason,
        "created_at": int((identity or {}).get("created_at") or created_at),
        "updated_at": created_at,
        "orphan": bool(orphan),
    }


def _normalized_job_timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _read_job(job_dir: Path) -> dict[str, object]:
    if job_dir.name.startswith(".creating-"):
        return _degraded_job(
            job_dir,
            reason="incomplete_job_creation",
            orphan=True,
        )
    state_result = _read_json_result(_state_path(job_dir))
    identity_result = _validated_identity(job_dir)
    if identity_result.state == "missing" and state_result.state == "valid":
        identity_result = _validated_identity_payload(job_dir, state_result.payload)
    if identity_result.state in {"invalid", "unreadable"}:
        return _degraded_job(
            job_dir,
            reason="invalid_job_identity",
            orphan=True,
        )
    if state_result.state == "valid":
        state = state_result.payload
        if identity_result.state == "valid" and any(
            str(state.get(key) or "") != str(identity_result.payload.get(key) or "")
            for key in ("job_id", "pack_id")
        ):
            return _degraded_job(
                job_dir,
                reason="job_identity_mismatch",
                identity=identity_result.payload,
            )
        fallback_created_at = (
            identity_result.payload.get("created_at")
            if identity_result.state == "valid"
            else None
        )
        created_at = _normalized_job_timestamp(
            state.get("created_at", fallback_created_at)
        )
        updated_at = _normalized_job_timestamp(
            state.get("updated_at", created_at)
        )
        if created_at is None or updated_at is None:
            return _degraded_job(
                job_dir,
                reason="invalid_job_timestamps",
                identity=(
                    identity_result.payload
                    if identity_result.state == "valid"
                    else None
                ),
                orphan=identity_result.state != "valid",
            )
        state["created_at"] = created_at
        state["updated_at"] = updated_at
        return state
    if identity_result.state == "valid":
        reason = {
            "missing": "missing_job_state",
            "invalid": "invalid_job_state",
            "unreadable": "unreadable_job_state",
        }[state_result.state]
        return _degraded_job(
            job_dir,
            reason=reason,
            identity=identity_result.payload,
        )
    return _degraded_job(
        job_dir,
        reason="invalid_job_identity",
        orphan=True,
    )


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
        creating_dir = jobs_root / f".creating-{uuid.uuid4().hex}"
        creating_dir.mkdir()
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
            atomic_write_json(
                _identity_path(creating_dir),
                {
                    "job_id": job_id,
                    "pack_id": pack.pack_id,
                    "created_at": now,
                    "entries_total": preflight.entries,
                    "chunks_total": preflight.projected_chunks,
                    "content_bytes": preflight.content_bytes,
                },
                ensure_ascii=False,
                indent=2,
            )
            atomic_write_bytes(
                creating_dir / PACK_ARTIFACT_NAME,
                canonical_pack_bytes(_pack_payload(pack)),
            )
            if has_prebuilt:
                atomic_write_bytes(creating_dir / INDEX_MANIFEST_NAME, index_manifest)
                atomic_write_bytes(creating_dir / VECTOR_ARTIFACT_NAME, vectors)
            if subscription is not None:
                atomic_write_json(
                    creating_dir / "subscription.json",
                    subscription,
                    ensure_ascii=False,
                )
            atomic_write_json(
                _state_path(creating_dir), state, ensure_ascii=False, indent=2
            )
            creating_dir.replace(job_dir)
        except Exception:
            shutil.rmtree(creating_dir, ignore_errors=True)
            raise
    from .indexer import notify_knowledge_index_changed

    notify_knowledge_index_changed()
    return state


def _ensure_community_capacity(service, pack: KnowledgePack, preflight) -> None:
    all_jobs = list_pack_jobs(service.knowledge_root)
    if any(job.get("orphan") is True for job in all_jobs):
        raise KnowledgeJobRegistryError("knowledge_job_registry_invalid")
    pending = [job for job in all_jobs if job.get("state") not in TERMINAL_STATES]
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
    with mutation_lock(jobs_root):
        if not jobs_root.is_dir():
            return ()
        job_dirs = tuple(
            job_dir
            for job_dir in jobs_root.iterdir()
            if job_dir.is_dir() and not job_dir.is_symlink()
        )
        items = [_read_job(job_dir) for job_dir in job_dirs]
        _prune_terminal_jobs(jobs_root, job_dirs, items)
        items = [
            item
            for job_dir, item in zip(job_dirs, items, strict=True)
            if job_dir.is_dir()
        ]
    return tuple(
        sorted(
            items,
            key=lambda item: (
                -int(item.get("created_at") or 0),
                str(item.get("job_id") or ""),
            ),
        )
    )


def _prune_terminal_jobs(
    jobs_root: Path,
    job_dirs: tuple[Path, ...],
    items: list[dict[str, object]],
) -> None:
    now = int(time.time())
    candidates: list[tuple[int, Path]] = []
    for job_dir, item in zip(job_dirs, items, strict=True):
        job_id = str(item.get("job_id") or "")
        if (
            item.get("state") not in TERMINAL_STATES
            or not job_id
            or job_id != job_dir.name
            or Path(job_id).name != job_id
        ):
            continue
        updated_at = int(item.get("updated_at") or item.get("created_at") or 0)
        candidates.append((updated_at, job_dir))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1].name))
    for index, (updated_at, job_dir) in enumerate(candidates):
        expired = updated_at > 0 and now - updated_at > TERMINAL_JOB_TTL_SECONDS
        over_count = index >= MAX_TERMINAL_JOB_DIRECTORIES
        if not expired and not over_count:
            continue
        if job_dir.is_symlink() or job_dir.parent.resolve() != jobs_root.resolve():
            continue
        try:
            shutil.rmtree(job_dir)
        except OSError:
            logger.warning("failed to prune terminal knowledge job %s", job_dir.name)


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


def discard_degraded_pack_job(knowledge_root: str | Path, job_id: str) -> bool:
    """Explicitly remove one quarantined job after validating its exact path."""
    if not job_id or Path(job_id).name != job_id:
        return False
    jobs_root = _jobs_root(knowledge_root)
    job_dir = jobs_root / job_id
    with mutation_lock(jobs_root):
        if not job_dir.is_dir() or _read_job(job_dir).get("state") != DEGRADED_STATE:
            return False
        shutil.rmtree(job_dir)
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
        state = _read_job(job_dir)
        if state.get("state") == DEGRADED_STATE:
            return state
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
        current = _read_job(job_dir)
        if current.get("state") == DEGRADED_STATE:
            return current
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


def _list_jobs_for_processing(knowledge_root: Path) -> tuple[dict[str, object], ...]:
    all_jobs = list_pack_jobs(knowledge_root)
    for item in all_jobs:
        item_job_id = str(item.get("job_id") or "")
        if (
            item.get("state") in TERMINAL_STATES
            and item_job_id
            and Path(item_job_id).name == item_job_id
        ):
            _cleanup_payload(_jobs_root(knowledge_root) / item_job_id)
    return all_jobs


async def process_pack_jobs(
    service,
    *,
    batch_size: int,
    ready_vector_chunks: int,
) -> dict[str, object]:
    """Verify and activate at most one staged community pack."""

    all_jobs = await asyncio.to_thread(
        _list_jobs_for_processing,
        service.knowledge_root,
    )
    jobs = [
        item
        for item in reversed(all_jobs)
        if item.get("state") not in TERMINAL_STATES | {DEGRADED_STATE}
    ]
    if not jobs:
        return {"state": "no_work", "selected": 0, "stored": 0}
    state = jobs[0]
    job_dir = _jobs_root(service.knowledge_root) / str(state["job_id"])
    try:
        state = await asyncio.to_thread(_prepare_job, job_dir)
        if state.get("state") == DEGRADED_STATE:
            return {"state": DEGRADED_STATE, "selected": 0, "stored": 0}
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
            if activated.get("state") == DEGRADED_STATE:
                return {"state": DEGRADED_STATE, "selected": 0, "stored": 0}
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
            if activated.get("state") == DEGRADED_STATE:
                return {"state": DEGRADED_STATE, "selected": 0, "stored": 0}
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
        if activated.get("state") == DEGRADED_STATE:
            return {"state": DEGRADED_STATE, "selected": 0, "stored": 0}
        if activated.get("state") == "cancelled":
            return {"state": "cancelled", "selected": 0, "stored": 0}
        return {"state": "ready_bm25", "selected": 0, "stored": 0}
    except Exception as exc:
        current = _read_job(job_dir)
        if current.get("state") == DEGRADED_STATE:
            return {"state": DEGRADED_STATE, "selected": 0, "stored": 0}
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
