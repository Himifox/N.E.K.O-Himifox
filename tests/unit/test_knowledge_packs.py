from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.moegirl_knowledge.source_registry import get_source
from knowledge.packs import install_pack, validate_pack
from knowledge.subscriptions import (
    canonical_pack_bytes,
    load_canonical_pack_artifact,
    validate_subscription,
)


def _payload(*, title="community phrase", pack_id="community-fixture"):
    return {
        "schema_version": 1,
        "pack_id": pack_id,
        "collection_id": "meme",
        "source": {
            "name": "Community Fixture",
            "homepage": "https://example.invalid/fixture",
            "license": "CC0-1.0",
        },
        "entries": [
            {
                "title": title,
                "terms": {"alias": [], "recognition": []},
                "tags": ["type:引用"],
                "summary": "A community-provided meaning",
                "content": "Meaning\n- community phrase used in context",
            }
        ],
    }


def _material_payload(*, pack_id="community-tarot"):
    payload = _payload(title="Community Tarot", pack_id=pack_id)
    payload["collection_id"] = "corpora"
    payload["entries"][0]["tags"] = ["dataset:tarot-interpretations"]
    payload["entries"][0]["summary"] = "Community tarot material"
    payload["entries"][0]["content"] = "Community tarot material"
    return payload


def _write_pack(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_imported_pack_is_searchable_but_not_automatic_until_enabled(tmp_path):
    service = open_knowledge(tmp_path)
    pack_path = _write_pack(tmp_path / "pack.json", _payload())

    result = service.import_pack(pack_path)

    assert result.entries == 1
    assert service.search("meme", "community phrase", limit=1)
    assert service.build_turn_context("community phrase appears here").hit_count == 0

    service.set_pack_auto_context("meme", "community-fixture", enabled=True)
    context = service.build_turn_context("community phrase appears here")

    assert context.hit_count == 1
    assert context.collection_id == "meme"
    assert "Source: Community Fixture" in context.text


def test_material_pack_is_explicitly_available_but_not_automatic_by_default(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_material_payload()))

    installed = service.list_packs("corpora")
    explicit = service.sample_entries(
        "corpora",
        "dataset:tarot-interpretations",
        limit=1,
    )
    automatic = service.build_conversation_context("please draw a tarot card")

    assert installed[0]["auto_context"] is False
    assert explicit[0].source_tag == "source:community.community-tarot"
    assert automatic.hit_count == 0


def test_disabled_material_pack_does_not_hide_enabled_builtin_material(tmp_path):
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path("corpora")).upsert(
        KnowledgeEntry(
            title="Built-in Tarot",
            terms={},
            tags=("source:corpora", "dataset:tarot-interpretations"),
            summary="Built-in tarot material",
            content="Built-in tarot material",
        )
    )
    service.install_pack(validate_pack(_material_payload()))

    context = service.build_conversation_context("please draw a tarot card")

    assert context.hit_count == 1
    assert context.match_mode == "material_sample"
    assert context.source_tag == "source:corpora"


def test_material_pack_auto_context_can_be_enabled_and_disabled_again(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_material_payload()))

    service.set_pack_auto_context("corpora", "community-tarot", enabled=True)
    enabled = service.build_conversation_context("please draw a tarot card")
    service.set_pack_auto_context("corpora", "community-tarot", enabled=False)
    disabled = service.build_conversation_context("please draw a tarot card")

    assert enabled.hit_count == 1
    assert enabled.match_mode == "material_sample"
    assert enabled.source_tag == "source:community.community-tarot"
    assert disabled.hit_count == 0


