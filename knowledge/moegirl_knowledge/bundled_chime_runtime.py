"""Lifecycle-safe operations for the fixed CHIME package asset."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.file_utils import atomic_write_json

from .store import MoegirlKnowledgeStore
from .sources import (
    CHIME_COMMIT,
    CHIME_DATASET_URL,
    CHIME_LICENSE,
    load_bundled_chime_dataset,
)


_chime_task: asyncio.Task | None = None


def get_chime_state_path(config_manager) -> Path:
    return Path(config_manager.knowledge_dir) / "moegirl-knowledge" / "chime_state.json"


def get_public_knowledge_status(config_manager) -> dict:
    """Return source-scoped, content-free diagnostics for the local UI."""
    root = Path(config_manager.knowledge_dir) / "moegirl-knowledge"
    store = MoegirlKnowledgeStore(root / "knowledge.db")
    chime_state = _load_public_state(root / "chime_state.json")
    moegirl_state = _load_public_state(root / "sync_state.json")
    return {
        "database": {"entries": store.count(), "integrity_ok": store.integrity_ok()},
        "sources": {
            "chime": {
                "status": chime_state.get("status", "not_imported"),
                "entries": store.count_by_id_prefix("chime:"),
                "last_success_at": chime_state.get("last_success_at", ""),
                "version": chime_state.get("commit", CHIME_COMMIT),
                "license": CHIME_LICENSE,
                "source_url": CHIME_DATASET_URL,
            },
            "moegirl": {
                "status": moegirl_state.get("status", "not_synced"),
                "entries": store.count_by_id_prefix("moegirl:"),
                "last_success_at": moegirl_state.get("last_success_at", ""),
                "failed": _safe_nonnegative_int(moegirl_state.get("failed")),
            },
        },
    }


def request_bundled_chime_reimport(config_manager, logger) -> str:
    """Schedule an idempotent local reimport for an authenticated UI request."""
    if not config_manager.ensure_knowledge_directory():
        return "unavailable"
    root = Path(config_manager.knowledge_dir) / "moegirl-knowledge"
    return _schedule_bundled_chime_import(root / "knowledge.db", root / "chime_state.json", logger)


def schedule_bundled_chime_import(config_manager, logger) -> str:
    """Schedule the normal startup import after storage ownership is ready."""
    root = Path(config_manager.knowledge_dir) / "moegirl-knowledge"
    return _schedule_bundled_chime_import(root / "knowledge.db", root / "chime_state.json", logger)


def _schedule_bundled_chime_import(database_path: Path, state_path: Path, logger) -> str:
    global _chime_task
    if _chime_task is not None and not _chime_task.done():
        return "already_running"
    _chime_task = asyncio.create_task(
        _import_bundled_chime(database_path, state_path, logger),
        name="moegirl-knowledge-chime-import",
    )
    return "scheduled"


async def stop_bundled_chime_import() -> None:
    global _chime_task
    if _chime_task is not None:
        _chime_task.cancel()
        try:
            await _chime_task
        except asyncio.CancelledError:
            pass
    _chime_task = None


async def _import_bundled_chime(database_path: Path, state_path: Path, logger) -> None:
    """Import only the package asset; no network or third-party code is run."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        dataset = await asyncio.to_thread(load_bundled_chime_dataset)
        store = MoegirlKnowledgeStore(database_path)
        results = await asyncio.to_thread(store.upsert_many, dataset.entries)
        status = {
            "status": "ready",
            "commit": dataset.commit,
            "sha256": dataset.sha256,
            "entries": len(dataset.entries),
            "added": sum(result.created for result in results),
            "updated": sum(result.updated for result in results),
            "unchanged": sum(result.unchanged for result in results),
            "last_success_at": now,
        }
        await asyncio.to_thread(atomic_write_json, state_path, status, ensure_ascii=False, indent=2)
        logger.info(
            "[moegirl-knowledge] bundled CHIME status=ready entries=%d added=%d updated=%d unchanged=%d",
            status["entries"], status["added"], status["updated"], status["unchanged"],
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status = {"status": "degraded", "error_type": type(exc).__name__, "updated_at": now}
        try:
            await asyncio.to_thread(atomic_write_json, state_path, status, ensure_ascii=False, indent=2)
        except Exception:
            pass
        logger.warning("[moegirl-knowledge] bundled CHIME import failed: %s", type(exc).__name__)


def _load_public_state(state_path: Path) -> dict:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    allowed_keys = {"status", "last_success_at", "commit", "failed"}
    return {key: payload[key] for key in allowed_keys if key in payload}


def _safe_nonnegative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
