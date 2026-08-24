from __future__ import annotations

import hashlib

import httpx
import pytest

from knowledge.api import canonical_pack_bytes
from plugin.server.routes import knowledge_market as module


def _pack():
    return {
        "schema_version": 1,
        "pack_id": "fixture-pack",
        "material_type": "knowledge",
        "source": {"name": "Fixture", "homepage": "", "license": "CC0-1.0"},
        "entries": [
            {
                "title": "fixture",
                "terms": {"alias": [], "recognition": []},
                "tags": [],
                "summary": "",
                "content": "fixture content",
            }
        ],
    }


@pytest.mark.asyncio
async def test_market_subscription_downloads_verifies_and_hands_off(monkeypatch):
    raw = canonical_pack_bytes(_pack())
    digest = hashlib.sha256(raw).hexdigest()
    request = module.KnowledgeSubscribeRequest(
        package_id=7,
        version="1.0.0",
        channel="stable",
    )
    descriptor = module.KnowledgeVersionDescriptor.model_validate(
        {
            "protocol_version": 1,
            "package_id": 7,
            "remote_id": "knowledge/fixture-pack",
            "pack_id": "fixture-pack",
            "material_type": "knowledge",
            "version": "1.0.0",
            "channel": "stable",
            "artifacts": {
                "knowledge": {
                    "url": "https://github.com/example/repo/releases/download/v1/fixture.neko-knowledge.json",
                    "sha256": digest,
                    "bytes": len(raw),
                },
                "index_manifest": None,
                "vectors": None,
            },
        }
    )
    captured = {}

    async def fake_fetch(_request):
        return descriptor

    async def fake_download(_descriptor, **_kwargs):
        return raw

    async def fake_subscription_main(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "job_id": "fixture-job",
            "pack_id": "fixture-pack",
            "state": "queued",
        }

    async def fake_main(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        if path == "packs/jobs":
            return {
                "ok": True,
                "jobs": [
                    {
                        "job_id": "fixture-job",
                        "state": "active",
                        "retrieval_mode": "hybrid",
                        "indexed_percent": 100.0,
                    }
                ],
            }
        raise AssertionError(path)

    async def no_report(*_args):
        return None

    monkeypatch.setattr(module, "_fetch_version_descriptor", fake_fetch)
    monkeypatch.setattr(module, "_download_verified_artifact", fake_download)
    monkeypatch.setattr(module, "_main_subscription_request", fake_subscription_main)
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
    assert captured["pack_raw"] == raw
    assert captured["manifest_raw"] is None
    assert captured["subscription"]["artifact_sha256"] == digest
    assert captured["subscription"]["material_type"] == "knowledge"
    result = module._tasks["fixture"]["result"]
    assert result["activation"]["state"] == "active"


@pytest.mark.asyncio
async def test_market_subscription_rejects_material_type_mismatch(monkeypatch):
    raw = canonical_pack_bytes(_pack())
    descriptor = module.KnowledgeVersionDescriptor.model_validate(
        {
            "protocol_version": 1,
            "package_id": 7,
            "remote_id": "knowledge/fixture-pack",
            "pack_id": "fixture-pack",
            "material_type": "corpus",
            "version": "1.0.0",
            "channel": "stable",
            "artifacts": {
                "knowledge": {
                    "url": "https://github.com/example/repo/releases/download/v1/fixture.neko-knowledge.json",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                },
                "index_manifest": None,
                "vectors": None,
            },
        }
    )

    async def fake_fetch(_request):
        return descriptor

    async def fake_download(_descriptor, **_kwargs):
        return raw

    monkeypatch.setattr(module, "_fetch_version_descriptor", fake_fetch)
    monkeypatch.setattr(module, "_download_verified_artifact", fake_download)
    module._tasks["material-type-mismatch"] = {
        "status": "pending",
        "stage": "pending",
        "progress": 0.0,
        "message": "",
        "result": None,
        "error": None,
        "error_code": None,
        "completed_at": None,
    }

    request = module.KnowledgeSubscribeRequest(
        package_id=7,
        version="1.0.0",
        channel="stable",
    )
    await module._execute_subscription("material-type-mismatch", request)

    task = module._tasks["material-type-mismatch"]
    assert task["status"] == "failed"
    assert task["error_code"] == "material_type_mismatch"


@pytest.mark.asyncio
async def test_market_subscription_rejects_hash_mismatch(monkeypatch):
    raw = canonical_pack_bytes(_pack())
    descriptor = module.KnowledgeArtifactDescriptor(
        url="https://github.com/example/repo/releases/download/v1/fixture.neko-knowledge.json",
        sha256="0" * 64,
        bytes=len(raw),
    )

    async def fake_download(_url, **_kwargs):
        return raw

    monkeypatch.setattr(module, "_download_artifact", fake_download)
    with pytest.raises(module._KnowledgeTaskError) as exc_info:
        await module._download_verified_artifact(
            descriptor,
            max_bytes=module.MAX_PACK_BYTES,
            required_suffix=".neko-knowledge.json",
        )
    assert exc_info.value.code == "artifact_hash_mismatch"


@pytest.mark.asyncio
async def test_artifact_download_validates_redirect_before_request(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://127.0.0.1/private"},
            request=request,
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(module._KnowledgeTaskError) as exc_info:
        await module._download_artifact(
            "https://github.com/example/repo/releases/download/v1/fixture.bin"
        )

    assert exc_info.value.code == "unsafe_artifact_redirect"
    assert requested == [
        "https://github.com/example/repo/releases/download/v1/fixture.bin"
    ]


@pytest.mark.asyncio
async def test_artifact_download_follows_validated_github_redirect(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://release-assets.githubusercontent.com/fixture.bin"
                },
                request=request,
            )
        return httpx.Response(200, content=b"verified", request=request)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await module._download_artifact(
        "https://github.com/example/repo/releases/download/v1/fixture.bin"
    )

    assert result == b"verified"
    assert requested == [
        "https://github.com/example/repo/releases/download/v1/fixture.bin",
        "https://release-assets.githubusercontent.com/fixture.bin",
    ]


@pytest.mark.asyncio
async def test_verified_artifact_maps_invalid_initial_url():
    descriptor = module.KnowledgeArtifactDescriptor(
        url="https://example.invalid/fixture.neko-knowledge.json",
        sha256="0" * 64,
        bytes=1,
    )

    with pytest.raises(module._KnowledgeTaskError) as exc_info:
        await module._download_verified_artifact(
            descriptor,
            max_bytes=module.MAX_PACK_BYTES,
            required_suffix=".neko-knowledge.json",
        )

    assert exc_info.value.code == "unsafe_artifact_url"


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
            "jobs": [
                {
                    "job_id": "fixture-job",
                    "state": state,
                    "indexed_percent": 50.0 if state == "embedding" else 100.0,
                }
            ],
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(module, "_main_request", fake_main)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    result = await module._wait_for_pack_job(
        task,
        job_id="fixture-job",
    )

    assert calls == 2
    assert task["stage"] == "indexing"
    assert task["progress"] == pytest.approx(0.895)
    assert result["state"] == "active"
