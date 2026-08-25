from __future__ import annotations

import json
import random

import pytest

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.catalog_overrides import (
    CatalogOverrideError,
    get_catalog_override_path,
    load_disabled_entries,
    set_entry_disabled,
)
from knowledge.packs import validate_pack
from knowledge.service import (
    MaterialKnowledgeHit,
    _is_direct_material_match,
    _is_short_query_embedded_in_term,
)
from knowledge.models import KnowledgeHit


def _entry(title: str, source: str, *tags: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": (f"{title} alias",), "recognition": ()},
        tags=(source, *tags),
        summary=f"Meaning of {title}",
        content=f"Reference for {title}",
    )


def test_invalid_catalog_override_fails_closed_and_is_not_overwritten(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(
        _pack(
            pack_id="override-fixture",
            material_type="knowledge",
            title="Disabled fixture",
        )
    )
    override_path = get_catalog_override_path(service.database_path())
    corrupt = b'{"disabled": ['
    override_path.write_bytes(corrupt)

    with pytest.raises(CatalogOverrideError):
        load_disabled_entries(override_path)
    with pytest.raises(CatalogOverrideError):
        service.set_entry_disabled(
            source_tag="source:community.override-fixture",
            title="Disabled fixture",
            disabled=True,
        )

    assert override_path.read_bytes() == corrupt
    assert service.build_turn_context("Disabled fixture").hit_count == 0
    status = service.get_status()
    assert status["catalog_override_state"] == "invalid"
    assert status["integrity_ok"] is False


def test_non_utf8_catalog_override_is_reported_as_invalid(tmp_path):
    service = open_knowledge(tmp_path)
    override_path = get_catalog_override_path(service.database_path())
    override_path.write_bytes(b"\xff\xfe")

    with pytest.raises(CatalogOverrideError):
        load_disabled_entries(override_path)
    assert service.get_status()["catalog_override_state"] == "invalid"


def test_fresh_empty_knowledge_root_is_healthy_without_creating_database(tmp_path):
    service = open_knowledge(tmp_path)

    status = service.get_status()

    assert status["integrity_ok"] is True
    assert status["entries"] == 0
    assert not service.database_path().exists()


def test_corrupt_database_status_is_structured_degraded(tmp_path):
    service = open_knowledge(tmp_path)
    service.database_path().write_bytes(b"not a sqlite database")

    status = service.get_status()

    assert status["integrity_ok"] is False
    assert status["schema_state"] == "invalid_or_unavailable"
    assert status["error_code"] == "knowledge_database_unavailable"
    assert status["entries"] == status["chunks_total"] == 0


def test_sample_entries_draws_from_complete_enabled_tag_population(
    tmp_path,
    monkeypatch,
):
    service = open_knowledge(tmp_path)
    store = KnowledgeStore(service.database_path())
    tag = "dataset:tarot-interpretations"
    store.upsert_many(
        tuple(
            _entry(f"card {index:03d}", "source:corpora", tag)
            for index in range(101)
        )
    )
    monkeypatch.setattr(random, "randrange", lambda _population: 0)

    selected = service.sample_entries(tag, limit=1)

    assert selected[0].title == "card 100"
    service.set_entry_disabled(
        source_tag="source:corpora",
        title="card 100",
        disabled=True,
    )
    assert service.sample_entries(tag, limit=1)[0].title == "card 099"


def _pack(*, pack_id: str, material_type: str, title: str, tags=()):
    return validate_pack(
        {
            "schema_version": 1,
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

    assert service.database_path() == tmp_path / "knowledge.db"
    assert service.search("Knowledge fact", limit=1)[0].entry.title == "Knowledge fact"
    assert service.search("Corpus sample", limit=1)[0].entry.title == "Corpus sample"


def test_builtin_knowledge_auto_injects_but_builtin_corpus_does_not(tmp_path):
    service = open_knowledge(tmp_path)
    store = KnowledgeStore(service.database_path())
    store.upsert(_entry("Exact knowledge", "source:chime"))
    store.upsert(_entry("Exact corpus", "source:corpora"))

    assert service.build_conversation_context("Exact knowledge appears").hit_count == 1
    assert service.build_conversation_context("Exact corpus appears").hit_count == 0


def test_latin_direct_match_uses_latin_boundaries_but_allows_cjk_adjacency():
    java = _entry("Java", "source:chime")

    assert _is_direct_material_match("Java 开发", java)
    assert _is_direct_material_match("Java开发", java)
    assert _is_direct_material_match("学习Ｊａｖａ", java)
    assert not _is_direct_material_match("JavaScript", java)
    assert not _is_direct_material_match("myjava2", java)


def test_latin_direct_match_preserves_meaningful_punctuation_and_short_symbols():
    node = _entry("node.js", "source:chime")
    cpp = _entry("C++", "source:chime")

    assert _is_direct_material_match("学习 node.js。", node)
    assert not _is_direct_material_match("学习 nodejs。", node)
    assert _is_direct_material_match("C++", cpp)
    assert not _is_direct_material_match("c", cpp)
    assert not _is_direct_material_match("C++ 开发", cpp)


def test_corpus_short_query_reuses_latin_boundaries_and_keeps_cjk_substrings():
    javascript = _entry("JavaScript 入门", "source:corpora")
    java_cjk = _entry("Java开发入门", "source:corpora")
    chinese = _entry("现在全网都在刷你急了你急了的梗", "source:corpora")

    assert not _is_short_query_embedded_in_term("java", javascript)
    assert _is_short_query_embedded_in_term("java", java_cjk)
    assert _is_short_query_embedded_in_term("你急了", chinese)


def test_accented_latin_direct_match_uses_casefolded_boundaries():
    cafe = _entry("Café", "source:chime")

    assert _is_direct_material_match("CAFÉ教程", cafe)
    assert not _is_direct_material_match("caféteria", cafe)


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


def test_corpus_pack_can_participate_in_automatic_conversation(tmp_path):
    service = open_knowledge(tmp_path)
    service.install_pack(
        _pack(pack_id="reply-samples", material_type="corpus", title="Reply sample")
    )

    assert service.search("Reply sample", limit=1)
    assert service.list_packs()[0]["auto_context"] is True
    service.set_pack_auto_context("reply-samples", enabled=False)
    assert service.list_packs()[0]["auto_context"] is False


@pytest.mark.asyncio
async def test_automatic_conversation_uses_corpus_without_magic_words(
    monkeypatch,
    tmp_path,
):
    service = open_knowledge(tmp_path)
    entry = _entry("你这瓜保熟吗", "source:corpora")
    entry = KnowledgeEntry(
        title=entry.title,
        terms=entry.terms,
        tags=entry.tags,
        summary="一条回应参考",
        content="保熟，不熟你提着瓜来找我。",
    )
    calls = []

    async def _asearch(*_args, **kwargs):
        calls.append(kwargs)
        return [
            MaterialKnowledgeHit(
                hit=KnowledgeHit(
                    entry=entry,
                    score=1.0,
                    retrieval_modes=("lexical",),
                    lexical_score=1.0,
                ),
                material_type="corpus",
            )
        ]

    monkeypatch.setattr(service, "asearch", _asearch)
    context = await service.abuild_conversation_context("你这瓜保熟吗？")

    assert len(calls) == 1
    assert calls[0]["load_model"] is False
    assert calls[0]["deadline_monotonic"] is None
    assert context.corpus_hits == 1
    assert context.knowledge_hits == 0
    assert "保熟,不熟你提着瓜来找我" in context.text
    assert "Reference material:" in context.text


@pytest.mark.asyncio
async def test_automatic_conversation_rejects_weak_semantic_corpus(
    monkeypatch,
    tmp_path,
):
    service = open_knowledge(tmp_path)
    entry = _entry("无关语料", "source:corpora")

    async def _asearch(*_args, **_kwargs):
        return [
            MaterialKnowledgeHit(
                hit=KnowledgeHit(
                    entry=entry,
                    score=0.69,
                    retrieval_modes=("semantic",),
                    semantic_score=0.69,
                ),
                material_type="corpus",
            )
        ]

    monkeypatch.setattr(service, "asearch", _asearch)
    context = await service.abuild_conversation_context("你好呀")

    assert context.hit_count == 0
    assert context.text == ""


@pytest.mark.asyncio
async def test_short_natural_corpus_phrase_does_not_require_an_intent_command(
    monkeypatch,
    tmp_path,
):
    service = open_knowledge(tmp_path)
    entry = _entry("现在全网都在刷你急了你急了的梗", "source:corpora")

    async def _asearch(*_args, **_kwargs):
        return [
            MaterialKnowledgeHit(
                hit=KnowledgeHit(
                    entry=entry,
                    score=0.62,
                    retrieval_modes=("lexical", "semantic"),
                    lexical_score=3.0,
                    semantic_score=0.62,
                ),
                material_type="corpus",
            )
        ]

    monkeypatch.setattr(service, "asearch", _asearch)
    context = await service.abuild_conversation_context("你急了")

    assert context.corpus_hits == 1
    assert "Conversation trigger:" in context.text


@pytest.mark.asyncio
async def test_automatic_conversation_shares_one_search_for_knowledge_and_corpus(
    monkeypatch,
    tmp_path,
):
    service = open_knowledge(tmp_path)
    knowledge_entry = _entry("周三电池", "source:chime", "domain:meme")
    corpus_entry = _entry("猫猫回应", "source:corpora")
    calls = 0

    async def _asearch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [
            MaterialKnowledgeHit(
                hit=KnowledgeHit(
                    entry=knowledge_entry,
                    score=1.0,
                    retrieval_modes=("lexical",),
                    lexical_score=1.0,
                ),
                material_type="knowledge",
            ),
            MaterialKnowledgeHit(
                hit=KnowledgeHit(
                    entry=corpus_entry,
                    score=0.82,
                    retrieval_modes=("semantic",),
                    semantic_score=0.82,
                ),
                material_type="corpus",
            ),
        ]

    monkeypatch.setattr(service, "asearch", _asearch)
    context = await service.abuild_conversation_context("周三电池是什么意思？")

    assert calls == 1
    assert context.hit_count == 2
    assert context.knowledge_hits == 1
    assert context.corpus_hits == 1
    assert "Knowledge term: 周三电池" in context.text
    assert "Conversation trigger: 猫猫回应" in context.text


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
    assert packs["legacy-corpus"]["auto_context"] is True
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

    assert not (tmp_path / "knowledge.db").exists()


def test_split_layout_discards_corrupt_vectors_without_losing_entries(tmp_path):
    legacy_database = tmp_path / "corpora" / "knowledge.db"
    legacy_store = KnowledgeStore(legacy_database)
    entry = _entry("Recoverable corpus", "source:corpora")
    legacy_store.replace_source(entry.source_tag, (entry,))
    with legacy_store._connection(writable=True) as connection:
        connection.execute(
            "UPDATE knowledge_chunks SET embedding_status='ready', "
            "embedding_model_id='legacy-model', embedding_dimensions=256, embedding=?",
            (b"invalid",),
        )

    service = open_knowledge(tmp_path)
    migrated_store = KnowledgeStore(service.database_path())

    assert migrated_store.get_entry(entry.source_tag, entry.title) is not None
    assert migrated_store.chunk_status()["chunks_ready"] == 0
    assert legacy_database.is_file()


def test_previous_unified_layout_moves_to_flat_knowledge_root(tmp_path):
    old_database = tmp_path / "public-knowledge" / "knowledge.db"
    old_store = KnowledgeStore(old_database)
    old_store.upsert(_entry("Previously unified", "source:chime"))

    service = open_knowledge(tmp_path)

    assert service.database_path() == tmp_path / "knowledge.db"
    assert KnowledgeStore(service.database_path()).get_entry(
        "source:chime",
        "Previously unified",
    ) is not None
    assert old_database.is_file()


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
