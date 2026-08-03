"""Generic, local-only management API for public knowledge collections."""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import APIRouter, Query, Request

from knowledge.api import open_knowledge
from knowledge.diagnostics import list_recent_knowledge_routes
from knowledge.engine.source_registry import get_source
from knowledge.moegirl_knowledge.catalog_overrides import (
    entry_key,
    get_catalog_override_path,
    load_disabled_entries,
)
from knowledge.packs import MAX_PACK_BYTES, validate_pack
from knowledge.subscriptions import (
    SUBSCRIPTION_PROTOCOL_VERSION,
    canonical_pack_bytes,
    validate_subscription,
)
from main_routers.shared_state import get_config_manager


router = APIRouter(prefix="/api/public-knowledge", tags=["public-knowledge"])
_PACK_ENVELOPE_OVERHEAD_BYTES = 64 * 1024


def _service():
    return open_knowledge(get_config_manager().knowledge_dir)


def _source_tag(value: str) -> str:
    value = str(value or "").strip()
    return value if not value or value.startswith("source:") else f"source:{value}"


def _entry_payload(
    service,
    collection_id: str,
    entry,
    *,
    detail: bool,
    score=None,
    disabled_entries=None,
    source_cache=None,
):
    database_path = service.database_path(collection_id)
    if source_cache is None:
        source_cache = {}
    source = source_cache.get(entry.source_tag)
    if source is None:
        source = get_source(entry.source_tag, database_path=database_path)
        source_cache[entry.source_tag] = source
    if disabled_entries is None:
        disabled_entries = load_disabled_entries(
            get_catalog_override_path(database_path)
        )
    disabled = entry_key(entry) in disabled_entries
    payload = {
        "collection_id": collection_id,
        "title": entry.title,
        "terms": {role: list(values) for role, values in entry.terms.items()},
        "tags": list(entry.tags),
        "summary": entry.summary,
        "source": {
            "tag": source.tag,
            "name": source.name,
            "homepage": source.homepage,
            "license": source.license,
        },
        "disabled": disabled,
    }
    if detail:
        payload["content"] = entry.content
    if score is not None:
        payload["score"] = score
    return payload


def _validate_mutation(request: Request, payload: dict):
    from .system_router import _validate_local_mutation_request

    return _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )


@router.get("/collections")
async def list_public_knowledge_collections():
    service = _service()
    collections = await asyncio.to_thread(service.list_collections)
    return {"ok": True, "collections": list(collections)}


@router.get("/entries")
async def list_public_knowledge_entries(
    collection: str = Query(..., min_length=1, max_length=64),
    query: str = Query(default="", max_length=200),
    source: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
):
    service = _service()
    source_tag = _source_tag(source)
    try:
        database_path = service.database_path(collection)
    except ValueError:
        return {"ok": False, "reason": "unknown_collection"}
    disabled_entries = await asyncio.to_thread(
        load_disabled_entries,
        get_catalog_override_path(database_path),
    )
    source_cache = {}
    if query.strip():
        page = await asyncio.to_thread(
            service.search_page,
            collection,
            query.strip(),
            source_tag=source_tag,
            limit=limit,
            offset=offset,
            include_disabled=True,
        )
        has_more = len(page) > limit
        items = [
            _entry_payload(
                service,
                collection,
                hit.entry,
                detail=False,
                score=hit.score,
                disabled_entries=disabled_entries,
                source_cache=source_cache,
            )
            for hit in page[:limit]
        ]
        total = None
    else:
        total = await asyncio.to_thread(
            service.count_entries,
            collection,
            source_tag=source_tag,
        )
        entries = await asyncio.to_thread(
            service.list_entries,
            collection,
            source_tag=source_tag,
            limit=limit,
            offset=offset,
        )
        has_more = total > offset + len(entries)
        items = [
            _entry_payload(
                service,
                collection,
                entry,
                detail=False,
                disabled_entries=disabled_entries,
                source_cache=source_cache,
            )
            for entry in entries
        ]
    return {
        "ok": True,
        "collection": collection,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "items": items,
    }


@router.get("/entry")
async def get_public_knowledge_entry(
    collection: str = Query(..., min_length=1, max_length=64),
    source: str = Query(..., min_length=1, max_length=100),
    title: str = Query(..., min_length=1, max_length=500),
):
    service = _service()
    try:
        entry = await asyncio.to_thread(
            service.get_entry,
            collection,
            source_tag=_source_tag(source),
            title=title.strip(),
        )
    except ValueError:
        return {"ok": False, "reason": "unknown_collection"}
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    return {
        "ok": True,
        "entry": _entry_payload(service, collection, entry, detail=True),
    }


@router.post("/entry/disabled")
async def set_public_knowledge_entry_disabled(request: Request):
    payload = await _json_payload(request)
    rejected = _validate_mutation(request, payload)
    if rejected is not None:
        return rejected
    collection = str(payload.get("collection") or "").strip()
    source_tag = _source_tag(str(payload.get("source") or ""))
    title = str(payload.get("title") or "").strip()
    disabled = payload.get("disabled")
    if not collection or not source_tag or not title or not isinstance(disabled, bool):
        return {"ok": False, "reason": "invalid_request"}
    service = _service()
    try:
        entry = await asyncio.to_thread(
            service.get_entry,
            collection,
            source_tag=source_tag,
            title=title,
        )
    except ValueError:
        return {"ok": False, "reason": "unknown_collection"}
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    count = await asyncio.to_thread(
        service.set_entry_disabled,
        collection,
        source_tag=source_tag,
        title=title,
        disabled=disabled,
    )
    return {"ok": True, "disabled": disabled, "disabled_entries": count}


