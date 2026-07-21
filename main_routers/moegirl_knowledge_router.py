"""Read-only diagnostics for the public meme knowledge runtime."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from knowledge.moegirl_knowledge.bundled_chime_runtime import (
    get_public_knowledge_status,
    request_bundled_chime_reimport,
)
from main_routers.shared_state import get_config_manager
from utils.logger_config import get_module_logger


router = APIRouter(prefix="/api/moegirl-knowledge", tags=["moegirl-knowledge"])
logger = get_module_logger(__name__, "Main")


@router.get("/status")
async def get_moegirl_knowledge_status():
    """Return source-level health without exposing queries or knowledge text."""
    try:
        payload = await asyncio.to_thread(get_public_knowledge_status, get_config_manager())
        return {"ok": True, **payload}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__}


@router.post("/chime/reimport")
async def reimport_bundled_chime(request: Request):
    """Reimport the fixed package asset after local-data maintenance."""
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
    outcome = request_bundled_chime_reimport(get_config_manager(), logger)
    return {"ok": outcome != "unavailable", "status": outcome}
