"""Main-Server-owned lifecycle for the fixed Corpora demonstration asset."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.file_utils import atomic_write_json

from .corpora_dataset import load_bundled_corpora_dataset
from .moegirl_knowledge.store import MoegirlKnowledgeStore


_corpora_task: asyncio.Task | None = None


def schedule_bundled_corpora_import(config_manager, logger) -> str:
    root = Path(config_manager.knowledge_dir) / "corpora"
    return _schedule_import(root / "knowledge.db", root / "state.json", logger)


def _schedule_import(database_path: Path, state_path: Path, logger) -> str:
    global _corpora_task
    if _corpora_task is not None and not _corpora_task.done():
        return "already_running"
    _corpora_task = asyncio.create_task(
        _import_bundled_corpora(database_path, state_path, logger),
        name="knowledge-corpora-import",
    )
    return "scheduled"


async def stop_bundled_corpora_import() -> None:
    global _corpora_task
    if _corpora_task is not None:
        _corpora_task.cancel()
        try:
            await _corpora_task
        except asyncio.CancelledError:
            pass
    _corpora_task = None


async def _import_bundled_corpora(database_path: Path, state_path: Path, logger) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        dataset = await asyncio.to_thread(load_bundled_corpora_dataset)
        store = MoegirlKnowledgeStore(database_path)
        previous = await asyncio.to_thread(_load_state, state_path)
        source_count = await asyncio.to_thread(
            store.count_by_source_tag,
            "source:corpora",
        )
        if (
            previous.get("status") == "ready"
            and previous.get("sha256") == dataset.sha256
            and source_count == len(dataset.entries)
        ):
            logger.info(
                "[knowledge:corpora] bundled import status=ready entries=%d unchanged_asset=true",
                len(dataset.entries),
            )
            return
        results = await asyncio.to_thread(
            store.replace_source,
            "source:corpora",
            dataset.entries,
        )
        state = {
            "status": "ready",
            "commit": dataset.commit,
            "sha256": dataset.sha256,
            "entries": len(dataset.entries),
            "added": sum(result.created for result in results),
            "last_success_at": now,
        }
        await asyncio.to_thread(
            atomic_write_json,
            state_path,
            state,
            ensure_ascii=False,
            indent=2,
        )
        logger.info(
            "[knowledge:corpora] bundled import status=ready entries=%d",
            state["entries"],
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state = {
            "status": "degraded",
            "error_type": type(exc).__name__,
            "updated_at": now,
        }
        try:
            await asyncio.to_thread(
                atomic_write_json,
                state_path,
                state,
                ensure_ascii=False,
                indent=2,
            )
        except Exception:
            pass
        logger.warning("[knowledge:corpora] bundled import failed: %s", type(exc).__name__)


def _load_state(state_path: Path) -> dict:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
