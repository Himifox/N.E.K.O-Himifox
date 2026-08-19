from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.chunking import derive_knowledge_chunks
from knowledge.packs import validate_pack
from knowledge.prebuilt_index import (
    PREBUILT_DIMENSIONS,
    PREBUILT_MODEL_ID,
    build_prebuilt_index_artifacts,
)
from knowledge.subscriptions import canonical_pack_bytes


def _entry(
    title: str, source: str, *, content: str = "Meaning\n- A typical use"
) -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": (), "recognition": ()},
        tags=(source, "type:reference"),
        summary="A compact summary",
        content=content,
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


def _prebuilt_artifacts(pack_payload: dict):
    pack_raw = canonical_pack_bytes(pack_payload)
    pack = validate_pack(pack_payload)
    chunks = tuple(
        chunk
        for entry in pack.entries
        for chunk in derive_knowledge_chunks(
            entry,
            entry_key=f"{entry.source_tag}:{entry.title}",
        )
    )
    vector = np.ones(PREBUILT_DIMENSIONS, dtype="<f2").tobytes()
    artifacts = build_prebuilt_index_artifacts(
        pack_raw,
        tuple(
            {
                "chunk_id": chunk.chunk_id,
                "content_hash": chunk.content_hash,
                "model_id": PREBUILT_MODEL_ID,
                "dimensions": PREBUILT_DIMENSIONS,
                "embedding": vector,
            }
            for chunk in chunks
        ),
    )
    return pack_raw, artifacts


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
        "meme",
        "corpora",
    }
    assert listing["total"] == 1
    assert listing["has_more"] is False
    assert listing["items"][0]["title"] == "reference fixture"
    assert detail["entry"]["content"] == "Meaning\n- A typical use"


def test_search_source_filter_is_applied_before_pagination(monkeypatch, tmp_path):
    service = open_knowledge(tmp_path)
    store = KnowledgeStore(service.database_path("meme"))
    store.upsert_many(
        (
            _entry("needle alpha", "source:a"),
            _entry("needle beta", "source:a"),
            _entry("target one", "source:b", content="needle reference one"),
            _entry("target two", "source:b", content="needle reference two"),
        )
    )
    client = _client(monkeypatch, tmp_path)

    first = client.get(
        "/api/public-knowledge/entries",
        params={
            "collection": "meme",
            "query": "needle",
            "source": "b",
            "limit": 1,
            "offset": 0,
        },
    ).json()
    second = client.get(
        "/api/public-knowledge/entries",
        params={
            "collection": "meme",
            "query": "needle",
            "source": "b",
            "limit": 1,
            "offset": 1,
        },
    ).json()
    missing = client.get(
        "/api/public-knowledge/entries",
        params={
            "collection": "meme",
            "query": "needle",
            "source": "missing",
            "limit": 1,
        },
    ).json()
    unfiltered = client.get(
        "/api/public-knowledge/entries",
        params={"collection": "meme", "query": "needle", "limit": 1},
    ).json()

    assert first["items"][0]["source"]["tag"] == "source:b"
    assert first["has_more"] is True
    assert second["items"][0]["source"]["tag"] == "source:b"
    assert second["items"][0]["title"] != first["items"][0]["title"]
    assert second["has_more"] is False
    assert missing["items"] == []
    assert missing["has_more"] is False
    assert unfiltered["items"][0]["source"]["tag"] == "source:a"


def test_management_search_can_show_and_restore_a_disabled_entry(monkeypatch, tmp_path):
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path("meme")).upsert(
        _entry("disabled query fixture", "source:chime")
    )
    service.set_entry_disabled(
        "meme",
        source_tag="source:chime",
        title="disabled query fixture",
        disabled=True,
    )
    client = _client(monkeypatch, tmp_path)

    disabled = client.get(
        "/api/public-knowledge/entries",
        params={"collection": "meme", "query": "disabled query fixture"},
    ).json()
    restored = client.post(
        "/api/public-knowledge/entry/disabled",
        json={
            "collection": "meme",
            "source": "chime",
            "title": "disabled query fixture",
            "disabled": False,
        },
    ).json()
    enabled = client.get(
        "/api/public-knowledge/entries",
        params={"collection": "meme", "query": "disabled query fixture"},
    ).json()

    assert disabled["items"][0]["disabled"] is True
    assert restored["ok"] is True
    assert enabled["items"][0]["disabled"] is False


def test_subscription_handoff_verifies_hash_and_stages_pack(monkeypatch, tmp_path):
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
        "entries": [
            {
                "title": "market phrase",
                "terms": {"alias": [], "recognition": []},
                "tags": ["type:reference"],
                "summary": "Market meaning",
                "content": "Meaning\n- Market use",
            }
        ],
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
    jobs = client.get(
        "/api/public-knowledge/packs/jobs",
        params={"collection": "meme"},
    ).json()

    assert response["ok"] is True
    assert response["entries_total"] == 1
    assert response["entries"] == 1
    assert response["collection"] == "meme"
    assert response["source_tag"] == "source:community.market-fixture"
    assert response["state"] == "queued"
    assert packs["packs"] == []
    assert jobs["jobs"][0]["pack_id"] == "market-fixture"
    subscription = json.loads(
        (tmp_path / ".staging" / response["job_id"] / "subscription.json").read_text(
            encoding="utf-8"
        )
    )
    assert subscription["remote_id"] == ("knowledge/market-fixture")

    payload["subscription"]["artifact_sha256"] = "0" * 64
    rejected = client.post(
        "/api/public-knowledge/subscriptions/apply",
        json=payload,
    ).json()
    assert rejected["reason"] == "artifact_hash_mismatch"


