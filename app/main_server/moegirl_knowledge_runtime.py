"""Main-Server-owned lifecycle for the local public knowledge database.

Remote encyclopedia acquisition is deliberately not imported here.  Geng8 and
Moegirl adapters remain offline maintenance components until their evidence
validation is safe enough to reconnect.
"""

from __future__ import annotations

from config.moegirl_knowledge_settings import CHIME_KNOWLEDGE_ENABLED
from knowledge.moegirl_knowledge.bundled_chime_runtime import (
    schedule_bundled_chime_import,
    stop_bundled_chime_import,
)


async def start_moegirl_knowledge_runtime(config_manager, logger) -> None:
    """Prepare the local database and bundled dataset without network access."""
    if not config_manager.ensure_knowledge_directory():
        logger.warning("[moegirl-knowledge] knowledge directory unavailable; runtime disabled")
        return
    if CHIME_KNOWLEDGE_ENABLED:
        schedule_bundled_chime_import(config_manager, logger)


async def stop_moegirl_knowledge_runtime() -> None:
    """Cancel local import work during Main Server shutdown."""
    await stop_bundled_chime_import()


# Compatibility aliases for entrypoints from builds created before the local-only
# boundary was introduced.  They perform no remote synchronization.
start_moegirl_knowledge_sync = start_moegirl_knowledge_runtime
stop_moegirl_knowledge_sync = stop_moegirl_knowledge_runtime
