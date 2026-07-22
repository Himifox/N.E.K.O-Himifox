"""Main-Server-owned scheduling for the regenerable public knowledge database."""

from __future__ import annotations

import asyncio
from pathlib import Path

from config.moegirl_knowledge_settings import (
    CHIME_KNOWLEDGE_ENABLED,
    MOEGIRL_KNOWLEDGE_ENABLED,
    MOEGIRL_KNOWLEDGE_REQUEST_DELAY_SECONDS,
    MOEGIRL_KNOWLEDGE_REQUEST_TIMEOUT_SECONDS,
    MOEGIRL_KNOWLEDGE_SEED_QUERIES,
    MOEGIRL_KNOWLEDGE_SYNC_INTERVAL_SECONDS,
    MOEGIRL_KNOWLEDGE_SYNC_MAX_ENTRIES,
)
from knowledge.moegirl_knowledge.bundled_chime_runtime import (
    schedule_bundled_chime_import,
    stop_bundled_chime_import,
)
from knowledge.moegirl_knowledge.sources import MoegirlWikiApiSource
from knowledge.moegirl_knowledge.sync import MoegirlKnowledgeSynchronizer


_sync_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_synchronizer: MoegirlKnowledgeSynchronizer | None = None
_logger = None
_remove_post_reply_hook = None


def get_knowledge_paths(config_manager) -> tuple[Path, Path]:
    root = Path(config_manager.knowledge_dir) / "moegirl-knowledge"
    return root / "knowledge.db", root / "sync_state.json"


async def start_moegirl_knowledge_sync(config_manager, logger) -> None:
    """Prepare knowledge sources; remote sync waits for a completed reply."""
    global _stop_event, _synchronizer, _logger, _remove_post_reply_hook
    if not config_manager.ensure_knowledge_directory():
        logger.warning("[moegirl-knowledge] knowledge directory unavailable; runtime disabled")
        return
    database_path, state_path = get_knowledge_paths(config_manager)
    if CHIME_KNOWLEDGE_ENABLED:
        schedule_bundled_chime_import(config_manager, logger)

    if not MOEGIRL_KNOWLEDGE_ENABLED or _synchronizer is not None:
        return
    _stop_event = asyncio.Event()
    source = MoegirlWikiApiSource(
        timeout_seconds=MOEGIRL_KNOWLEDGE_REQUEST_TIMEOUT_SECONDS,
        request_delay_seconds=MOEGIRL_KNOWLEDGE_REQUEST_DELAY_SECONDS,
    )
    _synchronizer = MoegirlKnowledgeSynchronizer(
        database_path,
        state_path,
        source,
        request_delay_seconds=MOEGIRL_KNOWLEDGE_REQUEST_DELAY_SECONDS,
    )

    _logger = logger
    from main_logic.core.turn import register_post_reply_hook

    _remove_post_reply_hook = register_post_reply_hook(request_moegirl_knowledge_sync)


def request_moegirl_knowledge_sync() -> None:
    """Start one cancellable sync loop immediately after a delivered reply."""
    global _sync_task
    if _synchronizer is None or _stop_event is None or _stop_event.is_set():
        return
    if _sync_task is not None and not _sync_task.done():
        return
    _sync_task = asyncio.create_task(_run_sync_loop(), name="moegirl-knowledge-sync")


async def _run_sync_loop() -> None:
    while _stop_event is not None and not _stop_event.is_set() and _synchronizer is not None:
        try:
            catalog_seed_result = await _synchronizer.sync_catalog_once(
                MOEGIRL_KNOWLEDGE_SEED_QUERIES,
                limit=MOEGIRL_KNOWLEDGE_SYNC_MAX_ENTRIES,
            )
            catalog_result = await _synchronizer.sync_recent_once(
                limit=MOEGIRL_KNOWLEDGE_SYNC_MAX_ENTRIES,
            )
            _logger.info(
                "[moegirl-knowledge] background sync seed_catalog_status=%s recent_catalog_status=%s entries=%s added=%s updated=%s unchanged=%s failed=%s",
                catalog_seed_result["status"], catalog_result["status"], catalog_result["entries"],
                int(catalog_seed_result["added"]) + int(catalog_result["added"]),
                int(catalog_seed_result["updated"]) + int(catalog_result["updated"]),
                int(catalog_seed_result["unchanged"]) + int(catalog_result["unchanged"]),
                int(catalog_seed_result["failed"]) + int(catalog_result["failed"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("[moegirl-knowledge] sync failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=MOEGIRL_KNOWLEDGE_SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def stop_moegirl_knowledge_sync() -> None:
    global _sync_task, _stop_event, _synchronizer, _logger, _remove_post_reply_hook
    if _stop_event is not None:
        _stop_event.set()
    if _sync_task is not None:
        _sync_task.cancel()
        try:
            await _sync_task
        except asyncio.CancelledError:
            pass
    _sync_task = None
    _stop_event = None
    _synchronizer = None
    _logger = None
    if _remove_post_reply_hook is not None:
        _remove_post_reply_hook()
    _remove_post_reply_hook = None
    await stop_bundled_chime_import()
