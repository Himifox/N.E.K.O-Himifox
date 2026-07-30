from __future__ import annotations

import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.subscriptions import canonical_pack_bytes


def _entry(title: str, source: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": (), "recognition": ()},
        tags=(source, "type:reference"),
        summary="A compact summary",
        content="Meaning\n- A typical use",
    )


def _client(monkeypatch, tmp_path) -> TestClient:
    import main_routers.public_knowledge_router as module

    monkeypatch.setattr(
        module,
        "get_config_manager",
        lambda: SimpleNamespace(knowledge_dir=tmp_path),
    )
    monkeypatch.setattr(module, "_validate_mutation", lambda *_args, **_kwargs: None)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def test_generic_management_api_lists_multiple_collections(monkeypatch, tmp_path):
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path("meme")).upsert(
        _entry("meme fixture", "source:chime")
    )
    KnowledgeStore(service.database_path("corpora")).upsert(
        _entry("reference fixture", "source:corpora")
    )
    client = _client(monkeypatch, tmp_path)

    collections = client.get("/api/public-knowledge/collections").json()
    listing = client.get(
        "/api/public-knowledge/entries",
        params={"collection": "corpora", "limit": 10},
    ).json()
    detail = client.get(
        "/api/public-knowledge/entry",
        params={
            "collection": "corpora",
            "source": "corpora",
            "title": "reference fixture",
        },
    ).json()

    assert {item["collection_id"] for item in collections["collections"]} == {
        "meme", "corpora",
    }
    assert listing["items"][0]["title"] == "reference fixture"
    assert detail["entry"]["content"] == "Meaning\n- A typical use"


def test_subscription_handoff_verifies_hash_and_installs_pack(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    pack = {
        "schema_version": 1,
        "pack_id": "market-fixture",
        "collection_id": "meme",
        "source": {
            "name": "Market Fixture",
            "homepage": "https://example.invalid/market-fixture",
            "license": "CC0-1.0",
        },
        "entries": [{
            "title": "market phrase",
            "terms": {"alias": [], "recognition": []},
            "tags": ["type:reference"],
            "summary": "Market meaning",
            "content": "Meaning\n- Market use",
        }],
    }
    digest = hashlib.sha256(canonical_pack_bytes(pack)).hexdigest()
    payload = {
        "protocol_version": 1,
        "subscription": {
            "provider": "plugin-market",
            "remote_id": "knowledge/market-fixture",
            "version": "1.0.0",
            "channel": "stable",
            "artifact_sha256": digest,
        },
        "pack": pack,
    }

    response = client.post(
        "/api/public-knowledge/subscriptions/apply",
        json=payload,
    ).json()
    packs = client.get(
        "/api/public-knowledge/packs",
        params={"collection": "meme"},
    ).json()

    assert response["ok"] is True
    assert response["entries"] == 1
    assert packs["packs"][0]["subscription"]["remote_id"] == (
        "knowledge/market-fixture"
    )

    payload["subscription"]["artifact_sha256"] = "0" * 64
    rejected = client.post(
        "/api/public-knowledge/subscriptions/apply",
        json=payload,
    ).json()
    assert rejected["reason"] == "artifact_hash_mismatch"


def test_subscription_size_limit_applies_to_pack_not_small_envelope(
    monkeypatch,
    tmp_path,
):
    import main_routers.public_knowledge_router as module

    client = _client(monkeypatch, tmp_path)
    pack = {
        "schema_version": 1,
        "pack_id": "size-fixture",
        "collection_id": "meme",
        "source": {"name": "Size", "homepage": "", "license": "CC0-1.0"},
        "entries": [{
            "title": "size fixture",
            "terms": {"alias": [], "recognition": []},
            "tags": [],
            "summary": "",
            "content": "content",
        }],
    }
    pack_size = len(canonical_pack_bytes(pack))
    monkeypatch.setattr(module, "MAX_PACK_BYTES", pack_size)
    digest = hashlib.sha256(canonical_pack_bytes(pack)).hexdigest()

    response = client.post(
        "/api/public-knowledge/subscriptions/apply",
        json={
            "protocol_version": 1,
            "subscription": {
                "provider": "plugin-market",
                "remote_id": "knowledge/size-fixture",
                "version": "1.0.0",
                "channel": "stable",
                "artifact_sha256": digest,
            },
            "pack": pack,
        },
    ).json()

    assert response["ok"] is True


def test_unknown_collection_management_requests_do_not_raise(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    listing = client.get(
        "/api/public-knowledge/packs",
        params={"collection": "missing"},
    ).json()
    toggle = client.post(
        "/api/public-knowledge/collection/auto-context",
        json={"collection": "missing", "enabled": True},
    ).json()
    disable = client.post(
        "/api/public-knowledge/entry/disabled",
        json={
            "collection": "missing",
            "source": "fixture",
            "title": "unknown",
            "disabled": True,
        },
    ).json()

    assert listing == {"ok": False, "reason": "unknown_collection"}
    assert toggle == {"ok": False, "reason": "unknown_collection"}
    assert disable == {"ok": False, "reason": "unknown_collection"}
