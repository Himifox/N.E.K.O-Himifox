"""Validated, local-only community knowledge data packs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.file_utils import atomic_write_json

from .engine.filters import sanitize_external_text
from .engine.models import KnowledgeEntry
from .engine.mutation_lock import mutation_lock
from .moegirl_knowledge.store import MoegirlKnowledgeStore


PACK_SCHEMA_VERSION = 1
MAX_PACK_BYTES = 10 * 1024 * 1024
MAX_PACK_ENTRIES = 10_000
_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TERM_ROLES = frozenset(("alias", "recognition"))


@dataclass(frozen=True, slots=True)
class KnowledgePackSource:
    name: str
    homepage: str
    license: str


@dataclass(frozen=True, slots=True)
class KnowledgePack:
    schema_version: int
    pack_id: str
    collection_id: str
    source: KnowledgePackSource
    entries: tuple[KnowledgeEntry, ...]

    @property
    def source_tag(self) -> str:
        return f"source:community.{self.pack_id}"


@dataclass(frozen=True, slots=True)
class PackInstallResult:
    pack_id: str
    collection_id: str
    source_tag: str
    entries: int


def get_pack_registry_path(database_path: str | Path) -> Path:
    return Path(database_path).with_name("packs.json")


def load_pack(path: str | Path) -> KnowledgePack:
    input_path = Path(path)
    if input_path.stat().st_size > MAX_PACK_BYTES:
        raise ValueError("knowledge pack exceeds the size limit")
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge pack is not valid UTF-8 JSON") from exc
    return validate_pack(payload)


def validate_pack(payload: object) -> KnowledgePack:
    if not isinstance(payload, dict):
        raise ValueError("knowledge pack root must be an object")
    _reject_unknown_keys(
        payload,
        {"schema_version", "pack_id", "collection_id", "source", "entries"},
        "knowledge pack",
    )
    if payload.get("schema_version") != PACK_SCHEMA_VERSION:
        raise ValueError("unsupported knowledge pack schema version")
    pack_id = _identifier(payload.get("pack_id"), "pack_id")
    collection_id = _identifier(payload.get("collection_id"), "collection_id")
    source_payload = payload.get("source")
    if not isinstance(source_payload, dict):
        raise ValueError("knowledge pack source must be an object")
    _reject_unknown_keys(source_payload, {"name", "homepage", "license"}, "source")
    source = KnowledgePackSource(
        name=_required_text(source_payload.get("name"), "source.name", 200),
        homepage=_optional_text(source_payload.get("homepage"), "source.homepage", 2_000),
        license=_required_text(source_payload.get("license"), "source.license", 500),
    )
    rows = payload.get("entries")
    if not isinstance(rows, list) or not rows:
        raise ValueError("knowledge pack entries must be a non-empty array")
    if len(rows) > MAX_PACK_ENTRIES:
        raise ValueError("knowledge pack contains too many entries")
    source_tag = f"source:community.{pack_id}"
    entries: list[KnowledgeEntry] = []
    seen_titles: set[str] = set()
    for index, row in enumerate(rows):
        entry = _entry_from_payload(row, source_tag=source_tag, index=index)
        normalized_title = entry.title.casefold()
        if normalized_title in seen_titles:
            raise ValueError("knowledge pack contains duplicate titles")
        seen_titles.add(normalized_title)
        entries.append(entry)
    return KnowledgePack(
        schema_version=PACK_SCHEMA_VERSION,
        pack_id=pack_id,
        collection_id=collection_id,
        source=source,
        entries=tuple(entries),
    )


def install_pack(
    database_path: str | Path,
    pack: KnowledgePack,
    *,
    subscription: dict[str, str] | None = None,
) -> PackInstallResult:
    """Replace one community source and its metadata with rollback on failure."""
    database_path = Path(database_path)
    registry_path = get_pack_registry_path(database_path)
    with mutation_lock(registry_path):
        store = MoegirlKnowledgeStore(database_path)
        old_entries = tuple(
            entry
            for entry in store.list_active_entries()
            if entry.source_tag == pack.source_tag
        )
        old_registry = _load_registry(registry_path)
        existing = old_registry.get("packs", {}).get(pack.pack_id)
        if isinstance(existing, dict):
            existing_collection = str(existing.get("collection_id") or "")
            if existing_collection and existing_collection != pack.collection_id:
                raise ValueError("knowledge pack cannot change its collection")
            _validate_subscription_identity(
                existing.get("subscription"),
                subscription,
            )
        new_registry = _registry_with_pack(
            old_registry,
            pack,
            subscription=subscription,
        )
        store.replace_source(pack.source_tag, pack.entries)
        try:
            atomic_write_json(registry_path, new_registry, ensure_ascii=False, indent=2)
        except Exception:
            store.replace_source(pack.source_tag, old_entries)
            raise
    return PackInstallResult(
        pack_id=pack.pack_id,
        collection_id=pack.collection_id,
        source_tag=pack.source_tag,
        entries=len(pack.entries),
    )


def list_installed_packs(database_path: str | Path) -> tuple[dict[str, Any], ...]:
    packs = _load_registry(get_pack_registry_path(database_path)).get("packs", {})
    if not isinstance(packs, dict):
        return ()
    return tuple(
        {"pack_id": pack_id, **value}
        for pack_id, value in sorted(packs.items())
        if isinstance(value, dict)
    )


def set_pack_auto_context(
    database_path: str | Path,
    pack_id: str,
    *,
    enabled: bool,
) -> None:
    pack_id = _identifier(pack_id, "pack_id")
    registry_path = get_pack_registry_path(database_path)
    with mutation_lock(registry_path):
        registry = _load_registry(registry_path)
        packs = registry.get("packs")
        if not isinstance(packs, dict) or not isinstance(packs.get(pack_id), dict):
            raise ValueError("knowledge pack is not installed")
        packs[pack_id] = {**packs[pack_id], "auto_context": bool(enabled)}
        atomic_write_json(registry_path, registry, ensure_ascii=False, indent=2)


def remove_pack(database_path: str | Path, pack_id: str) -> int:
    """Remove one community pack with registry rollback on failure."""
    pack_id = _identifier(pack_id, "pack_id")
    database_path = Path(database_path)
    registry_path = get_pack_registry_path(database_path)
    with mutation_lock(registry_path):
        registry = _load_registry(registry_path)
        packs = registry.get("packs")
        metadata = packs.get(pack_id) if isinstance(packs, dict) else None
        if not isinstance(metadata, dict):
            raise ValueError("knowledge pack is not installed")
        source_tag = str(metadata.get("source_tag") or "")
        if not source_tag.startswith("source:community."):
            raise ValueError("only community packs can be removed")

        store = MoegirlKnowledgeStore(database_path)
        old_entries = tuple(
            entry
            for entry in store.list_active_entries()
            if entry.source_tag == source_tag
        )
        new_packs = dict(packs)
        new_packs.pop(pack_id, None)
        store.replace_source(source_tag, ())
        try:
            atomic_write_json(
                registry_path,
                {"schema_version": 1, "packs": new_packs},
                ensure_ascii=False,
                indent=2,
            )
        except Exception:
            store.replace_source(source_tag, old_entries)
            raise
    return len(old_entries)


def enabled_pack_source_tags(database_path: str | Path) -> tuple[str, ...]:
    return tuple(
        str(pack.get("source_tag"))
        for pack in list_installed_packs(database_path)
        if pack.get("auto_context") is True and str(pack.get("source_tag") or "").startswith("source:")
    )


def _entry_from_payload(
    payload: object,
    *,
    source_tag: str,
    index: int,
) -> KnowledgeEntry:
    if not isinstance(payload, dict):
        raise ValueError(f"entries[{index}] must be an object")
    _reject_unknown_keys(
        payload,
        {"title", "terms", "tags", "summary", "content"},
        f"entries[{index}]",
    )
    terms = payload.get("terms", {})
    if not isinstance(terms, dict) or set(terms) - _TERM_ROLES:
        raise ValueError(f"entries[{index}].terms contains unsupported roles")
    normalized_terms: dict[str, tuple[str, ...]] = {}
    for role in _TERM_ROLES:
        values = terms.get(role, ())
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"entries[{index}].terms.{role} must be a string array")
        normalized_terms[role] = tuple(values)
    tags = payload.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError(f"entries[{index}].tags must be a string array")
    if any(tag.startswith("source:") for tag in tags):
        raise ValueError("community entries cannot declare source tags")
    return KnowledgeEntry(
        title=_required_text(payload.get("title"), f"entries[{index}].title", 500),
        terms=normalized_terms,
        tags=(source_tag, *tags),
        summary=_optional_text(payload.get("summary"), f"entries[{index}].summary", 4_000),
        content=_required_text(payload.get("content"), f"entries[{index}].content", 80_000),
    )


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not _PACK_ID_RE.fullmatch(text):
        raise ValueError(f"{field} must use lowercase letters, numbers, dots, dashes or underscores")
    return text


def _required_text(value: object, field: str, max_chars: int) -> str:
    text = _optional_text(value, field, max_chars)
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: object, field: str, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if len(value) > max_chars:
        raise ValueError(f"{field} exceeds the length limit")
    return sanitize_external_text(value, max_chars=max_chars)


def _reject_unknown_keys(payload: dict, allowed: set[str], field: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{field} contains unsupported fields")


def _load_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "packs": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("packs"), dict):
        return {"schema_version": 1, "packs": {}}
    return payload


def _registry_with_pack(
    registry: dict[str, Any],
    pack: KnowledgePack,
    *,
    subscription: dict[str, str] | None = None,
) -> dict[str, Any]:
    packs = dict(registry.get("packs", {}))
    previous = packs.get(pack.pack_id, {})
    auto_context = previous.get("auto_context") is True if isinstance(previous, dict) else False
    previous_subscription = (
        previous.get("subscription") if isinstance(previous, dict) else None
    )
    packs[pack.pack_id] = {
        "collection_id": pack.collection_id,
        "source_tag": pack.source_tag,
        "source": {
            "name": pack.source.name,
            "homepage": pack.source.homepage,
            "license": pack.source.license,
        },
        "entries": len(pack.entries),
        "auto_context": auto_context,
        "subscription": subscription if subscription is not None else previous_subscription,
    }
    return {"schema_version": 1, "packs": packs}


def _validate_subscription_identity(
    previous: object,
    replacement: dict[str, str] | None,
) -> None:
    previous_is_subscription = isinstance(previous, dict)
    replacement_is_subscription = isinstance(replacement, dict)
    if previous_is_subscription != replacement_is_subscription:
        raise ValueError("knowledge pack subscription identity cannot change")
    if not previous_is_subscription:
        return
    for field in ("provider", "remote_id"):
        if str(previous.get(field) or "") != str(replacement.get(field) or ""):
            raise ValueError("knowledge pack subscription identity cannot change")
