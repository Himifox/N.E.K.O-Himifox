"""One-time migration from former knowledge database layouts."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from utils.file_utils import atomic_write_json

from ._mutation_lock import mutation_lock
from .chunking import derive_knowledge_chunks
from .catalog_overrides import load_disabled_entries
from .models import (
    KnowledgeEntry,
    normalize_knowledge_title,
)
from .packs import PACK_REGISTRY_SCHEMA_VERSION
from .store import KnowledgeStore


_LEGACY_SPLIT_DIRECTORIES = ("moegirl-knowledge", "corpora")
_LEGACY_UNIFIED_DIRECTORY = "public-knowledge"
logger = logging.getLogger("N.E.K.O.Knowledge.Migration")


def migrate_legacy_knowledge_layout(
    knowledge_root: str | Path,
    destination_database: str | Path,
) -> bool:
    """Move an old unified store or merge split stores exactly once.

    The legacy databases remain untouched and therefore serve as recovery
    copies.  A complete replacement database and its sidecars are assembled in
    a sibling staging directory before the new database becomes visible.
    """
    root = Path(knowledge_root)
    destination = Path(destination_database)
    if destination.is_file():
        return False
    old_unified = root / _LEGACY_UNIFIED_DIRECTORY / "knowledge.db"
    legacy_databases = (
        (old_unified,)
        if old_unified.is_file()
        else tuple(
            path
            for directory in _LEGACY_SPLIT_DIRECTORIES
            if (path := root / directory / "knowledge.db").is_file()
        )
    )
    if not legacy_databases:
        return False

    with mutation_lock(destination):
        if destination.is_file():
            return False
        stage = Path(tempfile.mkdtemp(prefix=".knowledge-migration-", dir=root))
        try:
            staged_database = stage / "knowledge.db"
            staged_store = KnowledgeStore(staged_database)
            entries_by_source, policies, vectors = _collect_legacy_data(
                legacy_databases
            )
            for source_tag, entries in sorted(entries_by_source.items()):
                staged_store.replace_source(
                    source_tag,
                    tuple(entries.values()),
                    embedding_policy=policies[source_tag],
                )
            if vectors:
                try:
                    staged_store.store_chunk_embeddings_strict(tuple(vectors.values()))
                except ValueError as exc:
                    # Embeddings are rebuildable derived data.  A malformed
                    # legacy row must not make the authoritative entries and
                    # registry unavailable after an upgrade.
                    logger.warning(
                        "Discarding %d incompatible legacy knowledge vectors (%s)",
                        len(vectors),
                        type(exc).__name__,
                    )

            registry = _merge_registries(legacy_databases, staged_store)
            atomic_write_json(
                stage / "packs.json",
                registry,
                ensure_ascii=False,
                indent=2,
            )
            disabled = _merge_disabled_entries(legacy_databases)
            atomic_write_json(
                stage / "catalog.override.json",
                {
                    "disabled": [
                        {"source": source, "title": title}
                        for source, title in sorted(disabled)
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
            if not staged_store.integrity_ok():
                raise ValueError(
                    "unified knowledge migration failed integrity check"
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / "packs.json", destination.with_name("packs.json"))
            os.replace(
                stage / "catalog.override.json",
                destination.with_name("catalog.override.json"),
            )
            # Publish the database last. Its presence is the completion marker;
            # if the process stops while replacing sidecars, the next startup
            # safely rebuilds the still-unpublished migration.
            os.replace(staged_database, destination)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        else:
            shutil.rmtree(stage, ignore_errors=True)
    return True


def _collect_legacy_data(
    databases: tuple[Path, ...],
) -> tuple[
    dict[str, dict[str, KnowledgeEntry]],
    dict[str, str],
    dict[str, dict[str, object]],
]:
    entries_by_source: dict[str, dict[str, KnowledgeEntry]] = defaultdict(dict)
    entry_origins: dict[tuple[str, str], Path] = {}
    policy_votes: dict[str, set[str]] = defaultdict(set)
    vectors: dict[str, dict[str, object]] = {}
    conflicting_vector_ids: set[str] = set()
    for database in databases:
        store = KnowledgeStore(database)
        for entry in store.list_active_entries():
            normalized_title = normalize_knowledge_title(entry.title)
            identity = (entry.source_tag, normalized_title)
            previous = entries_by_source[entry.source_tag].get(normalized_title)
            previous_origin = entry_origins.get(identity)
            if (
                previous is not None
                and previous.content_hash != entry.content_hash
                and previous_origin != database
            ):
                raise ValueError(
                    "legacy knowledge contains conflicting source/title entries"
                )
            if previous is not None and previous.content_hash != entry.content_hash:
                logger.warning(
                    "Keeping the later legacy entry for source=%s title=%s",
                    entry.source_tag,
                    entry.title,
                )
            entries_by_source[entry.source_tag][normalized_title] = entry
            entry_origins[identity] = database
        for row in store.count_by_source_tags():
            source_tag = str(row.get("tag") or "")
            if not source_tag:
                continue
            counts = store.embedding_policy_counts(source_tag=source_tag)
            policy_votes[source_tag].update(
                policy for policy, count in counts.items() if int(count) > 0
            )
        for record in store.ready_embedding_records():
            chunk_id = str(record.get("chunk_id") or "")
            if not chunk_id or chunk_id in conflicting_vector_ids:
                continue
            previous = vectors.get(chunk_id)
            if previous is not None and previous != record:
                vectors.pop(chunk_id, None)
                conflicting_vector_ids.add(chunk_id)
                continue
            vectors[chunk_id] = record

    selected_chunk_ids = {
        chunk.chunk_id
        for entries in entries_by_source.values()
        for entry in entries.values()
        for chunk in derive_knowledge_chunks(
            entry,
            entry_key=f"{entry.source_tag}:{entry.title}",
        )
    }
    vectors = {
        chunk_id: record
        for chunk_id, record in vectors.items()
        if chunk_id in selected_chunk_ids
    }

    policies = {
        source_tag: ("prebuilt_only" if votes == {"prebuilt_only"} else "local")
        for source_tag, votes in policy_votes.items()
    }
    for source_tag in entries_by_source:
        policies.setdefault(source_tag, "local")
    return dict(entries_by_source), policies, vectors


def _merge_registries(
    databases: tuple[Path, ...],
    store: KnowledgeStore,
) -> dict[str, object]:
    merged: dict[str, dict[str, object]] = {}
    for database in databases:
        try:
            payload = json.loads(database.with_name("packs.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        packs = payload.get("packs") if isinstance(payload, dict) else None
        if not isinstance(packs, dict):
            continue
        for pack_id, raw in packs.items():
            if not isinstance(raw, dict):
                continue
            metadata = dict(raw)
            metadata.pop("collection_id", None)
            declared = str(metadata.get("declared_material_type") or "knowledge")
            if declared not in {"knowledge", "corpus"}:
                declared = "knowledge"
            override = metadata.get("material_type_override")
            if override not in {"knowledge", "corpus"}:
                override = None
            effective = str(override or declared)
            metadata.update(
                {
                    "declared_material_type": declared,
                    "material_type_override": override,
                    "effective_material_type": effective,
                    "auto_context": True
                    if effective == "corpus"
                    else bool(metadata.get("auto_context")),
                }
            )
            previous = merged.get(str(pack_id))
            if previous is not None:
                if previous.get("source_tag") != metadata.get(
                    "source_tag"
                ) or previous.get("subscription") != metadata.get("subscription"):
                    raise ValueError(
                        "legacy knowledge contains conflicting packs"
                    )
                if (
                    previous.get("effective_material_type") == "corpus"
                    or effective == "corpus"
                ):
                    metadata["declared_material_type"] = "corpus"
                    metadata["material_type_override"] = None
                    metadata["effective_material_type"] = "corpus"
                    metadata["auto_context"] = True
            source_tag = str(metadata.get("source_tag") or "")
            status = store.source_chunk_status(source_tag) if source_tag else {}
            total = int(status.get("chunks_total", 0))
            ready = int(status.get("chunks_ready", 0))
            metadata.update(
                {
                    "entries": sum(
                        1
                        for entry in store.list_active_entries()
                        if entry.source_tag == source_tag
                    ),
                    "retrieval_mode": "hybrid" if total and ready == total else "bm25",
                    "prebuilt_chunks_ready": ready,
                    "prebuilt_chunks_missing": max(total - ready, 0),
                }
            )
            merged[str(pack_id)] = metadata
    return {"schema_version": PACK_REGISTRY_SCHEMA_VERSION, "packs": merged}


def _merge_disabled_entries(databases: tuple[Path, ...]) -> set[tuple[str, str]]:
    disabled: set[tuple[str, str]] = set()
    for database in databases:
        disabled.update(
            load_disabled_entries(database.with_name("catalog.override.json"))
        )
    return disabled
