"""Burst-control tests for the web_search plugin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from plugin.plugins.web_search import _resilience as resilience
from tests.fake_clock import patch_module_clock

pytestmark = pytest.mark.plugin_unit


def _response(status: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("GET", "https://example.com/search"),
    )


def test_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc)
    assert resilience.retry_after_seconds({"Retry-After": "2.5"}, now=now) == 2.5
    assert resilience.retry_after_seconds(
        {"Retry-After": "Wed, 12 Aug 2026 10:00:03 GMT"}, now=now
    ) == 3.0
    assert resilience.retry_after_seconds({"Retry-After": "invalid"}, now=now) is None


@pytest.mark.asyncio
async def test_request_retries_transient_status_once(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [_response(429, headers={"Retry-After": "0"}), _response(200)]
    sleeps: list[float] = []

    async def request() -> httpx.Response:
        return responses.pop(0)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(resilience.asyncio, "sleep", sleep)
    monkeypatch.setattr(resilience.random, "uniform", lambda _a, _b: 0.0)

    result = await resilience.request_with_retry(request, max_attempts=2)

    assert result.status_code == 200
    assert sleeps == [0.0]
    assert responses == []


@pytest.mark.asyncio
async def test_request_does_not_retry_non_transient_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def request() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(404)

    with pytest.raises(httpx.HTTPStatusError):
        await resilience.request_with_retry(request, max_attempts=3)

    assert calls == 1


@pytest.mark.asyncio
async def test_long_retry_after_is_not_retried_early() -> None:
    calls = 0

    async def request() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(429, headers={"Retry-After": "120"})

    with pytest.raises(httpx.HTTPStatusError):
        await resilience.request_with_retry(request, max_attempts=2, max_delay=4)

    assert calls == 1


@pytest.mark.asyncio
async def test_same_query_burst_is_coalesced_and_cached() -> None:
    coordinator = resilience.SearchCoordinator(ttl_seconds=60, stale_seconds=60)
    calls = 0
    release = asyncio.Event()

    async def fetch() -> resilience.SearchResults:
        nonlocal calls
        calls += 1
        await release.wait()
        return [{"title": "NEKO", "url": "https://example.com", "snippet": "ok"}]

    tasks = [asyncio.create_task(coordinator.run(("ddg", "neko", 3), fetch)) for _ in range(20)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all(result[0]["title"] == "NEKO" for result in results)

    # Cache values are defensive copies: a caller cannot corrupt later hits.
    results[0][0]["title"] = "changed"
    cached = await coordinator.run(("ddg", "neko", 3), fetch)
    assert calls == 1
    assert cached[0]["title"] == "NEKO"


@pytest.mark.asyncio
async def test_stale_cache_is_used_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    patch_module_clock(monkeypatch, resilience, monotonic=lambda: now)
    coordinator = resilience.SearchCoordinator(ttl_seconds=5, stale_seconds=30)

    async def first() -> resilience.SearchResults:
        return [{"title": "cached", "url": "https://example.com", "snippet": ""}]

    assert (await coordinator.run("key", first))[0]["title"] == "cached"
    now = 106.0

    async def failing() -> resilience.SearchResults:
        raise httpx.ConnectTimeout("temporary")

    assert (await coordinator.run("key", failing))[0]["title"] == "cached"


@pytest.mark.asyncio
async def test_failure_without_cache_is_not_hidden() -> None:
    coordinator = resilience.SearchCoordinator(ttl_seconds=5, stale_seconds=30)

    async def failing() -> resilience.SearchResults:
        raise httpx.ConnectTimeout("temporary")

    with pytest.raises(httpx.ConnectTimeout):
        await coordinator.run("key", failing)
