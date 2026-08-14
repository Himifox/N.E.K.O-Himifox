from __future__ import annotations

import asyncio
import time

from plugin.core.message_plane_transport import MessagePlaneRpcClient
from plugin.core.state import state
from plugin.logging_config import get_logger
from plugin.server.domain import IO_RUNTIME_ERRORS
from plugin.server.domain.errors import ServerDomainError
from plugin.settings import MESSAGE_PLANE_ZMQ_RPC_ENDPOINT

logger = get_logger("server.application.messages.context_query")
_USER_CONTEXT_TTL_SECONDS = 60.0 * 60.0
_message_plane_client = MessagePlaneRpcClient(
    plugin_id="user_context_query",
    endpoint=str(MESSAGE_PLANE_ZMQ_RPC_ENDPOINT),
)


def _coerce_limit(value: object) -> int:
    if value is None:
        return 20
    if isinstance(value, bool):
        raise ServerDomainError(
            code="INVALID_ARGUMENT",
            message="limit must be an integer",
            status_code=400,
            details={},
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ServerDomainError(
            code="INVALID_ARGUMENT",
            message="limit must be an integer",
            status_code=400,
            details={},
        ) from exc
    if parsed <= 0:
        return 1
    if parsed > 500:
        return 500
    return parsed


def _message_plane_user_context(bucket_id: str, limit: int) -> list[dict[str, object]]:
    response = _message_plane_client.request_sync(
        op="bus.get_recent",
        args={
            "store": "memory",
            "topic": bucket_id,
            "limit": limit,
            "light": False,
        },
        timeout=1.0,
    )
    if not isinstance(response, dict) or not response.get("ok"):
        return []
    result = response.get("result")
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []

    cutoff = time.time() - _USER_CONTEXT_TTL_SECONDS
    history: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        normalized = {
            key: value for key, value in payload.items() if isinstance(key, str)
        }
        try:
            timestamp = float(normalized.get("_ts") or item.get("ts") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp < cutoff:
            continue
        normalized.setdefault("_ts", timestamp)
        history.append(normalized)
    return history


def _get_user_context_sync(bucket_id: str, limit: int) -> list[dict[str, object]]:
    remote_history = _message_plane_user_context(bucket_id, limit)
    local_history = state.get_user_context(bucket_id=bucket_id, limit=limit)

    history: list[dict[str, object]] = []
    seen: set[tuple[object, object, object, object]] = set()
    for item in [*local_history, *remote_history]:
        if not isinstance(item, dict):
            continue
        normalized: dict[str, object] = {}
        for key, value in item.items():
            if isinstance(key, str):
                normalized[key] = value
        dedupe_key = (
            normalized.get("_ts"),
            normalized.get("lanlan"),
            normalized.get("content"),
            normalized.get("is_voice"),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        history.append(normalized)
    history.sort(key=lambda item: float(item.get("_ts") or 0.0))
    return history[-limit:]


class UserContextQueryService:
    async def get_user_context(self, *, bucket_id: str, limit: object) -> dict[str, object]:
        if not isinstance(bucket_id, str) or not bucket_id:
            raise ServerDomainError(
                code="INVALID_ARGUMENT",
                message="bucket_id is required",
                status_code=400,
                details={},
            )
        normalized_limit = _coerce_limit(limit)
        try:
            history = await asyncio.to_thread(_get_user_context_sync, bucket_id, normalized_limit)
            return {"bucket_id": bucket_id, "history": history}
        except ServerDomainError:
            raise
        except IO_RUNTIME_ERRORS as exc:
            logger.error(
                "get_user_context failed: bucket_id={}, err_type={}, err={}",
                bucket_id,
                type(exc).__name__,
                str(exc),
            )
            raise ServerDomainError(
                code="USER_CONTEXT_QUERY_FAILED",
                message="Failed to query user context",
                status_code=500,
                details={"bucket_id": bucket_id, "error_type": type(exc).__name__},
            ) from exc
