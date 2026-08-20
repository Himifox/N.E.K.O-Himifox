from __future__ import annotations

import json

import pytest

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.moegirl_knowledge.catalog_overrides import (
    get_catalog_override_path,
    load_disabled_entries,
    set_entry_disabled,
)
from knowledge.packs import validate_pack


def _entry(title: str, source: str, *tags: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": (f"{title} alias",), "recognition": ()},
        tags=(source, *tags),
        summary=f"Meaning of {title}",
        content=f"Reference for {title}",
    )


def _pack(*, pack_id: str, material_type: str, title: str, tags=()):
    return validate_pack(
        {
            "schema_version": 3,
            "pack_id": pack_id,
            "material_type": material_type,
            "source": {"name": pack_id, "homepage": "", "license": "CC0"},
            "entries": [
                {
                    "title": title,
                    "terms": {"alias": [f"{title} alias"], "recognition": []},
                    "tags": list(tags),
                    "summary": f"Meaning of {title}",
                    "content": f"Reference for {title}",
                }
            ],
        }
    )


def test_service_uses_one_database_and_searches_all_material_types(tmp_path):
    service = open_knowledge(tmp_path)
    store = KnowledgeStore(service.database_path())
    store.upsert(_entry("Knowledge fact", "source:chime"))
    store.upsert(_entry("Corpus sample", "source:corpora"))

    assert service.database_path() == tmp_path / "public-knowledge" / "knowledge.db"
    assert service.search("Knowledge fact", limit=1)[0].entry.title == "Knowledge fact"
    assert service.search("Corpus sample", limit=1)[0].entry.title == "Corpus sample"


def test_builtin_knowledge_auto_injects_but_builtin_corpus_does_not(tmp_path):
    service = open_knowledge(tmp_path)
    store = KnowledgeStore(service.database_path())
    store.upsert(_entry("Exact knowledge", "source:chime"))
    store.upsert(_entry("Exact corpus", "source:corpora"))

    assert service.build_conversation_context("Exact knowledge appears").hit_count == 1
    assert service.build_conversation_context("Exact corpus appears").hit_count == 0


def test_meme_domain_tag_changes_style_not_routing_permission(tmp_path):
    service = open_knowledge(tmp_path)
    pack = _pack(
        pack_id="meme-domain",
        material_type="knowledge",
        title="Tagged phrase",
        tags=("domain:meme", "type:引用"),
    )
    service.install_pack(pack)

    assert service.build_conversation_context("Tagged phrase").hit_count == 0
    service.set_pack_auto_context("meme-domain", enabled=True)
    context = service.build_conversation_context("Tagged phrase")

    assert context.hit_count == 1
    assert "Knowledge type: 引用" in context.text


