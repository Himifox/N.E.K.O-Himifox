from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.source_registry import get_source
from knowledge.packs import (
    MAX_PACK_TAG_BYTES_PER_ENTRY,
    MAX_PACK_TAGS_PER_ENTRY,
    MAX_PACK_TERM_BYTES_PER_ENTRY,
    MAX_PACK_TERMS_PER_ROLE,
    install_pack,
    installed_source_embedding_policies,
    pack_payload,
    validate_pack,
)
from knowledge.subscriptions import (
    canonical_pack_bytes,
    load_canonical_pack_artifact,
    validate_subscription,
)


def _payload(*, title="community phrase", pack_id="community-fixture"):
    return {
        "schema_version": 1,
        "pack_id": pack_id,
        "material_type": "knowledge",
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
    payload["material_type"] = "corpus"
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
    assert service.search("community phrase", limit=1)
    assert service.build_turn_context("community phrase appears here").hit_count == 0

    service.set_pack_auto_context("community-fixture", enabled=True)
    context = service.build_turn_context("community phrase appears here")

    assert context.hit_count == 1
    assert "Source: Community Fixture" in context.text


def test_corpus_pack_enables_automatic_conversation_use_by_default(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_material_payload()))

    installed = service.list_packs()
    explicit = service.sample_entries(
        "dataset:tarot-interpretations",
        limit=1,
    )
    automatic = service.build_conversation_context("please draw a tarot card")

    assert installed[0]["auto_context"] is True
    assert explicit[0].source_tag == "source:community.community-tarot"
    assert automatic.hit_count == 0


def test_legacy_exact_matcher_still_excludes_corpus_material(tmp_path):
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path()).upsert(
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

    assert context.hit_count == 0


def test_corpus_pack_automatic_context_can_be_disabled_and_enabled(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_material_payload()))

    service.set_pack_auto_context("community-tarot", enabled=False)
    assert service.list_packs()[0]["auto_context"] is False
    service.set_pack_auto_context("community-tarot", enabled=True)

    installed = service.list_packs()
    context = service.build_conversation_context("please draw a tarot card")
    assert installed[0]["effective_material_type"] == "corpus"
    assert installed[0]["auto_context"] is True
    assert context.hit_count == 0


