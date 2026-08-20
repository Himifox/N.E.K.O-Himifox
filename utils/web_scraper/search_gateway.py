"""Controlled main-process access to the web-search plugin.

All proactive search traffic goes through the plugin run protocol so the
plugin owns upstream caching, rate limits, cooldowns, and backend fallback.
This module adds a longer-lived proactive cache and failure cooldown to avoid
turning frequent context refreshes into frequent plugin runs.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
from typing import Any, Dict, Optional
import uuid

import httpx

from config import USER_PLUGIN_SERVER_PORT

from ._shared import logger


_CACHE_TTL_SECONDS = 600.0
_FAILURE_COOLDOWN_SECONDS = 300.0
_MIN_RUN_INTERVAL_SECONDS = 5.0
_MAX_CACHE_ENTRIES = 64
_RUN_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.25

_cache: OrderedDict[tuple[str, str, int], tuple[float, Dict[str, Any]]] = OrderedDict()
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None
_next_run_at = 0.0
_failure_cooldown_until = 0.0


def _copy_result(result: Dict[str, Any]) -> Dict[str, Any]:
    copied = dict(result)
    copied["results"] = [
        dict(item) for item in result.get("results", []) if isinstance(item, dict)
    ]
    return copied


def _gateway_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _cached(key: tuple[str, str, int]) -> Optional[Dict[str, Any]]:
    cached = _cache.get(key)
    if cached is None:
        return None
    expires_at, result = cached
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return _copy_result(result)


def _store(key: tuple[str, str, int], result: Dict[str, Any]) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, _copy_result(result))
    _cache.move_to_end(key)
    while len(_cache) > _MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)


def _error_message(value: object, default: str) -> str:
    if isinstance(value, dict):
        return str(value.get("message") or value.get("code") or default)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _extract_export(payload: object) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("搜索插件返回了无效导出数据")
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "json":
            continue
        raw = item.get("json")
        if raw is None:
            raw = item.get("json_data")
        if not isinstance(raw, dict):
            continue
        if raw.get("error"):
            raise RuntimeError(_error_message(raw.get("error"), "搜索插件执行失败"))
        data = raw.get("data")
        if isinstance(data, dict):
            return data
    raise RuntimeError("搜索插件没有返回结构化结果")


async def _invoke_plugin(
    query: str,
    limit: int,
    backend: Optional[str],
) -> Dict[str, Any]:
    base = f"http://127.0.0.1:{USER_PLUGIN_SERVER_PORT}"
    args: Dict[str, Any] = {
        "query": query,
        "max_results": limit,
        "_ctx": {"entry_timeout": _RUN_TIMEOUT_SECONDS},
    }
    if backend in {"baidu", "duckduckgo"}:
        args["backend"] = backend
    body = {
        "task_id": f"proactive-search-{uuid.uuid4().hex}",
        "plugin_id": "web_search",
        "entry_id": "search",
        "args": args,
    }
    timeout = httpx.Timeout(5.0, connect=1.0)
    async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False) as client:
        response = await client.post(f"{base}/runs", json=body)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"联网搜索插件不可用（HTTP {response.status_code}）")
        accepted = response.json()
        run_id = accepted.get("run_id") if isinstance(accepted, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("联网搜索插件未返回有效任务编号")

        deadline = asyncio.get_running_loop().time() + _RUN_TIMEOUT_SECONDS
        terminal = {"succeeded", "failed", "canceled", "timeout"}
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("等待联网搜索插件超时")
            response = await client.get(f"{base}/runs/{run_id}")
            if response.status_code != 200:
                raise RuntimeError(f"读取联网搜索任务失败（HTTP {response.status_code}）")
            candidate = response.json()
            run_data = candidate if isinstance(candidate, dict) else {}
            status = str(run_data.get("status") or "")
            if status in terminal:
                if status != "succeeded":
                    raise RuntimeError(
                        _error_message(run_data.get("error"), f"联网搜索任务状态：{status}")
                    )
                break
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

        response = await client.get(f"{base}/runs/{run_id}/export", params={"limit": 20})
        if response.status_code != 200:
            raise RuntimeError(f"导出联网搜索结果失败（HTTP {response.status_code}）")
        data = _extract_export(response.json())

    normalized_results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        normalized_results.append(
            {
                "title": title,
                "abstract": str(item.get("snippet") or item.get("abstract") or "").strip(),
                "url": url,
            }
        )
    return {
        "success": bool(normalized_results),
        "query": query,
        "results": normalized_results,
        "error": "" if normalized_results else "未获得搜索结果",
    }


async def search_via_plugin(
    query: str,
    limit: int = 5,
    *,
    backend: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a cached, rate-limited search through the web_search plugin."""
    global _next_run_at, _failure_cooldown_until

    normalized_query = " ".join(str(query or "").split())
    if len(normalized_query) < 2:
        return {"success": False, "error": "搜索关键词太短", "results": []}
    bounded_limit = max(3, min(int(limit), 10))
    selected_backend = backend if backend in {"baidu", "duckduckgo"} else "auto"
    key = (selected_backend, normalized_query.casefold(), bounded_limit)
    cached = _cached(key)
    if cached is not None:
        return cached

    async with _gateway_lock():
        cached = _cached(key)
        if cached is not None:
            return cached
        now = time.monotonic()
        if now < _failure_cooldown_until:
            return {
                "success": False,
                "error": "联网搜索暂处于失败冷却期",
                "results": [],
            }
        if now < _next_run_at:
            await asyncio.sleep(_next_run_at - now)

        try:
            result = await _invoke_plugin(normalized_query, bounded_limit, backend)
        except Exception as error:
            _failure_cooldown_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
            logger.warning(
                "受控联网搜索失败 (query_len=%s, error_type=%s)",
                len(normalized_query),
                type(error).__name__,
            )
            return {
                "success": False,
                "error": str(error),
                "results": [],
            }
        finally:
            _next_run_at = time.monotonic() + _MIN_RUN_INTERVAL_SECONDS

        # A valid empty result is cacheable too; otherwise a stable obscure
        # window title would still trigger one upstream search every refresh.
        _store(key, result)
        return _copy_result(result)