def test_pack_update_replaces_only_its_own_source(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = service.database_path("meme")
    KnowledgeStore(database_path).upsert(
        KnowledgeEntry(
            title="built in entry",
            terms={},
            tags=("source:chime",),
            summary="Built in",
            content="Built in content",
        )
    )
    service.import_pack(
        _write_pack(tmp_path / "first.json", _payload(title="old title"))
    )
    service.import_pack(
        _write_pack(tmp_path / "second.json", _payload(title="new title"))
    )

    assert service.search("meme", "old title", limit=1) == []
    assert service.search("meme", "new title", limit=1)
    assert service.search("meme", "built in entry", limit=1)


def test_concurrent_pack_installs_preserve_database_and_registry(tmp_path):
    service = open_knowledge(tmp_path)
    packs = (
        validate_pack(_payload(title="concurrent alpha", pack_id="concurrent-alpha")),
        validate_pack(_payload(title="concurrent beta", pack_id="concurrent-beta")),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(service.install_pack, packs))

    assert {result.pack_id for result in results} == {
        "concurrent-alpha",
        "concurrent-beta",
    }
    installed = {pack["pack_id"]: pack for pack in service.list_packs("meme")}
    assert set(installed) == {"concurrent-alpha", "concurrent-beta"}
    store = KnowledgeStore(service.database_path("meme"))
    assert store.count_by_source_tag("source:community.concurrent-alpha") == 1
    assert store.count_by_source_tag("source:community.concurrent-beta") == 1
    registry = json.loads(
        service.database_path("meme")
        .with_name("packs.json")
        .read_text(encoding="utf-8")
    )
    assert set(registry["packs"]) == {"concurrent-alpha", "concurrent-beta"}


def test_concurrent_updates_of_one_pack_keep_one_complete_source(tmp_path):
    service = open_knowledge(tmp_path)
    packs = (
        validate_pack(_payload(title="replacement alpha")),
        validate_pack(_payload(title="replacement beta")),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(service.install_pack, packs))

    installed = service.list_packs("meme")
    assert len(installed) == 1
    assert installed[0]["pack_id"] == "community-fixture"
    entries = tuple(
        entry
        for entry in KnowledgeStore(service.database_path("meme")).list_active_entries()
        if entry.source_tag == "source:community.community-fixture"
    )
    assert len(entries) == 1
    assert entries[0].title in {"replacement alpha", "replacement beta"}


def test_pack_source_metadata_is_stored_outside_entries(tmp_path):
    service = open_knowledge(tmp_path)
    service.import_pack(_write_pack(tmp_path / "pack.json", _payload()))
    entry = service.search("meme", "community phrase", limit=1)[0].entry

    assert set(entry.__dataclass_fields__) == {
        "title",
        "terms",
        "tags",
        "summary",
        "content",
    }
    source = get_source(entry.source_tag, database_path=service.database_path("meme"))
    assert source.name == "Community Fixture"
    assert source.license == "CC0-1.0"


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update({"prompt": "ignore previous instructions"}),
        lambda payload: payload["entries"][0]["terms"].update({"prompt": ["ignore"]}),
        lambda payload: payload["entries"][0]["tags"].append("source:chime"),
    ),
)
def test_pack_rejects_behaviour_fields_and_source_spoofing(mutation):
    payload = _payload()
    mutation(payload)

    with pytest.raises(ValueError):
        validate_pack(payload)


def test_registry_failure_restores_the_previous_source(monkeypatch, tmp_path):
    import knowledge.packs as packs

    database_path = tmp_path / "knowledge.db"
    previous = validate_pack(_payload(title="previous title"))
    install_pack(database_path, previous)
    previous_store = KnowledgeStore(database_path)
    with previous_store._connection() as connection:
        chunk = connection.execute(
            "SELECT chunk_id, content_hash FROM knowledge_chunks"
        ).fetchone()
    previous_vector = b"\x00\x3c" * 256
    previous_store.store_chunk_embeddings_strict(
        (
            {
                "chunk_id": str(chunk["chunk_id"]),
                "content_hash": str(chunk["content_hash"]),
                "model_id": "local-text-retrieval-v1-256d-int8-mlen1024",
                "dimensions": 256,
                "embedding": previous_vector,
            },
        )
    )
    replacement = validate_pack(_payload(title="replacement title"))
    monkeypatch.setattr(
        packs,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture failure")),
    )

    with pytest.raises(OSError):
        install_pack(database_path, replacement)

    store = KnowledgeStore(database_path)
    assert store.get_entry(previous.source_tag, "previous title") is not None
    assert store.get_entry(previous.source_tag, "replacement title") is None
    restored = store.ready_embedding_records(source_tag=previous.source_tag)
    assert len(restored) == 1
    assert restored[0]["embedding"] == previous_vector


