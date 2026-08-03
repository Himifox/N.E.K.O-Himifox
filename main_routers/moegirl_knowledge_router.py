"""Local management API for the public meme knowledge database."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request

from knowledge.api import KnowledgeService, open_knowledge
from knowledge.engine.catalog_overrides import (
    entry_key,
    get_catalog_override_path,
    load_disabled_entries,
)
from knowledge.engine.source_registry import get_source
from knowledge.moegirl_knowledge.status import get_public_knowledge_status
from main_routers.shared_state import get_config_manager


router = APIRouter(prefix="/api/moegirl-knowledge", tags=["moegirl-knowledge"])


def _service() -> KnowledgeService:
    return open_knowledge(get_config_manager().knowledge_dir)


def _source_tag(value: str) -> str:
    value = str(value or "").strip()
    return value if not value or value.startswith("source:") else f"source:{value}"


def _entry_payload(
    entry,
    *,
    database_path,
    disabled: bool,
    score: float | None = None,
    detail: bool = False,
) -> dict:
    source = get_source(entry.source_tag, database_path=database_path)
    payload = {
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
    if score is not None:
        payload["score"] = score
    if detail:
        payload["content"] = entry.content
    return payload


@router.get("/status")
async def get_moegirl_knowledge_status():
    """Return local database and source-level health without knowledge text."""
    try:
        payload = await asyncio.to_thread(get_public_knowledge_status, get_config_manager())
        return {"ok": True, **payload}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__}


@router.get("/entries")
async def list_moegirl_knowledge_entries(
    query: str = Query(default="", max_length=200),
    source: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
):
    """Browse local cards or diagnose retrieval using the production retriever."""
    service = _service()
    database_path = service.database_path("meme")
    source_tag = _source_tag(source)
    disabled = await asyncio.to_thread(
        load_disabled_entries,
        get_catalog_override_path(database_path),
    )
    if query.strip():
        page = await asyncio.to_thread(
            service.search_page,
            "meme",
            query.strip(),
            source_tag=source_tag,
            limit=limit,
            offset=offset,
            include_disabled=True,
        )
        has_more = len(page) > limit
        visible = page[:limit]
        total = None
        items = [
            _entry_payload(
                hit.entry,
                database_path=database_path,
                disabled=entry_key(hit.entry) in disabled,
                score=hit.score,
            )
            for hit in visible
        ]
    else:
        total = await asyncio.to_thread(
            service.count_entries,
            "meme",
            source_tag=source_tag,
        )
        entries = await asyncio.to_thread(
            service.list_entries,
            "meme",
            source_tag=source_tag,
            limit=limit,
            offset=offset,
        )
        items = [
            _entry_payload(
                entry,
                database_path=database_path,
                disabled=entry_key(entry) in disabled,
            )
            for entry in entries
        ]
        has_more = total > offset + len(entries)
    return {
        "ok": True,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "items": items,
    }


@router.get("/entry")
async def get_moegirl_knowledge_entry(
    source: str = Query(..., min_length=1, max_length=80),
    title: str = Query(..., min_length=1, max_length=500),
):
    """Return one complete five-field local card selected by source and title."""
    service = _service()
    database_path = service.database_path("meme")
    entry = await asyncio.to_thread(
        service.get_entry,
        "meme",
        source_tag=_source_tag(source),
        title=title.strip(),
    )
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    disabled = load_disabled_entries(
        get_catalog_override_path(database_path)
    )
    return {"ok": True, "entry": _entry_payload(
        entry,
        database_path=database_path,
        disabled=entry_key(entry) in disabled,
        detail=True,
    )}


@router.post("/entry/disabled")
async def set_moegirl_knowledge_entry_disabled(request: Request):
    """Disable or restore one local card without changing the database row."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    from .system_router import _validate_local_mutation_request

    rejected = _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )
    if rejected is not None:
        return rejected
    source_tag = _source_tag(str(payload.get("source") or ""))
    title = str(payload.get("title") or "").strip()
    disabled = payload.get("disabled")
    if not source_tag.startswith("source:") or not title or not isinstance(disabled, bool):
        return {"ok": False, "reason": "invalid_request"}
    service = _service()
    entry = await asyncio.to_thread(
        service.get_entry,
        "meme",
        source_tag=source_tag,
        title=title,
    )
    if entry is None:
        return {"ok": False, "reason": "not_found"}
    count = await asyncio.to_thread(
        service.set_entry_disabled,
        "meme",
        source_tag=source_tag,
        title=title,
        disabled=disabled,
    )
    return {"ok": True, "disabled": disabled, "disabled_entries": count}
