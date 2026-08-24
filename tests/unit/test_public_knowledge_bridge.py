from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
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


def test_bridge_token_error_uses_stable_code():
    from plugin.server.routes import market_bridge as module

    with pytest.raises(HTTPException) as failure:
        module._verify_token("not-the-current-token")

    assert failure.value.status_code == 403
    assert failure.value.detail == {
        "code": "invalid_bridge_token",
        "message": "无效的 bridge token",
    }
