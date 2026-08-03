from __future__ import annotations

import hashlib

import pytest

from knowledge.api import canonical_pack_bytes
from plugin.server.routes import knowledge_market as module


def _pack():
    return {
        "schema_version": 1,
        "pack_id": "fixture-pack",
        "collection_id": "meme",
        "source": {"name": "Fixture", "homepage": "", "license": "CC0-1.0"},
        "entries": [{
            "title": "fixture",
            "terms": {"alias": [], "recognition": []},
            "tags": [],
            "summary": "",
            "content": "fixture content",
        }],
    }


@pytest.mark.asyncio
async def test_market_subscription_downloads_verifies_and_hands_off(monkeypatch):
    raw = canonical_pack_bytes(_pack())
    digest = hashlib.sha256(raw).hexdigest()
    request = module.KnowledgeSubscribeRequest(
        package_id=7,
        remote_id="knowledge/fixture-pack",
        pack_id="fixture-pack",
        version="1.0.0",
        channel="stable",
        artifact_url="https://github.com/example/repo/releases/download/v1/fixture.neko-knowledge.json",
        artifact_sha256=digest,
    )
    captured = {}

    async def fake_download(_url):
        return raw

    async def fake_main(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"ok": True, "pack_id": "fixture-pack", "entries": 1}

    async def no_report(*_args):
        return None

    monkeypatch.setattr(module, "_download_artifact", fake_download)
    monkeypatch.setattr(module, "_main_request", fake_main)
    monkeypatch.setattr(module, "_report_subscription_best_effort", no_report)
    module._tasks["fixture"] = {
        "status": "pending",
        "stage": "pending",
        "progress": 0.0,
        "message": "",
        "result": None,
        "error": None,
        "error_code": None,
        "completed_at": None,
    }

    await module._execute_subscription("fixture", request)

    assert module._tasks["fixture"]["status"] == "completed"
    assert captured["path"] == "subscriptions/apply"
    assert captured["json"]["subscription"]["provider"] == "plugin-market"
    assert captured["json"]["pack"]["pack_id"] == "fixture-pack"


@pytest.mark.asyncio
async def test_market_subscription_rejects_hash_mismatch(monkeypatch):
    raw = canonical_pack_bytes(_pack())
    request = module.KnowledgeSubscribeRequest(
        package_id=7,
        remote_id="knowledge/fixture-pack",
        pack_id="fixture-pack",
        version="1.0.0",
        artifact_url="https://github.com/example/repo/releases/download/v1/fixture.neko-knowledge.json",
        artifact_sha256="0" * 64,
    )

    async def fake_download(_url):
        return raw

    monkeypatch.setattr(module, "_download_artifact", fake_download)
    module._tasks["mismatch"] = {
        "status": "pending",
        "stage": "pending",
        "progress": 0.0,
        "message": "",
        "result": None,
        "error": None,
        "error_code": None,
        "completed_at": None,
    }

    await module._execute_subscription("mismatch", request)

    assert module._tasks["mismatch"]["status"] == "failed"
    assert module._tasks["mismatch"]["error_code"] == "artifact_hash_mismatch"
