"""Content-free diagnostics for the local meme knowledge collection."""

from __future__ import annotations

from pathlib import Path

from .catalog_overrides import entry_key, get_catalog_override_path, load_disabled_entries
from .source_registry import SOURCES
from .store import MoegirlKnowledgeStore


def get_public_knowledge_status(config_manager) -> dict:
    """Return source-scoped local diagnostics without exposing knowledge text."""
    root = Path(config_manager.knowledge_dir) / "moegirl-knowledge"
    database_path = root / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path) if database_path.is_file() else None
    disabled = load_disabled_entries(get_catalog_override_path(database_path))
    entries = store.list_active_entries() if store is not None else ()
    existing_keys = {entry_key(entry) for entry in entries}
    disabled_count = len(disabled & existing_keys)
    sources = {}
    for source_tag, source in SOURCES.items():
        count = store.count_by_source_tag(source_tag) if store is not None else 0
        source_disabled = sum(
            1 for key in disabled & existing_keys if key[0] == source_tag
        )
        sources[source_tag.removeprefix("source:")] = {
            "status": "available" if count else "empty",
            "entries": count,
            "active_entries": count - source_disabled,
            "disabled_entries": source_disabled,
            "last_success_at": "",
            "name": source.name,
            "license": source.license,
            "homepage": source.homepage,
            "acquisition": "local_package" if count else "not_installed",
        }
    return {
        "mode": "local_only",
        "remote_acquisition": "isolated",
        "database": {
            "entries": len(entries),
            "active_entries": len(entries) - disabled_count,
            "disabled_entries": disabled_count,
            "integrity_ok": store.integrity_ok() if store is not None else False,
        },
        "sources": sources,
    }
