"""Persistent declarations for data-only community knowledge collections."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from utils.file_utils import atomic_write_json

from .collection_specs import (
    COMMUNITY_MATCH_POLICY,
    GENERIC_REFERENCE_RESPONSE_POLICY,
    CollectionSpec,
)


COMMUNITY_REGISTRY_VERSION = 1
_COLLECTION_ID_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{value}" for value in range(1, 10)}
    | {f"lpt{value}" for value in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class CommunityCollectionRecord:
    collection_id: str
    display_name: str
    storage_directory: str
    created_by_pack: str
    status: str = "active"


def validate_collection_id(value: object) -> str:
    """Validate a portable identifier before deriving a directory from it."""
    text = str(value or "").strip()
    if not _COLLECTION_ID_RE.fullmatch(text):
        raise ValueError(
            "collection_id must be 1-64 lowercase letters, numbers, dots, "
            "dashes or underscores and must start and end with a letter or number"
        )
    stem = text.split(".", 1)[0]
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError("collection_id is reserved by the operating system")
    return text


def community_storage_directory(collection_id: str) -> str:
    """Return the only directory shape accepted for a community collection."""
    return f"community/{validate_collection_id(collection_id)}"


def get_community_registry_path(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / "collections.json"


def get_community_mutation_lock_path(knowledge_root: str | Path) -> Path:
    return Path(knowledge_root) / ".mutation.lock"


def load_community_collections(
    knowledge_root: str | Path,
) -> dict[str, CommunityCollectionRecord]:
    """Load valid records and safely ignore damaged or forged paths."""
    path = get_community_registry_path(knowledge_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("collections")
    if payload.get("schema_version") != COMMUNITY_REGISTRY_VERSION or not isinstance(rows, dict):
        return {}
    records: dict[str, CommunityCollectionRecord] = {}
    for collection_id, raw in rows.items():
        if not isinstance(raw, dict):
            continue
        try:
            normalized_id = validate_collection_id(collection_id)
            expected_directory = community_storage_directory(normalized_id)
            display_name = _display_name(raw.get("display_name"))
            created_by_pack = str(raw.get("created_by_pack") or "").strip()
            status = str(raw.get("status") or "active").strip()
            if raw.get("storage_directory") != expected_directory:
                continue
            if not created_by_pack or status not in {"active", "conflict"}:
                continue
        except ValueError:
            continue
        records[normalized_id] = CommunityCollectionRecord(
            collection_id=normalized_id,
            display_name=display_name,
            storage_directory=expected_directory,
            created_by_pack=created_by_pack,
            status=status,
        )
    return records


def write_community_collections(
    knowledge_root: str | Path,
    records: Mapping[str, CommunityCollectionRecord],
) -> None:
    """Atomically replace the lightweight community collection registry."""
    payload = {
        "schema_version": COMMUNITY_REGISTRY_VERSION,
        "collections": {
            collection_id: {
                key: value
                for key, value in asdict(records[collection_id]).items()
                if key != "collection_id"
            }
            for collection_id in sorted(records)
        },
    }
    path = get_community_registry_path(knowledge_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload, ensure_ascii=False, indent=2)


def new_community_collection(
    collection_id: str,
    display_name: object,
    *,
    created_by_pack: str,
) -> CommunityCollectionRecord:
    normalized_id = validate_collection_id(collection_id)
    return CommunityCollectionRecord(
        collection_id=normalized_id,
        display_name=_display_name(display_name),
        storage_directory=community_storage_directory(normalized_id),
        created_by_pack=created_by_pack,
    )


def community_collection_spec(record: CommunityCollectionRecord) -> CollectionSpec:
    """Build the fixed, non-overridable policy for one community database."""
    return CollectionSpec(
        collection_id=record.collection_id,
        storage_directory=record.storage_directory,
        display_name=record.display_name,
        priority=0,
        auto_context_enabled=False,
        restrict_auto_context_to_registered_sources=True,
        match_policy=COMMUNITY_MATCH_POLICY,
        response_policy=GENERIC_REFERENCE_RESPONSE_POLICY,
        community_managed=True,
    )


def _display_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("collection.display_name is required")
    text = value.strip()
    if len(text) > 200:
        raise ValueError("collection.display_name exceeds the length limit")
    return text
