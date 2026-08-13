"""Small resilience helpers for the web-search plugin.

The caller may invoke search frequently, but identical concurrent queries are
collapsed into one upstream request and recent results are served from memory.
Network retries are deliberately bounded so a traffic spike is not amplified.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
from typing import Awaitable, Callable, Dict, Hashable, List, Mapping, Optional

import httpx

SearchResults = List[Dict[str, str]]

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _copy_results(results: SearchResults) -> SearchResults:
    return [dict(item) for item in results]


def retry_after_seconds(
    headers: Mapping[str, str],
    *,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Parse Retry-After seconds or an HTTP date; invalid values are ignored."""
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (target - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def should_skip_fallback(error: BaseException) -> bool:
    """Return whether another endpoint would violate an upstream cooldown."""
    if not isinstance(error, httpx.HTTPStatusError):
        return False
    response = error.response
    return response.status_code == 429 or retry_after_seconds(response.headers) is not None


async def request_with_retry(
    request: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: int = 2,
    base_delay: float = 0.5,
    max_delay: float = 4.0,
) -> httpx.Response:
    """Run an HTTP request with one bounded transient-failure retry by default."""
    attempts = max(1, min(int(max_attempts), 3))
    base = max(0.0, float(base_delay))
    delay_cap = max(base, float(max_delay))

    for attempt in range(attempts):
        try:
            response = await request()
            if response.status_code not in _RETRYABLE_STATUS:
                response.raise_for_status()
                return response
            if attempt + 1 >= attempts:
                response.raise_for_status()
            server_delay = retry_after_seconds(response.headers)
            # Never retry earlier than the server requested. A long cooldown is
            # outside this interactive call's small retry budget, so surface the
            # 429/5xx now instead of increasing anti-bot pressure.
            if server_delay is not None and server_delay > delay_cap:
                response.raise_for_status()
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt + 1 >= attempts:
                raise
            server_delay = None

        exponential = min(delay_cap, base * (2**attempt))
        delay = server_delay if server_delay is not None else exponential
        # A small jitter prevents simultaneous callers from retrying in lockstep.
        delay = min(delay_cap, delay + random.uniform(0.0, max(0.05, delay * 0.25)))
        await asyncio.sleep(delay)

    raise RuntimeError("unreachable retry state")


@dataclass(frozen=True)
class _CacheEntry:
    results: SearchResults
    fresh_until: float
    stale_until: float


class SearchCoordinator:
    """TTL cache plus single-flight coalescing for identical searches."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 120.0,
        stale_seconds: float = 600.0,
        max_entries: int = 128,
    ) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.stale_seconds = max(0.0, float(stale_seconds))
        self.max_entries = max(1, min(int(max_entries), 1024))
        self._cache: OrderedDict[Hashable, _CacheEntry] = OrderedDict()
        self._inflight: Dict[Hashable, asyncio.Task[SearchResults]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _entry(self, key: Hashable, *, fresh: bool) -> Optional[SearchResults]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        deadline = entry.fresh_until if fresh else entry.stale_until
        if time.monotonic() > deadline:
            if not fresh:
                self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return _copy_results(entry.results)

    def _store(self, key: Hashable, results: SearchResults) -> None:
        now = time.monotonic()
        self._cache[key] = _CacheEntry(
            results=_copy_results(results),
            fresh_until=now + self.ttl_seconds,
            stale_until=now + self.ttl_seconds + self.stale_seconds,
        )
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

    async def run(
        self,
        key: Hashable,
        fetch: Callable[[], Awaitable[SearchResults]],
    ) -> SearchResults:
        cached = self._entry(key, fresh=True)
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # Host lifecycles may use separate asyncio.run() calls. Futures must
            # never leak into a different event loop; the plain cache may persist.
            self._loop = loop
            self._inflight.clear()

        task = self._inflight.get(key)
        if task is None:
            task = loop.create_task(fetch())
            self._inflight[key] = task

            def finish(done: asyncio.Task[SearchResults]) -> None:
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)
                if not done.cancelled() and done.exception() is None:
                    self._store(key, done.result())

            task.add_done_callback(finish)

        try:
            return _copy_results(await asyncio.shield(task))
        except Exception:
            stale = self._entry(key, fresh=False)
            if stale is not None:
                return stale
            raise