def test_v3_registry_enables_existing_corpus_for_conversation(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_material_payload()))
    registry_path = service.database_path().with_name("packs.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["schema_version"] = 3
    registry["packs"]["community-tarot"]["auto_context"] = False
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    installed = service.list_packs()

    assert installed[0]["effective_material_type"] == "corpus"
    assert installed[0]["auto_context"] is True


def test_pack_requires_material_type():
    missing = _payload()
    missing.pop("material_type")
    with pytest.raises(ValueError, match="material_type"):
        validate_pack(missing)

    current = validate_pack(_payload())
    assert current.material_type == "knowledge"
    assert pack_payload(current)["material_type"] == "knowledge"


def test_pack_rejects_pre_release_schema_and_removed_collection_field():
    wrong_version = _payload()
    wrong_version["schema_version"] = 3

    with pytest.raises(ValueError, match="unsupported knowledge pack schema"):
        validate_pack(wrong_version)

    current_with_collection = _payload()
    current_with_collection["collection_id"] = "meme"
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_pack(current_with_collection)


def test_pack_rejects_too_many_terms_before_normalization():
    payload = _payload()
    payload["entries"][0]["terms"]["alias"] = [
        f"alias-{index}" for index in range(MAX_PACK_TERMS_PER_ROLE + 1)
    ]

    with pytest.raises(ValueError, match="contains too many terms"):
        validate_pack(payload)


def test_pack_rejects_oversized_term_metadata_by_utf8_bytes():
    payload = _payload()
    payload["entries"][0]["terms"]["alias"] = [
        "猫" * (MAX_PACK_TERM_BYTES_PER_ENTRY // 3 + 1)
    ]

    with pytest.raises(ValueError, match="metadata size limit"):
        validate_pack(payload)


def test_pack_accepts_the_term_cardinality_boundary():
    payload = _payload()
    payload["entries"][0]["terms"]["alias"] = [
        f"alias-{index}" for index in range(MAX_PACK_TERMS_PER_ROLE)
    ]

    pack = validate_pack(payload)

    assert len(pack.entries[0].aliases) == MAX_PACK_TERMS_PER_ROLE


@pytest.mark.parametrize(
    "terms",
    (
        None,
        {},
        {"alias": ["alias"]},
        {"recognition": ["recognition"]},
    ),
)
def test_pack_normalizes_missing_term_roles_to_empty_lists(terms):
    payload = _payload()
    if terms is None:
        payload["entries"][0].pop("terms")
    else:
        payload["entries"][0]["terms"] = terms

    entry = validate_pack(payload).entries[0]

    assert entry.terms == {
        "alias": tuple((terms or {}).get("alias", [])),
        "recognition": tuple((terms or {}).get("recognition", [])),
    }


@pytest.mark.parametrize("terms", ({"alias": None}, {"recognition": "term"}))
def test_pack_rejects_explicit_invalid_term_role_types(terms):
    payload = _payload()
    payload["entries"][0]["terms"] = terms

    with pytest.raises(ValueError, match="must be a string array"):
        validate_pack(payload)


def test_pack_rejects_too_many_tags_before_normalization():
    payload = _payload()
    payload["entries"][0]["tags"] = [
        f"topic:tag-{index}" for index in range(MAX_PACK_TAGS_PER_ENTRY + 1)
    ]

    with pytest.raises(ValueError, match="contains too many tags"):
        validate_pack(payload)


def test_pack_rejects_oversized_tag_metadata_by_utf8_bytes():
    payload = _payload()
    payload["entries"][0]["tags"] = [
        "topic:" + "猫" * (MAX_PACK_TAG_BYTES_PER_ENTRY // 3)
    ]

    with pytest.raises(ValueError, match="metadata size limit"):
        validate_pack(payload)


def test_pack_rejects_obviously_oversized_tag_before_encoding():
    class OversizedTag(str):
        def encode(self, *_args, **_kwargs):
            pytest.fail("an obviously oversized tag must not be encoded")

    payload = _payload()
    payload["entries"][0]["tags"] = [
        OversizedTag("x" * (MAX_PACK_TAG_BYTES_PER_ENTRY + 1))
    ]

    with pytest.raises(ValueError, match="metadata size limit"):
        validate_pack(payload)


@pytest.mark.parametrize("field", ("terms", "tags"))
def test_pack_rejects_invalid_utf8_metadata_within_budget(field):
    payload = _payload()
    if field == "terms":
        payload["entries"][0]["terms"]["alias"] = ["\ud800"]
    else:
        payload["entries"][0]["tags"] = ["\ud800"]

    with pytest.raises(ValueError, match="valid UTF-8"):
        validate_pack(payload)


def test_pack_accepts_exact_utf8_metadata_byte_boundaries():
    payload = _payload()
    payload["entries"][0]["terms"]["alias"] = [
        "x" * MAX_PACK_TERM_BYTES_PER_ENTRY
    ]
    payload["entries"][0]["tags"] = ["x" * MAX_PACK_TAG_BYTES_PER_ENTRY]

    pack = validate_pack(payload)

    assert pack.entries[0].aliases
    assert pack.entries[0].tags[1]


def test_pack_accepts_the_tag_cardinality_boundary():
    payload = _payload()
    payload["entries"][0]["tags"] = [
        f"topic:tag-{index}" for index in range(MAX_PACK_TAGS_PER_ENTRY)
    ]

    pack = validate_pack(payload)

    assert len(pack.entries[0].tags) == MAX_PACK_TAGS_PER_ENTRY + 1


def test_material_type_override_changes_routing_without_rewriting_entries(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path()
    store = KnowledgeStore(database_path)
    before = store.count_by_source_tag("source:community.community-fixture")

    service.set_pack_material_type_override("community-fixture", material_type="corpus")

    installed = service.list_packs()
    status = service.get_status()
    assert installed[0]["declared_material_type"] == "knowledge"
    assert installed[0]["effective_material_type"] == "corpus"
    assert store.count_by_source_tag("source:community.community-fixture") == before
    assert status["knowledge_entries"] == 0
    assert status["corpus_entries"] == 1
    assert service.build_turn_context("community phrase appears here").hit_count == 0


def test_pack_update_replaces_only_its_own_source(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = service.database_path()
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

    assert service.search("old title", limit=1) == []
    assert service.search("new title", limit=1)
    assert service.search("built in entry", limit=1)


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
    installed = {pack["pack_id"]: pack for pack in service.list_packs()}
    assert set(installed) == {"concurrent-alpha", "concurrent-beta"}
    store = KnowledgeStore(service.database_path())
    assert store.count_by_source_tag("source:community.concurrent-alpha") == 1
    assert store.count_by_source_tag("source:community.concurrent-beta") == 1
    registry = json.loads(
        service.database_path().with_name("packs.json").read_text(encoding="utf-8")
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

    installed = service.list_packs()
    assert len(installed) == 1
    assert installed[0]["pack_id"] == "community-fixture"
    entries = tuple(
        entry
        for entry in KnowledgeStore(service.database_path()).list_active_entries()
        if entry.source_tag == "source:community.community-fixture"
    )
    assert len(entries) == 1
    assert entries[0].title in {"replacement alpha", "replacement beta"}


def test_pack_source_metadata_is_stored_outside_entries(tmp_path):
    service = open_knowledge(tmp_path)
    service.import_pack(_write_pack(tmp_path / "pack.json", _payload()))
    entry = service.search("community phrase", limit=1)[0].entry

    assert set(entry.__dataclass_fields__) == {
        "title",
        "terms",
        "tags",
        "summary",
        "content",
    }
    source = get_source(entry.source_tag, database_path=service.database_path())
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


def test_remove_failure_restores_entries_policy_and_vectors(monkeypatch, tmp_path):
    import knowledge.packs as packs

    service = open_knowledge(tmp_path)
    pack = validate_pack(_payload())
    service.install_pack(pack)
    store = KnowledgeStore(service.database_path())
    with store._connection() as connection:
        chunk = connection.execute(
            "SELECT chunk_id, content_hash FROM knowledge_chunks"
        ).fetchone()
    vector = b"\x00\x3c" * 256
    store.store_chunk_embeddings_strict(
        (
            {
                "chunk_id": str(chunk["chunk_id"]),
                "content_hash": str(chunk["content_hash"]),
                "model_id": "local-text-retrieval-v1-256d-int8-mlen1024",
                "dimensions": 256,
                "embedding": vector,
            },
        )
    )
    before_embeddings = store.ready_embedding_records(source_tag=pack.source_tag)
    monkeypatch.setattr(
        packs,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture failure")),
    )

    with pytest.raises(OSError, match="fixture failure"):
        service.remove_pack(pack.pack_id)

    assert store.get_entry(pack.source_tag, "community phrase") is not None
    assert store.embedding_policy_counts(source_tag=pack.source_tag) == {
        "local": 0,
        "prebuilt_only": 1,
    }
    assert store.ready_embedding_records(source_tag=pack.source_tag) == before_embeddings


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
            "material_type": "knowledge",
            "index_manifest_sha256": "",
            "vectors_sha256": "",
            "trust": "trusted_market",
        }
    )

    service.install_pack(pack, subscription=subscription.to_dict())

    installed = service.list_packs()
    assert installed[0]["subscription"] == subscription.to_dict()
    entry = service.get_entry(
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


def test_subscription_requires_supported_material_type():
    payload = {
        "provider": "plugin-market",
        "provider_package_id": "7",
        "remote_id": "knowledge/community-fixture",
        "version": "1.2.3",
        "channel": "stable",
        "artifact_sha256": "a" * 64,
        "material_type": "corpus",
        "index_manifest_sha256": "",
        "vectors_sha256": "",
        "trust": "trusted_market",
    }

    subscription = validate_subscription(payload)
    assert subscription.material_type == "corpus"
    assert subscription.provider_package_id == "7"
    with pytest.raises(ValueError, match="material_type"):
        validate_subscription({**payload, "material_type": "meme"})
    with pytest.raises(ValueError, match="provider_package_id"):
        validate_subscription({**payload, "provider_package_id": "07"})
    for invalid_package_id in ("７", "٧", "1" * 20):
        with pytest.raises(ValueError, match="provider_package_id"):
            validate_subscription(
                {**payload, "provider_package_id": invalid_package_id}
            )


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
        "provider_package_id": "7",
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
        service.install_pack(
            pack,
            subscription={**subscription, "provider_package_id": "8"},
        )
    with pytest.raises(ValueError, match="identity"):
        service.install_pack(pack)


def test_removing_pack_does_not_remove_another_source(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = service.database_path()
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

    removed = service.remove_pack("community-fixture")

    assert removed == 1
    assert service.search("community phrase", limit=1) == []
    assert service.search("built in entry", limit=1)
    assert service.list_packs() == ()


def test_invalid_pack_registry_degrades_health_instead_of_looking_empty(tmp_path):
    service = open_knowledge(tmp_path)
    service.database_path().with_name("packs.json").write_text(
        "not-json",
        encoding="utf-8",
    )

    status = service.get_status()

    assert status["pack_registry_state"] == "invalid"
    assert status["integrity_ok"] is False
    assert status["packs"] == 0


def test_community_pack_requires_explicit_local_embedding_consent(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path()
    store = KnowledgeStore(database_path)

    installed = service.list_packs()[0]
    assert installed["local_embedding_enabled"] is False
    assert store.embedding_policy_counts(source_tag=installed["source_tag"]) == {
        "local": 0,
        "prebuilt_only": 1,
    }

    service.set_pack_index_policy(
        "community-fixture",
        local_embedding_enabled=True,
    )
    assert service.list_packs()[0]["local_embedding_enabled"] is True
    assert (
        store.embedding_policy_counts(source_tag=installed["source_tag"])["local"] == 1
    )


def test_index_policy_registry_failure_restores_previous_policy(monkeypatch, tmp_path):
    import knowledge.packs as packs

    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path()
    source_tag = "source:community.community-fixture"
    monkeypatch.setattr(
        packs,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture failure")),
    )

    with pytest.raises(OSError, match="fixture failure"):
        service.set_pack_index_policy(
            "community-fixture",
            local_embedding_enabled=True,
        )

    assert KnowledgeStore(database_path).embedding_policy_counts(
        source_tag=source_tag
    ) == {"local": 0, "prebuilt_only": 1}
    assert service.list_packs()[0]["local_embedding_enabled"] is False


def test_legacy_policy_migration_rolls_back_when_registry_write_fails(
    monkeypatch, tmp_path
):
    import knowledge.packs as packs

    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path()
    registry_path = database_path.with_name("packs.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    metadata = registry["packs"]["community-fixture"]
    metadata.pop("local_embedding_enabled")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    source_tag = "source:community.community-fixture"
    store = KnowledgeStore(database_path)
    store.set_source_embedding_policy(source_tag, "local")
    monkeypatch.setattr(
        packs,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture failure")),
    )

    with pytest.raises(OSError, match="fixture failure"):
        packs.migrate_legacy_pack_index_policies(database_path)

    assert store.embedding_policy_counts(source_tag=source_tag) == {
        "local": 1,
        "prebuilt_only": 0,
    }
    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "local_embedding_enabled" not in persisted["packs"]["community-fixture"]


def test_community_chunk_backfill_preserves_explicit_embedding_policy(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path()
    store = KnowledgeStore(database_path)
    source_tag = "source:community.community-fixture"
    with store._connection(writable=True) as connection:
        connection.execute(
            "DELETE FROM knowledge_chunks WHERE entry_rowid IN ("
            "SELECT entries.rowid FROM entries WHERE EXISTS ("
            "SELECT 1 FROM json_each(entries.tags) tag WHERE tag.value=?))",
            (source_tag,),
        )

    assert store.backfill_missing_chunks(
        limit=1,
        embedding_policy_by_source=installed_source_embedding_policies(database_path),
    ) == 1
    assert store.embedding_policy_counts(source_tag=source_tag) == {
        "local": 0,
        "prebuilt_only": 1,
    }


def test_legacy_community_vectors_are_preserved_but_not_rebuilt_automatically(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(validate_pack(_payload()))
    database_path = service.database_path()
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

    migrated = open_knowledge(tmp_path).list_packs()[0]

    assert migrated["local_embedding_enabled"] is False
    assert migrated["index_origin"] == "none"
    assert (
        KnowledgeStore(database_path).embedding_policy_counts(
            source_tag=migrated["source_tag"]
        )["prebuilt_only"]
        == 1
    )
