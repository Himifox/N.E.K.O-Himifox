from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import aiohttp
import pytest
from aiohttp import web

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _name, _path in (
    ("plugin", _REPO_ROOT / "plugin"),
    ("plugin.plugins", _REPO_ROOT / "plugin" / "plugins"),
    (
        "plugin.plugins.proactive_recommender",
        _REPO_ROOT / "plugin" / "plugins" / "proactive_recommender",
    ),
):
    if _name not in sys.modules:
        _module = ModuleType(_name)
        _module.__path__ = [str(_path)]  # type: ignore[attr-defined]
        sys.modules[_name] = _module

from plugin.plugins.proactive_recommender.openbiliclaw_compat import (
    OpenBiliClawCompatibilityServer,
)
from plugin.plugins.proactive_recommender.openbiliclaw_recommendations import (
    fetch_openbiliclaw_recommendations,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_extension_background_contract_and_cookie_boundary() -> None:
    received: list[Mapping[str, Any]] = []

    async def on_events(events: list[Mapping[str, Any]]) -> dict[str, Any]:
        received.extend(events)
        return {"accepted": len(events), "rejected": []}

    port = _free_port()
    server = OpenBiliClawCompatibilityServer(
        host="127.0.0.1",
        port=port,
        on_events=on_events,
        status_provider=lambda: {
            "initialized": True,
            "recommendation_count": 0,
            "pending_signal_events": 0,
            "unread_count": 0,
        },
        logger=None,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as client:
            ping = await client.get(f"http://127.0.0.1:{port}/api/ping")
            assert ping.status == 200
            assert (await ping.json())["service"] == "neko-openbiliclaw-compat"

            status = await client.get(f"http://127.0.0.1:{port}/api/runtime-status")
            assert (await status.json())["initialized"] is True

            response = await client.post(
                f"http://127.0.0.1:{port}/api/events",
                json={
                    "events": [
                        {
                            "event_id": "evt-1",
                            "type": "like",
                            "url": "https://www.bilibili.com/video/BV1xx",
                            "title": "Rust 教程",
                            "timestamp": 1_765_000_000_000,
                            "source_platform": "bilibili",
                            "context": {"pageType": "video"},
                            "metadata": {"bvid": "BV1xx"},
                        }
                    ]
                },
            )
            assert await response.json() == {"accepted": 1, "rejected": []}
            assert received[0]["event_id"] == "evt-1"

            cookie = await client.post(
                f"http://127.0.0.1:{port}/api/bilibili/cookie",
                json={"cookie": "SESSDATA=secret; bili_jct=secret; DedeUserID=1"},
            )
            cookie_result = await cookie.json()
            assert cookie_result["ok"] is False
            assert cookie_result["error_code"] == "cookie_storage_disabled"

            websocket = await client.ws_connect(
                f"http://127.0.0.1:{port}/api/runtime-stream?client=background"
            )
            event = await websocket.receive_json()
            assert event["type"] == "compat.connected"
            await websocket.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_event_endpoint_rejects_malformed_batches() -> None:
    async def on_events(_: list[Mapping[str, Any]]) -> dict[str, Any]:
        return {"accepted": 0, "rejected": []}

    port = _free_port()
    server = OpenBiliClawCompatibilityServer(
        host="127.0.0.1",
        port=port,
        on_events=on_events,
        status_provider=dict,
        logger=None,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/api/events", json={"events": "bad"}
            )
            assert response.status == 422
            assert (await response.json())["accepted"] == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stop_honors_short_timeout_when_server_thread_is_stuck() -> None:
    async def on_events(_: list[Mapping[str, Any]]) -> dict[str, Any]:
        return {"accepted": 0, "rejected": []}

    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    server = OpenBiliClawCompatibilityServer(
        host="127.0.0.1",
        port=_free_port(),
        on_events=on_events,
        status_provider=dict,
        logger=None,
    )
    server._thread = thread

    started_at = time.monotonic()
    try:
        assert await server.stop(timeout=0.05) is False
        assert time.monotonic() - started_at < 0.3
        assert server._thread is thread
    finally:
        release.set()
        thread.join(timeout=1.0)

    assert await server.stop(timeout=0.05) is True
    assert server._thread is None


@pytest.mark.asyncio
async def test_recommendation_client_reads_official_openbiliclaw_contract() -> None:
    async def recommendations(_: Any) -> web.Response:
        return web.json_response(
            {
                "items": [
                    {
                        "id": 7,
                        "item_key": "youtube:abc",
                        "title": "A careful systems design talk",
                        "expression": "It connects several topics you follow.",
                        "topic_label": "systems design",
                        "content_url": "https://www.youtube.com/watch?v=abc",
                        "source_platform": "youtube",
                    }
                ]
            }
        )

    port = _free_port()
    app = web.Application()
    app.router.add_get("/api/recommendations", recommendations)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        result = await fetch_openbiliclaw_recommendations(port=port)
        assert result.error == ""
        assert result.candidates[0]["source"] == "openbiliclaw:youtube"
        assert result.candidates[0]["openbiliclaw_id"] == "7"
    finally:
        await runner.cleanup()
