"""Lifecycle-safe operations for the fixed CHIME package asset."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.file_utils import atomic_write_json

from .catalog_overrides import entry_key, get_catalog_override_path, load_disabled_entries
from .source_registry import SOURCES
from .store import MoegirlKnowledgeStore
from .sources.chime import (
    CHIME_COMMIT,
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
    disabled = load_disabled_entries(get_catalog_override_path(store.database_path))
    entries = store.list_active_entries()
    existing_keys = {entry_key(entry) for entry in entries}
    disabled_count = len(disabled & existing_keys)
    sources = {}
    for source_tag, source in SOURCES.items():
        count = store.count_by_source_tag(source_tag)
        source_disabled = sum(
            1 for key in disabled & existing_keys if key[0] == source_tag
        )
        source_key = source_tag.removeprefix("source:")
        state = chime_state if source_tag == "source:chime" else {}
        sources[source_key] = {
            "status": state.get("status", "available" if count else "empty"),
            "entries": count,
            "active_entries": count - source_disabled,
            "disabled_entries": source_disabled,
            "last_success_at": state.get("last_success_at", ""),
            "name": source.name,
            "license": source.license,
            "homepage": source.homepage,
            "acquisition": (
                "bundled" if source_tag == "source:chime"
                else "local_import" if source_tag == "source:geng-guide"
                else "isolated"
            ),
        }
        if source_tag == "source:chime":
            sources[source_key]["version"] = state.get("commit", CHIME_COMMIT)
    return {
        "mode": "local_only",
        "remote_acquisition": "isolated",
        "database": {
            "entries": len(entries),
            "active_entries": len(entries) - disabled_count,
            "disabled_entries": disabled_count,
            "integrity_ok": store.integrity_ok(),
        },
        "sources": sources,
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
        results = await asyncio.to_thread(store.replace_source, "source:chime", dataset.entries)
        from knowledge.service import KnowledgeService

        service = KnowledgeService.for_collection("meme", database_path)
        await asyncio.to_thread(service.refresh_routing_index)
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
