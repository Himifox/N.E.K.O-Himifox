"""Validated, local-only community knowledge data packs."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.file_utils import atomic_write_json

from ._mutation_lock import mutation_lock
from .chunking import derive_knowledge_chunks
from .filters import sanitize_external_text
from .models import (
    KnowledgeEntry,
    normalize_knowledge_title,
)
from .store import KnowledgeStore


PACK_SCHEMA_VERSION = 1
PACK_REGISTRY_SCHEMA_VERSION = 4
MATERIAL_TYPES = frozenset(("knowledge", "corpus"))
MAX_PACK_BYTES = 10 * 1024 * 1024
MAX_PACK_ENTRIES = 5_000
MAX_PACK_PROJECTED_CHUNKS = 5_000
MIN_INSTALL_FREE_BYTES = 512 * 1024 * 1024
_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TERM_ROLES = frozenset(("alias", "recognition"))


class KnowledgePackRegistryError(ValueError):
    """Raised when an existing pack registry cannot be trusted."""


@dataclass(frozen=True, slots=True)
class KnowledgePackSource:
    name: str
    homepage: str
    license: str


@dataclass(frozen=True, slots=True)
class KnowledgePack:
    schema_version: int
    pack_id: str
    material_type: str
    source: KnowledgePackSource
    entries: tuple[KnowledgeEntry, ...]

    @property
    def source_tag(self) -> str:
        return f"source:community.{self.pack_id}"


@dataclass(frozen=True, slots=True)
class PackInstallResult:
    pack_id: str
    source_tag: str
    material_type: str
    entries: int
    retrieval_mode: str = "bm25"


@dataclass(frozen=True, slots=True)
class PackPreflight:
    entries: int
    projected_chunks: int
    content_bytes: int
    estimated_working_bytes: int


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    entries: tuple[KnowledgeEntry, ...]
    ready_embeddings: tuple[dict[str, object], ...]
    embedding_policy: str


def pack_payload(pack: KnowledgePack) -> dict[str, object]:
    """Return the canonical package payload; entries remain strictly five-field."""
    payload: dict[str, object] = {
        "schema_version": pack.schema_version,
        "pack_id": pack.pack_id,
        "material_type": pack.material_type,
        "source": {
            "name": pack.source.name,
            "homepage": pack.source.homepage,
            "license": pack.source.license,
        },
        "entries": [
            {
                "title": entry.title,
                "terms": {role: list(values) for role, values in entry.terms.items()},
                "tags": [tag for tag in entry.tags if not tag.startswith("source:")],
                "summary": entry.summary,
                "content": entry.content,
            }
            for entry in pack.entries
        ],
    }
    return payload


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
    schema_version = payload.get("schema_version")
    if schema_version != PACK_SCHEMA_VERSION:
        raise ValueError("unsupported knowledge pack schema version")
    allowed_keys = {"schema_version", "pack_id", "material_type", "source", "entries"}
    _reject_unknown_keys(payload, allowed_keys, "knowledge pack")
    material_type = _material_type(payload.get("material_type"))
    pack_id = _identifier(payload.get("pack_id"), "pack_id")
    source_payload = payload.get("source")
    if not isinstance(source_payload, dict):
        raise ValueError("knowledge pack source must be an object")
    _reject_unknown_keys(source_payload, {"name", "homepage", "license"}, "source")
    source = KnowledgePackSource(
        name=_required_text(source_payload.get("name"), "source.name", 200),
        homepage=_optional_text(
            source_payload.get("homepage"), "source.homepage", 2_000
        ),
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
        normalized_title = normalize_knowledge_title(entry.title)
        if normalized_title in seen_titles:
            raise ValueError("knowledge pack contains duplicate titles")
        seen_titles.add(normalized_title)
        entries.append(entry)
    pack = KnowledgePack(
        schema_version=int(schema_version),
        pack_id=pack_id,
        material_type=material_type,
        source=source,
        entries=tuple(entries),
    )
    preflight_pack(pack)
    return pack


def preflight_pack(pack: KnowledgePack) -> PackPreflight:
    """Bound user-controlled work before it reaches SQLite or ONNX."""
    projected_chunks = 0
    content_bytes = 0
    for entry in pack.entries:
        projected_chunks += len(
            derive_knowledge_chunks(
                entry,
                entry_key=f"{entry.source_tag}:{entry.title}",
            )
        )
        if projected_chunks > MAX_PACK_PROJECTED_CHUNKS:
            raise ValueError("knowledge pack would create too many chunks")
        content_bytes += len(entry.content.encode("utf-8"))
    # Allow room for the staging database, WAL/rollback work and float16 vectors.
    estimated = max(content_bytes * 2 + projected_chunks * 1024, 1)
    return PackPreflight(
        entries=len(pack.entries),
        projected_chunks=projected_chunks,
        content_bytes=content_bytes,
        estimated_working_bytes=estimated,
    )


def ensure_install_capacity(root: str | Path, preflight: PackPreflight) -> None:
    root_path = Path(root)
    probe = root_path if root_path.exists() else root_path.parent
    free = int(shutil.disk_usage(probe).free)
    required = max(MIN_INSTALL_FREE_BYTES, preflight.estimated_working_bytes * 2)
    if free < required:
        raise ValueError("not enough free disk space for knowledge pack staging")


def _snapshot_source(
    store: KnowledgeStore,
    source_tag: str,
    metadata: object,
) -> _SourceSnapshot:
    policy_counts = store.embedding_policy_counts(source_tag=source_tag)
    active_policies = tuple(
        policy for policy, count in policy_counts.items() if int(count) > 0
    )
    embedding_policy = (
        active_policies[0]
        if len(active_policies) == 1
        else (
            "local"
            if isinstance(metadata, dict)
            and metadata.get("local_embedding_enabled") is True
            else "prebuilt_only"
        )
    )
    return _SourceSnapshot(
        entries=tuple(
            entry
            for entry in store.list_active_entries()
            if entry.source_tag == source_tag
        ),
        ready_embeddings=store.ready_embedding_records(source_tag=source_tag),
        embedding_policy=embedding_policy,
    )


def _restore_source(
    store: KnowledgeStore,
    source_tag: str,
    snapshot: _SourceSnapshot,
) -> None:
    store.replace_source(
        source_tag,
        snapshot.entries,
        embedding_policy=snapshot.embedding_policy,
    )
    if snapshot.ready_embeddings:
        store.store_chunk_embeddings_strict(snapshot.ready_embeddings)


def install_pack(
    database_path: str | Path,
    pack: KnowledgePack,
    *,
    subscription: dict[str, str] | None = None,
    prepared_embeddings: tuple[dict[str, object], ...] = (),
    retrieval_mode: str = "bm25",
    embedding_policy: str = "prebuilt_only",
    index_metadata: dict[str, object] | None = None,
) -> PackInstallResult:
    """Replace one community source and its metadata with rollback on failure."""
    if retrieval_mode not in {"hybrid", "mixed", "bm25"}:
        raise ValueError("unsupported knowledge pack retrieval mode")
    if embedding_policy not in {"local", "prebuilt_only"}:
        raise ValueError("unsupported knowledge pack embedding policy")
    database_path = Path(database_path)
    registry_path = get_pack_registry_path(database_path)
    with mutation_lock(registry_path):
        old_registry = _load_registry(registry_path)
        existing = old_registry.get("packs", {}).get(pack.pack_id)
        if isinstance(existing, dict):
            _validate_subscription_identity(
                existing.get("subscription"),
                subscription,
            )
        store = KnowledgeStore(database_path)
        snapshot = _snapshot_source(store, pack.source_tag, existing)
        local_embedding_enabled = embedding_policy == "local" or (
            isinstance(existing, dict)
            and existing.get("local_embedding_enabled") is True
        )
        effective_policy = "local" if local_embedding_enabled else "prebuilt_only"
        new_registry = _registry_with_pack(
            old_registry,
            pack,
            subscription=subscription,
            retrieval_mode=retrieval_mode,
            index_metadata=index_metadata,
            local_embedding_enabled=local_embedding_enabled,
        )
        try:
            store.replace_source(
                pack.source_tag,
                pack.entries,
                embedding_policy=effective_policy,
            )
            if prepared_embeddings:
                stored = store.store_chunk_embeddings_strict(prepared_embeddings)
                if stored != len(prepared_embeddings):
                    raise ValueError(
                        "prebuilt knowledge index was not imported completely"
                    )
            atomic_write_json(registry_path, new_registry, ensure_ascii=False, indent=2)
        except Exception:
            _restore_source(store, pack.source_tag, snapshot)
            raise
    return PackInstallResult(
        pack_id=pack.pack_id,
        source_tag=pack.source_tag,
        material_type=pack.material_type,
        entries=len(pack.entries),
        retrieval_mode=retrieval_mode,
    )


def list_installed_packs(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = 5_000,
) -> tuple[dict[str, Any], ...]:
    try:
        packs = _load_registry(get_pack_registry_path(database_path)).get("packs", {})
    except KnowledgePackRegistryError:
        return ()
    if not isinstance(packs, dict):
        return ()
    store = KnowledgeStore(database_path, busy_timeout_ms=busy_timeout_ms)
    items: list[dict[str, Any]] = []
    for pack_id, value in sorted(packs.items()):
        if not isinstance(value, dict):
            continue
        source_tag = str(value.get("source_tag") or "")
        status = (
            store.source_chunk_status(source_tag)
            if source_tag
            else {
                "chunks_total": 0,
                "chunks_ready": 0,
            }
        )
        items.append(
            {
                "pack_id": pack_id,
                **value,
                "prebuilt_chunks_ready": int(status["chunks_ready"]),
                "prebuilt_chunks_missing": max(
                    int(status["chunks_total"]) - int(status["chunks_ready"]),
                    0,
                ),
            }
        )
    return tuple(items)


def installed_source_embedding_policies(
    database_path: str | Path,
) -> dict[str, str]:
    """Return explicit generation ownership for installed community sources."""
    try:
        packs = _load_registry(get_pack_registry_path(database_path))["packs"]
    except KnowledgePackRegistryError:
        return {}
    policies: dict[str, str] = {}
    for metadata in packs.values():
        source_tag = str(metadata.get("source_tag") or "")
        if not source_tag.startswith("source:community."):
            continue
        policies[source_tag] = (
            "local"
            if metadata.get("local_embedding_enabled") is True
            else "prebuilt_only"
        )
    return policies


def migrate_legacy_pack_index_policies(database_path: str | Path) -> int:
    """Adopt installed community packs without scheduling new local inference."""
    database_path = Path(database_path)
    registry_path = get_pack_registry_path(database_path)
    with mutation_lock(registry_path):
        try:
            registry = _load_registry(registry_path)
        except KnowledgePackRegistryError:
            return 0
        packs = registry.get("packs")
        if not isinstance(packs, dict):
            return 0
        store = KnowledgeStore(database_path)
        changed = 0
        snapshots: list[tuple[str, _SourceSnapshot]] = []
        try:
            for pack_id, metadata in tuple(packs.items()):
                if (
                    not isinstance(metadata, dict)
                    or "local_embedding_enabled" in metadata
                ):
                    continue
                source_tag = str(metadata.get("source_tag") or "")
                if not source_tag.startswith("source:community."):
                    continue
                status = store.source_chunk_status(source_tag)
                snapshots.append(
                    (source_tag, _snapshot_source(store, source_tag, metadata))
                )
                store.set_source_embedding_policy(source_tag, "prebuilt_only")
                has_ready = int(status["chunks_ready"]) > 0
                packs[pack_id] = {
                    **metadata,
                    "index_origin": "legacy" if has_ready else "none",
                    "index_trust": "local_generated" if has_ready else "none",
                    "index_validation": "accepted" if has_ready else "absent",
                    "index_fallback_reason": "",
                    "local_embedding_enabled": False,
                    "prebuilt_chunks_ready": int(status["chunks_ready"]),
                    "prebuilt_chunks_missing": max(
                        int(status["chunks_total"]) - int(status["chunks_ready"]),
                        0,
                    ),
                }
                changed += 1
            if changed:
                atomic_write_json(
                    registry_path, registry, ensure_ascii=False, indent=2
                )
        except Exception:
            for source_tag, snapshot in reversed(snapshots):
                _restore_source(store, source_tag, snapshot)
            raise
    return changed


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
        metadata = packs[pack_id]
        packs[pack_id] = {**metadata, "auto_context": bool(enabled)}
        atomic_write_json(registry_path, registry, ensure_ascii=False, indent=2)


def set_pack_material_type_override(
    database_path: str | Path,
    pack_id: str,
    *,
    material_type: str | None,
) -> None:
    """Persist a local usage override without changing entries or vectors."""
    pack_id = _identifier(pack_id, "pack_id")
    normalized = None if material_type is None else _material_type(material_type)
    registry_path = get_pack_registry_path(database_path)
    with mutation_lock(registry_path):
        registry = _load_registry(registry_path)
        packs = registry.get("packs")
        metadata = packs.get(pack_id) if isinstance(packs, dict) else None
        if not isinstance(metadata, dict):
            raise ValueError("knowledge pack is not installed")
        declared = _material_type(metadata.get("declared_material_type", "knowledge"))
        previous_effective = _effective_material_type(metadata)
        effective = normalized or declared
        packs[pack_id] = {
            **metadata,
            "material_type_override": normalized,
            "effective_material_type": effective,
            "auto_context": True
            if effective == "corpus" and previous_effective != "corpus"
            else bool(metadata.get("auto_context")),
        }
        registry["schema_version"] = PACK_REGISTRY_SCHEMA_VERSION
        atomic_write_json(registry_path, registry, ensure_ascii=False, indent=2)


def set_pack_index_policy(
    database_path: str | Path,
    pack_id: str,
    *,
    local_embedding_enabled: bool,
) -> None:
    """Persist explicit consent for local maintenance of one community index."""
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
            raise ValueError("only community packs have an index policy")
        policy = "local" if local_embedding_enabled else "prebuilt_only"
        store = KnowledgeStore(database_path)
        snapshot = _snapshot_source(store, source_tag, metadata)
        packs[pack_id] = {
            **metadata,
            "local_embedding_enabled": bool(local_embedding_enabled),
        }
        try:
            store.set_source_embedding_policy(source_tag, policy)
            atomic_write_json(registry_path, registry, ensure_ascii=False, indent=2)
        except Exception:
            _restore_source(store, source_tag, snapshot)
            raise


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

        store = KnowledgeStore(database_path)
        snapshot = _snapshot_source(store, source_tag, metadata)
        new_packs = dict(packs)
        new_packs.pop(pack_id, None)
        store.replace_source(source_tag, ())
        try:
            atomic_write_json(
                registry_path,
                {"schema_version": PACK_REGISTRY_SCHEMA_VERSION, "packs": new_packs},
                ensure_ascii=False,
                indent=2,
            )
        except Exception:
            _restore_source(store, source_tag, snapshot)
            raise
    return len(snapshot.entries)


def enabled_pack_source_tags(database_path: str | Path) -> tuple[str, ...]:
    return tuple(
        str(pack.get("source_tag"))
        for pack in list_installed_packs(database_path)
        if pack.get("auto_context") is True
        and _effective_material_type(pack) == "knowledge"
        and str(pack.get("source_tag") or "").startswith("source:")
    )


def material_source_tags(
    database_path: str | Path,
    material_types: tuple[str, ...],
) -> tuple[str, ...]:
    """Return community source tags whose effective type is allowed."""
    allowed = frozenset(_material_type(value) for value in material_types)
    return tuple(
        str(pack.get("source_tag"))
        for pack in list_installed_packs(database_path)
        if _effective_material_type(pack) in allowed
        and str(pack.get("source_tag") or "").startswith("source:community.")
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
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
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
        summary=_optional_text(
            payload.get("summary"), f"entries[{index}].summary", 4_000
        ),
        content=_required_text(
            payload.get("content"), f"entries[{index}].content", 80_000
        ),
    )


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not _PACK_ID_RE.fullmatch(text):
        raise ValueError(
            f"{field} must use lowercase letters, numbers, dots, dashes or underscores"
        )
    return text


def _material_type(value: object) -> str:
    text = str(value or "").strip().lower()
    if text not in MATERIAL_TYPES:
        raise ValueError("material_type must be knowledge or corpus")
    return text


def _effective_material_type(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return "knowledge"
    override = metadata.get("material_type_override")
    if override in MATERIAL_TYPES:
        return str(override)
    declared = metadata.get("declared_material_type")
    return str(declared) if declared in MATERIAL_TYPES else "knowledge"


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
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": PACK_REGISTRY_SCHEMA_VERSION, "packs": {}}
    except (OSError, UnicodeError) as exc:
        raise KnowledgePackRegistryError(
            "knowledge pack registry is unreadable"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KnowledgePackRegistryError(
            "knowledge pack registry is corrupt"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("packs"), dict):
        raise KnowledgePackRegistryError(
            "knowledge pack registry has an invalid structure"
        )
    previous_schema_version = payload.get("schema_version")
    if (
        isinstance(previous_schema_version, int)
        and previous_schema_version > PACK_REGISTRY_SCHEMA_VERSION
    ):
        raise KnowledgePackRegistryError(
            "knowledge pack registry uses a newer schema version"
        )
    migrate_corpus_auto_context = (
        not isinstance(previous_schema_version, int)
        or previous_schema_version < PACK_REGISTRY_SCHEMA_VERSION
    )
    for pack_id, metadata in tuple(payload["packs"].items()):
        if not isinstance(metadata, dict):
            raise KnowledgePackRegistryError(
                f"knowledge pack registry entry {pack_id!r} is invalid"
            )
        declared = (
            str(metadata.get("declared_material_type"))
            if metadata.get("declared_material_type") in MATERIAL_TYPES
            else "knowledge"
        )
        override = metadata.get("material_type_override")
        if override not in MATERIAL_TYPES:
            override = None
        effective = str(override or declared)
        auto_context = bool(metadata.get("auto_context"))
        if (
            effective == "corpus"
            and migrate_corpus_auto_context
        ):
            auto_context = True
        normalized = {
            **metadata,
            "declared_material_type": declared,
            "material_type_override": override,
            "effective_material_type": effective,
            "auto_context": auto_context,
        }
        if normalized != metadata:
            payload["packs"][pack_id] = normalized
    if payload.get("schema_version") != PACK_REGISTRY_SCHEMA_VERSION:
        payload["schema_version"] = PACK_REGISTRY_SCHEMA_VERSION
    return payload


def _registry_with_pack(
    registry: dict[str, Any],
    pack: KnowledgePack,
    *,
    subscription: dict[str, str] | None = None,
    retrieval_mode: str = "bm25",
    index_metadata: dict[str, object] | None = None,
    local_embedding_enabled: bool = False,
) -> dict[str, Any]:
    packs = dict(registry.get("packs", {}))
    previous = packs.get(pack.pack_id, {})
    auto_context = (
        previous.get("auto_context") is True
        if isinstance(previous, dict) and previous
        else pack.material_type == "corpus"
    )
    previous_subscription = (
        previous.get("subscription") if isinstance(previous, dict) else None
    )
    override = (
        str(previous.get("material_type_override"))
        if isinstance(previous, dict)
        and previous.get("material_type_override") in MATERIAL_TYPES
        else None
    )
    effective_material_type = override or pack.material_type
    packs[pack.pack_id] = {
        "source_tag": pack.source_tag,
        "source": {
            "name": pack.source.name,
            "homepage": pack.source.homepage,
            "license": pack.source.license,
        },
        "entries": len(pack.entries),
        "declared_material_type": pack.material_type,
        "material_type_override": override,
        "effective_material_type": effective_material_type,
        "auto_context": auto_context,
        "subscription": subscription
        if subscription is not None
        else previous_subscription,
        "retrieval_mode": retrieval_mode,
        "index_origin": str((index_metadata or {}).get("index_origin") or "none"),
        "index_trust": str((index_metadata or {}).get("index_trust") or "none"),
        "index_validation": str(
            (index_metadata or {}).get("index_validation") or "absent"
        ),
        "index_fallback_reason": str(
            (index_metadata or {}).get("index_fallback_reason") or ""
        )[:80],
        "local_embedding_enabled": bool(local_embedding_enabled),
        "prebuilt_chunks_ready": int(
            (index_metadata or {}).get("prebuilt_chunks_ready") or 0
        ),
        "prebuilt_chunks_missing": int(
            (index_metadata or {}).get("prebuilt_chunks_missing") or 0
        ),
    }
    return {"schema_version": PACK_REGISTRY_SCHEMA_VERSION, "packs": packs}


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