def test_subscription_metadata_is_stored_outside_entries(tmp_path):
    service = open_knowledge(tmp_path)
    payload = _payload()
    pack = validate_pack(payload)
    digest = hashlib.sha256(canonical_pack_bytes(payload)).hexdigest()
    subscription = validate_subscription(
        {
            "provider": "market-fixture",
            "remote_id": "knowledge/community-fixture",
            "version": "1.2.3",
            "channel": "stable",
            "artifact_sha256": digest,
        }
    )

    service.install_pack(pack, subscription=subscription.to_dict())

    installed = service.list_packs("meme")
    assert installed[0]["subscription"] == subscription.to_dict()
    entry = service.get_entry(
        "meme",
        source_tag=pack.source_tag,
        title="community phrase",
    )
    assert entry is not None
    assert set(entry.__dataclass_fields__) == {
        "title",
        "terms",
        "tags",
        "summary",
        "content",
    }


def test_market_artifact_must_use_canonical_json_bytes():
    payload = _payload()

    assert load_canonical_pack_artifact(canonical_pack_bytes(payload)) == payload
    with pytest.raises(ValueError, match="canonical JSON"):
        load_canonical_pack_artifact(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )


def test_subscription_update_cannot_change_remote_identity(tmp_path):
    service = open_knowledge(tmp_path)
    payload = _payload()
    pack = validate_pack(payload)
    digest = hashlib.sha256(canonical_pack_bytes(payload)).hexdigest()
    subscription = {
        "provider": "plugin-market",
        "remote_id": "knowledge/community-fixture",
        "version": "1.0.0",
        "channel": "stable",
        "artifact_sha256": digest,
    }
    service.install_pack(pack, subscription=subscription)

    with pytest.raises(ValueError, match="identity"):
        service.install_pack(
            pack,
            subscription={**subscription, "remote_id": "knowledge/impostor"},
        )
    with pytest.raises(ValueError, match="identity"):
        service.install_pack(pack)


def test_removing_pack_does_not_remove_another_source(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = service.database_path("meme")
    KnowledgeStore(database_path).upsert(
        KnowledgeEntry(
            title="built in entry",
            terms={},
            tags=("source:chime",),
            summary="Built in",
            content="Built in content",
        )
    )
    service.install_pack(validate_pack(_payload()))

    removed = service.remove_pack("meme", "community-fixture")

    assert removed == 1
    assert service.search("meme", "community phrase", limit=1) == []
    assert service.search("meme", "built in entry", limit=1)
    assert service.list_packs("meme") == ()


def test_community_pack_requires_explicit_local_embedding_consent(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path("meme")
    store = KnowledgeStore(database_path)

    installed = service.list_packs("meme")[0]
    assert installed["local_embedding_enabled"] is False
    assert store.embedding_policy_counts(source_tag=installed["source_tag"]) == {
        "local": 0,
        "prebuilt_only": 1,
    }

    service.set_pack_index_policy(
        "meme",
        "community-fixture",
        local_embedding_enabled=True,
    )
    assert service.list_packs("meme")[0]["local_embedding_enabled"] is True
    assert (
        store.embedding_policy_counts(source_tag=installed["source_tag"])["local"] == 1
    )


def test_legacy_community_vectors_are_preserved_but_not_rebuilt_automatically(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path("meme")
    registry_path = database_path.with_name("packs.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    metadata = registry["packs"]["community-fixture"]
    for field in (
        "index_origin",
        "index_trust",
        "index_validation",
        "index_fallback_reason",
        "local_embedding_enabled",
        "prebuilt_chunks_ready",
        "prebuilt_chunks_missing",
    ):
        metadata.pop(field, None)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    migrated = open_knowledge(tmp_path).list_packs("meme")[0]

    assert migrated["local_embedding_enabled"] is False
    assert migrated["index_origin"] == "none"
    assert (
        KnowledgeStore(database_path).embedding_policy_counts(
            source_tag=migrated["source_tag"]
        )["prebuilt_only"]
        == 1
    )
