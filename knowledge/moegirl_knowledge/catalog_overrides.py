"""Local enable/disable overrides kept outside the five-field entry table."""

from __future__ import annotations

import json
from pathlib import Path

from utils.file_utils import atomic_write_json


EntryKey = tuple[str, str]


def get_catalog_override_path(database_path: str | Path) -> Path:
    return Path(database_path).with_name("catalog.override.json")


def load_disabled_entries(path: str | Path) -> frozenset[EntryKey]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    rows = payload.get("disabled", ()) if isinstance(payload, dict) else ()
    result: set[EntryKey] = set()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        title = str(row.get("title") or "").strip()
        if source.startswith("source:") and title:
            result.add((source, title))
    return frozenset(result)


def set_entry_disabled(
    path: str | Path,
    *,
    source_tag: str,
    title: str,
    disabled: bool,
) -> int:
    """Atomically update one source/title override and return the disabled count."""
    source_tag = str(source_tag or "").strip()
    title = str(title or "").strip()
    if not source_tag.startswith("source:") or not title:
        raise ValueError("source and title are required")
    output_path = Path(path)
    entries = set(load_disabled_entries(output_path))
    key = (source_tag, title)
    if disabled:
        entries.add(key)
    else:
        entries.discard(key)
    payload = {
        "disabled": [
            {"source": source, "title": entry_title}
            for source, entry_title in sorted(entries)
        ]
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, payload, ensure_ascii=False, indent=2)
    return len(entries)


def entry_key(entry) -> EntryKey:
    return entry.source_tag, entry.title
