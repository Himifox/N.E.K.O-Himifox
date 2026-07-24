from __future__ import annotations

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.moegirl_knowledge.retrieval import MatchPolicy
from knowledge.service import (
    MEME_COLLECTION,
    MEME_RESPONSE_POLICY,
    CollectionSpec,
    KnowledgeService,
)


def _entry(title: str, *, source: str, tags=(), content="Meaning\n- Example"):
    return KnowledgeEntry(
        title=title,
        terms={},
        tags=(f"source:{source}", *tags),
        summary="A compact meaning",
        content=content,
    )


def test_stable_api_opens_the_existing_meme_database(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = tmp_path / "moegirl-knowledge" / "knowledge.db"
    KnowledgeStore(database_path).upsert(_entry("known phrase", source="chime"))

    hits = service.search("meme", "known phrase", limit=1)

    assert hits[0].entry.title == "known phrase"
    assert service.database_path("meme") == database_path


def test_strong_match_in_another_collection_wins_over_a_meme_weak_hint(tmp_path):
    reference_spec = CollectionSpec(
        collection_id="reference",
        storage_directory="reference",
        priority=10,
        auto_context_enabled=True,
        match_policy=MatchPolicy(),
        response_policy=MEME_RESPONSE_POLICY,
    )
    service = KnowledgeService(
        tmp_path,
        collections=(MEME_COLLECTION, reference_spec),
    )
    KnowledgeStore(service.database_path("meme")).upsert(_entry(
        "xy",
        source="chime",
        tags=("type:phenomenon",),
    ))
    KnowledgeStore(service.database_path("reference")).upsert(_entry(
        "strongterm",
        source="fixture",
    ))

    context = service.build_turn_context("xy and strongterm appear together")

    assert context.collection_id == "reference"
    assert context.match_mode == "strong"
    assert "Term: strongterm" in context.text


def test_generic_disable_management_removes_an_entry_from_search(tmp_path):
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path("meme")).upsert(_entry(
        "disable me",
        source="fixture",
    ))

    assert service.search("meme", "disable me", limit=1)

    count = service.set_entry_disabled(
        "meme",
        source_tag="source:fixture",
        title="disable me",
        disabled=True,
    )

    assert count == 1
    assert service.search("meme", "disable me", limit=1) == []