def test_corpus_pack_is_explicit_only(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(
        _pack(pack_id="reply-samples", material_type="corpus", title="Reply sample")
    )

    assert service.search("Reply sample", limit=1)
    assert service.build_conversation_context("Reply sample").hit_count == 0
    with pytest.raises(ValueError, match="corpus packs cannot enable"):
        service.set_pack_auto_context("reply-samples", enabled=True)


def test_material_type_override_rebuilds_auto_route_without_rewriting_entry(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(
        _pack(pack_id="switchable", material_type="knowledge", title="Switch phrase")
    )
    service.set_pack_auto_context("switchable", enabled=True)
    assert service.build_conversation_context("Switch phrase").hit_count == 1

    service.set_pack_material_type_override("switchable", material_type="corpus")

    assert service.search("Switch phrase", limit=1)
    assert service.build_conversation_context("Switch phrase").hit_count == 0
    assert service.list_packs()[0]["effective_material_type"] == "corpus"


def test_split_layout_migrates_entries_vectors_packs_and_overrides(tmp_path):
    meme_database = tmp_path / "moegirl-knowledge" / "knowledge.db"
    corpora_database = tmp_path / "corpora" / "knowledge.db"
    meme_store = KnowledgeStore(meme_database)
    corpus_store = KnowledgeStore(corpora_database)
    meme_entry = _entry("Legacy meme", "source:community.legacy-meme", "domain:meme")
    corpus_entry = _entry("Legacy corpus", "source:community.legacy-corpus")
    meme_store.replace_source(meme_entry.source_tag, (meme_entry,))
    corpus_store.replace_source(
        corpus_entry.source_tag,
        (corpus_entry,),
        embedding_policy="prebuilt_only",
    )
    chunk = corpus_store.pending_embedding_chunks(
        limit=1,
        model_id="local-text-retrieval-v1-256d-int8-mlen1024",
        embedding_policy="prebuilt_only",
    )[0]
    corpus_store.store_chunk_embeddings_strict(
        (
            {
                "chunk_id": chunk["chunk_id"],
                "content_hash": chunk["content_hash"],
                "model_id": "local-text-retrieval-v1-256d-int8-mlen1024",
                "dimensions": 256,
                "embedding": b"\x00\x3c" * 256,
            },
        )
    )
    for database, pack_id, source_tag, material_type in (
        (meme_database, "legacy-meme", meme_entry.source_tag, "knowledge"),
        (corpora_database, "legacy-corpus", corpus_entry.source_tag, "corpus"),
    ):
        database.with_name("packs.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "packs": {
                        pack_id: {
                            "collection_id": "meme"
                            if material_type == "knowledge"
                            else "corpora",
                            "source_tag": source_tag,
                            "declared_material_type": material_type,
                            "effective_material_type": material_type,
                            "auto_context": material_type == "knowledge",
                            "local_embedding_enabled": material_type == "knowledge",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
    set_entry_disabled(
        get_catalog_override_path(corpora_database),
        source_tag=corpus_entry.source_tag,
        title=corpus_entry.title,
        disabled=True,
    )

    service = open_knowledge(tmp_path)
    migrated_store = KnowledgeStore(service.database_path())

    assert {entry.title for entry in migrated_store.list_active_entries()} == {
        "Legacy meme",
        "Legacy corpus",
    }
    assert migrated_store.chunk_status()["chunks_ready"] == 1
    assert meme_database.is_file()
    assert corpora_database.is_file()
    packs = {pack["pack_id"]: pack for pack in service.list_packs()}
    assert set(packs) == {"legacy-meme", "legacy-corpus"}
    assert packs["legacy-corpus"]["effective_material_type"] == "corpus"
    assert "collection_id" not in packs["legacy-corpus"]
    assert (
        corpus_entry.source_tag,
        corpus_entry.title,
    ) in load_disabled_entries(get_catalog_override_path(service.database_path()))


def test_split_layout_conflict_does_not_publish_partial_database(tmp_path):
    meme_database = tmp_path / "moegirl-knowledge" / "knowledge.db"
    corpora_database = tmp_path / "corpora" / "knowledge.db"
    KnowledgeStore(meme_database).upsert(_entry("Collision", "source:community.same"))
    KnowledgeStore(corpora_database).upsert(
        KnowledgeEntry(
            title="Collision",
            terms={"alias": (), "recognition": ()},
            tags=("source:community.same",),
            summary="Different",
            content="Different content",
        )
    )

    with pytest.raises(ValueError, match="conflicting source/title"):
        open_knowledge(tmp_path)

    assert not (tmp_path / "public-knowledge" / "knowledge.db").exists()


def test_split_layout_keeps_later_same_database_title_conflict(tmp_path):
    database = tmp_path / "moegirl-knowledge" / "knowledge.db"
    store = KnowledgeStore(database)
    earlier = _entry("Collision", "source:chime")
    later = KnowledgeEntry(
        title="Collision",
        terms=earlier.terms,
        tags=earlier.tags,
        summary="Newer summary",
        content="Newer content",
    )
    store.upsert(earlier)
    with store._connection(writable=True) as connection:
        connection.execute(
            "INSERT INTO entries(title, terms, tags, summary, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                later.title,
                json.dumps({key: list(value) for key, value in later.terms.items()}),
                json.dumps(list(later.tags)),
                later.summary,
                later.content,
            ),
        )

    service = open_knowledge(tmp_path)
    migrated = KnowledgeStore(service.database_path()).get_entry(
        later.source_tag,
        later.title,
    )

    assert migrated is not None
    assert migrated.summary == "Newer summary"
    assert migrated.content == "Newer content"
