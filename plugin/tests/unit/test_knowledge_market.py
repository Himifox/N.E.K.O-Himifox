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
        if path == "packs/jobs":
            return {
                "ok": True,
                "jobs": [{
                    "job_id": "fixture-job",
                    "state": "active",
                    "retrieval_mode": "hybrid",
                    "indexed_percent": 100.0,
                }],
            }
        return {
            "ok": True,
            "job_id": "fixture-job",
            "pack_id": "fixture-pack",
            "collection_id": "meme",
            "state": "queued",
        }

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
    assert captured["path"] == "packs/jobs"
    result = module._tasks["fixture"]["result"]
    assert result["activation"]["state"] == "active"


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


@pytest.mark.asyncio
async def test_market_task_waits_for_staged_pack_activation(monkeypatch):
    calls = 0
    task = {"stage": "installing", "progress": 0.75, "message": ""}

    async def fake_main(_method, _path, **_kwargs):
        nonlocal calls
        calls += 1
        state = "embedding" if calls == 1 else "active"
        return {
            "ok": True,
            "jobs": [{
                "job_id": "fixture-job",
                "state": state,
                "indexed_percent": 50.0 if state == "embedding" else 100.0,
            }],
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(module, "_main_request", fake_main)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    result = await module._wait_for_pack_job(
        task,
        job_id="fixture-job",
        collection_id="meme",
    )

    assert calls == 2
    assert task["stage"] == "indexing"
    assert task["progress"] == pytest.approx(0.895)
    assert result["state"] == "active"
