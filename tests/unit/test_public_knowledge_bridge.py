from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, captured: dict) -> TestClient:
    from plugin.server.routes import market_bridge as module

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, target, **kwargs):
            captured.update(method=method, target=target, **kwargs)
            return httpx.Response(
                200,
                content=b'{"ok":true}',
                headers={"content-type": "application/json"},
            )

    monkeypatch.setattr(module, "_verify_token", lambda _token: None)
    monkeypatch.setattr(module, "_main_server_port", lambda: 48911)
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def test_knowledge_bridge_forwards_only_allowlisted_local_api(monkeypatch):
    captured = {}
    client = _client(monkeypatch, captured)

    response = client.post(
        "/market/knowledge/packs/import",
        params={"token": "fixture"},
        json={"pack": {"schema_version": 1}},
    )

    assert response.json() == {"ok": True}
    assert captured["target"] == (
        "http://127.0.0.1:48911/api/public-knowledge/packs/import"
    )
    assert ("token", "fixture") not in captured["params"]
    assert captured["headers"]["Origin"] == "http://127.0.0.1:48911"
    assert captured["headers"]["X-CSRF-Token"]


def test_knowledge_bridge_rejects_arbitrary_proxy_paths(monkeypatch):
    captured = {}
    client = _client(monkeypatch, captured)

    response = client.get(
        "/market/knowledge/system/config",
        params={"token": "fixture"},
    )

    assert response.status_code == 404
    assert captured == {}


def test_remote_market_origin_cannot_call_management_proxy(monkeypatch):
    captured = {}
    client = _client(monkeypatch, captured)

    response = client.get(
        "/market/knowledge/collections",
        params={"token": "fixture"},
        headers={"Origin": "https://market.example.com"},
    )

    assert response.status_code == 403
    assert captured == {}


def test_local_vite_origin_can_use_a_different_loopback_port(monkeypatch):
    captured = {}
    client = _client(monkeypatch, captured)

    response = client.get(
        "/market/knowledge/collections",
        params={"token": "fixture"},
        headers={"Origin": "http://127.0.0.1:5173"},
    )

    assert response.status_code == 200
    assert captured["target"].endswith("/api/public-knowledge/collections")


def test_knowledge_proxy_rejects_oversized_body(monkeypatch):
    from plugin.server.routes import market_bridge as module

    captured = {}
    client = _client(monkeypatch, captured)
    monkeypatch.setattr(module, "MAX_PACK_BYTES", 16)

    response = client.post(
        "/market/knowledge/packs/import",
        params={"token": "fixture"},
        content=b"x" * (module._KNOWLEDGE_BRIDGE_ENVELOPE_BYTES + 17),
    )

    assert response.status_code == 413
    assert captured == {}
