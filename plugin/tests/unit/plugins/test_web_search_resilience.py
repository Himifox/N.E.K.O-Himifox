"""Burst-control tests for the web_search plugin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from plugin.plugins import web_search
from plugin.plugins.web_search import _resilience as resilience
from tests.fake_clock import patch_module_clock

pytestmark = pytest.mark.plugin_unit


def _response(status: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("GET", "https://example.com/search"),
    )


class _PluginStub:
    _is_cn = False
    _backend = "duckduckgo"
    _user_agent = "test-agent"
    logger = type("Logger", (), {"warning": lambda *_args: None})()

    def __init__(self, *, total_timeout: float = 1.0) -> None:
        self._coordinator = resilience.SearchCoordinator()
        self._total_timeout = total_timeout

    @staticmethod
    def _get_client() -> object:
        return object()

    def _defaults(self) -> dict[str, float | int]:
        return {
            "retry_attempts": 2,
            "retry_base_delay": 0.0,
            "total_timeout": self._total_timeout,
        }


def test_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc)
    assert resilience.retry_after_seconds({"Retry-After": "2.5"}, now=now) == 2.5
    assert resilience.retry_after_seconds(
        {"Retry-After": "Wed, 12 Aug 2026 10:00:03 GMT"}, now=now
    ) == 3.0
    assert resilience.retry_after_seconds({"Retry-After": "invalid"}, now=now) is None


def test_rate_limit_and_retry_after_skip_endpoint_fallback() -> None:
    for response in (
        _response(429),
        _response(503, headers={"Retry-After": "2"}),
    ):
        error = httpx.HTTPStatusError(
            "upstream cooldown",
            request=response.request,
            response=response,
        )
        assert resilience.should_skip_fallback(error) is True

    ordinary_response = _response(500)
    ordinary_error = httpx.HTTPStatusError(
        "ordinary failure",
        request=ordinary_response.request,
        response=ordinary_response,
    )
    assert resilience.should_skip_fallback(ordinary_error) is False


@pytest.mark.parametrize(
    ("configured", "country", "expected"),
    [
        ("auto", "CN", "baidu"),
        ("auto", "JP", "duckduckgo"),
        ("auto", None, "baidu"),
        ("invalid", None, "baidu"),
        ("baidu", "JP", "baidu"),
        ("duckduckgo", "CN", "duckduckgo"),
    ],
)
def test_backend_selection_has_safe_fallback(
    configured: str,
    country: str | None,
    expected: str,
) -> None:
    assert web_search._select_backend(configured, country) == expected


def test_geoip_providers_are_https() -> None:
    assert web_search._GEOIP_PROVIDERS
    assert all(url.startswith("https://") for url, _field in web_search._GEOIP_PROVIDERS)


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
async def test_different_queries_are_serialized_per_backend() -> None:
    coordinator = resilience.SearchCoordinator(min_interval_seconds=0)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first() -> resilience.SearchResults:
        first_started.set()
        await release_first.wait()
        return [{"title": "first", "url": "https://example.com/1", "snippet": ""}]

    async def second() -> resilience.SearchResults:
        second_started.set()
        return [{"title": "second", "url": "https://example.com/2", "snippet": ""}]

    first_task = asyncio.create_task(coordinator.run(("ddg", "first"), first))
    await first_started.wait()
    second_task = asyncio.create_task(coordinator.run(("ddg", "second"), second))
    await asyncio.sleep(0)
    assert not second_started.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_different_query_queue_wait_is_bounded() -> None:
    coordinator = resilience.SearchCoordinator(
        min_interval_seconds=0,
        queue_wait_seconds=0.01,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> resilience.SearchResults:
        first_started.set()
        await release_first.wait()
        return [{"title": "first", "url": "https://example.com/1", "snippet": ""}]

    async def second() -> resilience.SearchResults:
        return [{"title": "second", "url": "https://example.com/2", "snippet": ""}]

    first_task = asyncio.create_task(coordinator.run(("ddg", "first"), first))
    await first_started.wait()
    with pytest.raises(resilience.SearchBusyError):
        await coordinator.run(("ddg", "second"), second)
    release_first.set()
    await first_task


@pytest.mark.asyncio
async def test_block_starts_backend_cooldown() -> None:
    coordinator = resilience.SearchCoordinator(
        min_interval_seconds=0,
        cooldown_seconds=30,
    )
    second_calls = 0

    async def blocked() -> resilience.SearchResults:
        raise web_search.SearchBlockedError("challenge", retry_after_seconds=10)

    async def second() -> resilience.SearchResults:
        nonlocal second_calls
        second_calls += 1
        return []

    with pytest.raises(web_search.SearchBlockedError):
        await coordinator.run(("ddg", "first"), blocked)
    with pytest.raises(resilience.SearchCooldownError):
        await coordinator.run(("ddg", "second"), second)
    assert second_calls == 0


@pytest.mark.asyncio
async def test_empty_results_are_not_cached() -> None:
    coordinator = resilience.SearchCoordinator(min_interval_seconds=0)
    calls = 0

    async def empty() -> resilience.SearchResults:
        nonlocal calls
        calls += 1
        return []

    assert await coordinator.run(("ddg", "empty"), empty) == []
    assert await coordinator.run(("ddg", "empty"), empty) == []
    assert calls == 2


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


@pytest.mark.asyncio
async def test_ddg_rate_limit_does_not_fall_back_to_lite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_calls = 0
    lite_calls = 0

    async def html(*_args: object, **_kwargs: object) -> resilience.SearchResults:
        nonlocal html_calls
        html_calls += 1
        response = _response(429, headers={"Retry-After": "120"})
        raise httpx.HTTPStatusError(
            "rate limited",
            request=response.request,
            response=response,
        )

    async def lite(*_args: object, **_kwargs: object) -> resilience.SearchResults:
        nonlocal lite_calls
        lite_calls += 1
        return []

    monkeypatch.setattr(web_search, "_search_ddg_html", html)
    monkeypatch.setattr(web_search, "_search_ddg_lite", lite)

    with pytest.raises(httpx.HTTPStatusError):
        await web_search.WebSearchPlugin._do_text_search(_PluginStub(), "neko", 3, 1.0)

    assert html_calls == 1
    assert lite_calls == 0


@pytest.mark.asyncio
async def test_ddg_202_is_reported_as_blocked() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, request=request, content=b"challenge")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.SearchBlockedError):
            await web_search._search_ddg_html(
                client,
                "neko",
                retry_attempts=1,
            )


@pytest.mark.asyncio
async def test_ddg_unparseable_200_is_not_a_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"<html></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.SearchResponseError):
            await web_search._search_ddg_lite(
                client,
                "neko",
                retry_attempts=1,
            )


@pytest.mark.asyncio
async def test_complete_search_has_one_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def slow_html(*_args: object, **_kwargs: object) -> resilience.SearchResults:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return []

    monkeypatch.setattr(web_search, "_search_ddg_html", slow_html)

    with pytest.raises(TimeoutError):
        await web_search.WebSearchPlugin._do_text_search(
            _PluginStub(total_timeout=0.01), "neko", 3, 15.0
        )

    assert cancelled.is_set()