def test_indexed_subscription_stages_complete_prebuilt_sidecars(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    pack = {
        "schema_version": 1,
        "pack_id": "indexed-fixture",
        "collection_id": "meme",
        "source": {
            "name": "Indexed Fixture",
            "homepage": "https://example.invalid/indexed-fixture",
            "license": "CC0-1.0",
        },
        "entries": [
            {
                "title": "indexed phrase",
                "terms": {"alias": [], "recognition": []},
                "tags": ["type:reference"],
                "summary": "Indexed meaning",
                "content": "Meaning\n- Indexed use",
            }
        ],
    }
    pack_raw, artifacts = _prebuilt_artifacts(pack)
    subscription = {
        "provider": "plugin-market",
        "remote_id": "knowledge/indexed-fixture",
        "version": "1.0.0",
        "channel": "stable",
        "artifact_sha256": artifacts.pack_sha256,
        "index_manifest_sha256": artifacts.manifest_sha256,
        "vectors_sha256": artifacts.vectors_sha256,
        "trust": "trusted_market",
    }

    response = client.post(
        "/api/public-knowledge/subscriptions/apply-v2",
        data={
            "protocol_version": "2",
            "subscription": json.dumps(subscription),
        },
        files={
            "pack": (
                "indexed-fixture.neko-knowledge.json",
                pack_raw,
                "application/json",
            ),
            "index_manifest": (
                "indexed-fixture.neko-knowledge.index.json",
                artifacts.manifest,
                "application/json",
            ),
            "vectors": (
                "indexed-fixture.neko-knowledge.vectors.f16",
                artifacts.vectors,
                "application/octet-stream",
            ),
        },
    ).json()

    assert response["ok"] is True
    job_root = tmp_path / ".staging" / response["job_id"]
    assert (
        job_root / "pack.neko-knowledge.index.json"
    ).read_bytes() == artifacts.manifest
    assert (
        job_root / "pack.neko-knowledge.vectors.f16"
    ).read_bytes() == artifacts.vectors


def test_indexed_subscription_rejects_cache_but_keeps_raw_pack(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    pack = {
        "schema_version": 1,
        "pack_id": "fallback-fixture",
        "collection_id": "meme",
        "source": {"name": "Fallback", "homepage": "", "license": "CC0-1.0"},
        "entries": [
            {
                "title": "fallback phrase",
                "terms": {"alias": [], "recognition": []},
                "tags": [],
                "summary": "Fallback meaning",
                "content": "Fallback content",
            }
        ],
    }
    pack_raw, artifacts = _prebuilt_artifacts(pack)
    subscription = {
        "provider": "plugin-market",
        "remote_id": "knowledge/fallback-fixture",
        "version": "1.0.0",
        "channel": "stable",
        "artifact_sha256": artifacts.pack_sha256,
        "index_manifest_sha256": artifacts.manifest_sha256,
        "vectors_sha256": artifacts.vectors_sha256,
        "trust": "trusted_market",
    }

    response = client.post(
        "/api/public-knowledge/subscriptions/apply-v2",
        data={
            "protocol_version": "2",
            "subscription": json.dumps(subscription),
        },
        files={
            "pack": ("fallback.neko-knowledge.json", pack_raw, "application/json"),
            "index_manifest": (
                "fallback.neko-knowledge.index.json",
                artifacts.manifest,
                "application/json",
            ),
            "vectors": (
                "fallback.neko-knowledge.vectors.f16",
                artifacts.vectors[:-2],
                "application/octet-stream",
            ),
        },
    ).json()

    assert response["ok"] is True
    job_root = tmp_path / ".staging" / response["job_id"]
    state = json.loads((job_root / "state.json").read_text(encoding="utf-8"))
    assert (job_root / "pack.neko-knowledge.json").read_bytes() == pack_raw
    assert not (job_root / "pack.neko-knowledge.index.json").exists()
    assert not (job_root / "pack.neko-knowledge.vectors.f16").exists()
    assert state["index_fallback_reason"] == "prebuilt_index_rejected"


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
        "entries": [
            {
                "title": "size fixture",
                "terms": {"alias": [], "recognition": []},
                "tags": [],
                "summary": "",
                "content": "content",
            }
        ],
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


def test_oversized_pack_request_is_rejected_before_json_decode(monkeypatch, tmp_path):
    import main_routers.public_knowledge_router as module

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "MAX_PACK_BYTES", 16)
    monkeypatch.setattr(module, "_PACK_ENVELOPE_OVERHEAD_BYTES", 0)

    response = client.post(
        "/api/public-knowledge/packs/import",
        content=b"{" + b"x" * 64,
        headers={"content-type": "application/json"},
    ).json()

    assert response == {"ok": False, "reason": "pack_too_large"}


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
