"""Small local overrides for collection-level conversational participation."""

from __future__ import annotations

import json
from pathlib import Path

from utils.file_utils import atomic_write_json


def get_collection_override_path(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / "collection.overrides.json"


def load_auto_context_overrides(path: str | Path) -> dict[str, bool]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    values = payload.get("auto_context", {}) if isinstance(payload, dict) else {}
    if not isinstance(values, dict):
        return {}
    return {
        str(collection_id): enabled
        for collection_id, enabled in values.items()
        if isinstance(collection_id, str) and isinstance(enabled, bool)
    }


def set_collection_auto_context(
    path: str | Path,
    *,
    collection_id: str,
    enabled: bool,
) -> None:
    collection_id = str(collection_id or "").strip()
    if not collection_id:
        raise ValueError("collection_id is required")
    output_path = Path(path)
    values = load_auto_context_overrides(output_path)
    values[collection_id] = bool(enabled)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        output_path,
        {"auto_context": dict(sorted(values.items()))},
        ensure_ascii=False,
        indent=2,
    )