@router.post("/collection/auto-context")
async def set_public_knowledge_collection_auto_context(request: Request):
    payload = await _json_payload(request)
    rejected = _validate_mutation(request, payload)
    if rejected is not None:
        return rejected
    collection = str(payload.get("collection") or "").strip()
    enabled = payload.get("enabled")
    if not collection or not isinstance(enabled, bool):
        return {"ok": False, "reason": "invalid_request"}
    service = _service()
    try:
        await asyncio.to_thread(
            service.set_collection_auto_context,
            collection,
            enabled=enabled,
        )
    except ValueError:
        return {"ok": False, "reason": "unknown_collection"}
    return {"ok": True, "collection": collection, "auto_context": enabled}


@router.get("/packs")
async def list_public_knowledge_packs(
    collection: str = Query(..., min_length=1, max_length=64),
):
    try:
        packs = await asyncio.to_thread(_service().list_packs, collection)
    except ValueError:
        return {"ok": False, "reason": "unknown_collection"}
    return {"ok": True, "collection": collection, "packs": list(packs)}


@router.post("/packs/import")
async def import_public_knowledge_pack(request: Request):
    raw = await request.body()
    if len(raw) > MAX_PACK_BYTES + _PACK_ENVELOPE_OVERHEAD_BYTES:
        return {"ok": False, "reason": "pack_too_large"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rejected = _validate_mutation(request, payload)
    if rejected is not None:
        return rejected
    try:
        pack_payload = payload.get("pack")
        if len(canonical_pack_bytes(pack_payload)) > MAX_PACK_BYTES:
            return {"ok": False, "reason": "pack_too_large"}
        pack = validate_pack(pack_payload)
        result = await asyncio.to_thread(_service().install_pack, pack)
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": "invalid_pack", "error_type": type(exc).__name__}
    return {
        "ok": True,
        "pack_id": result.pack_id,
        "collection": result.collection_id,
        "source_tag": result.source_tag,
        "entries": result.entries,
    }


@router.post("/subscriptions/apply")
async def apply_public_knowledge_subscription(request: Request):
    """Install provider-verified data without coupling to a market protocol."""
    raw = await request.body()
    if len(raw) > MAX_PACK_BYTES + _PACK_ENVELOPE_OVERHEAD_BYTES:
        return {"ok": False, "reason": "pack_too_large"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rejected = _validate_mutation(request, payload)
    if rejected is not None:
        return rejected
    if payload.get("protocol_version") != SUBSCRIPTION_PROTOCOL_VERSION:
        return {"ok": False, "reason": "unsupported_protocol"}
    try:
        subscription = validate_subscription(payload.get("subscription"))
        pack_payload = payload.get("pack")
        pack_bytes = canonical_pack_bytes(pack_payload)
        if len(pack_bytes) > MAX_PACK_BYTES:
            return {"ok": False, "reason": "pack_too_large"}
        digest = hashlib.sha256(pack_bytes).hexdigest()
        if digest != subscription.artifact_sha256:
            return {"ok": False, "reason": "artifact_hash_mismatch"}
        pack = validate_pack(pack_payload)
        result = await asyncio.to_thread(
            _service().install_pack,
            pack,
            subscription=subscription.to_dict(),
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": "invalid_pack", "error_type": type(exc).__name__}
    return {
        "ok": True,
        "protocol_version": SUBSCRIPTION_PROTOCOL_VERSION,
        "provider": subscription.provider,
        "remote_id": subscription.remote_id,
        "pack_id": result.pack_id,
        "collection": result.collection_id,
        "entries": result.entries,
    }


@router.post("/packs/auto-context")
async def set_public_knowledge_pack_auto_context(request: Request):
    payload = await _json_payload(request)
    rejected = _validate_mutation(request, payload)
    if rejected is not None:
        return rejected
    collection = str(payload.get("collection") or "").strip()
    pack_id = str(payload.get("pack_id") or "").strip()
    enabled = payload.get("enabled")
    if not collection or not pack_id or not isinstance(enabled, bool):
        return {"ok": False, "reason": "invalid_request"}
    try:
        await asyncio.to_thread(
            _service().set_pack_auto_context,
            collection,
            pack_id,
            enabled=enabled,
        )
    except ValueError:
        return {"ok": False, "reason": "not_found"}
    return {"ok": True, "auto_context": enabled}


@router.post("/packs/remove")
async def remove_public_knowledge_pack(request: Request):
    payload = await _json_payload(request)
    rejected = _validate_mutation(request, payload)
    if rejected is not None:
        return rejected
    collection = str(payload.get("collection") or "").strip()
    pack_id = str(payload.get("pack_id") or "").strip()
    if not collection or not pack_id:
        return {"ok": False, "reason": "invalid_request"}
    try:
        removed = await asyncio.to_thread(
            _service().remove_pack,
            collection,
            pack_id,
        )
    except ValueError:
        return {"ok": False, "reason": "not_found"}
    return {"ok": True, "removed_entries": removed}


@router.get("/diagnostics/recent")
async def get_recent_public_knowledge_diagnostics():
    return {"ok": True, "items": list(list_recent_knowledge_routes())}


async def _json_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
