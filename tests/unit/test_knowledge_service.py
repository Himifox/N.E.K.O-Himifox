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


def test_management_page_can_restore_disabled_without_changing_public_search(tmp_path):
    service = open_knowledge(tmp_path)
    store = KnowledgeStore(service.database_path("meme"))
    store.upsert(_entry("disabled management phrase", source="chime"))
    store.upsert(_entry("disabled management phrase other", source="other"))
    service.set_entry_disabled(
        "meme",
        source_tag="source:chime",
        title="disabled management phrase",
        disabled=True,
    )

    assert all(
        hit.entry.title != "disabled management phrase"
        for hit in service.search(
            "meme",
            "disabled management phrase",
            limit=10,
        )
    )
    assert service.search_page(
        "meme",
        "disabled management phrase",
        source_tag="source:chime",
        limit=10,
    ) == ()
    visible = service.search_page(
        "meme",
        "disabled management phrase",
        source_tag="source:chime",
        limit=10,
        include_disabled=True,
    )
    assert [hit.entry.title for hit in visible] == ["disabled management phrase"]
    assert service.build_conversation_context(
        "disabled management phrase appears"
    ).hit_count == 0

    service.set_entry_disabled(
        "meme",
        source_tag="source:chime",
        title="disabled management phrase",
        disabled=False,
    )

    assert service.search("meme", "disabled management phrase", limit=1)
    assert service.build_conversation_context(
        "disabled management phrase appears"
    ).hit_count == 1


def test_collection_auto_context_override_does_not_disable_explicit_search(tmp_path):
    service = open_knowledge(tmp_path)
    KnowledgeStore(service.database_path("meme")).upsert(_entry(
        "known phrase",
        source="chime",
    ))
    assert service.build_turn_context("known phrase appears").hit_count == 1

    service.set_collection_auto_context("meme", enabled=False)

    assert service.build_turn_context("known phrase appears").hit_count == 0
    assert service.search("meme", "known phrase", limit=1)
    restarted = open_knowledge(tmp_path)
    assert restarted.get_status("meme")["auto_context"] is False


def test_missing_database_is_degraded_without_being_created(tmp_path):
    knowledge_root = tmp_path / "knowledge"
    database_parent = knowledge_root / "moegirl-knowledge"
    database_parent.mkdir(parents=True)
    database_path = database_parent / "knowledge.db"
    service = open_knowledge(knowledge_root)

    collections = service.list_collections()
    meme = next(item for item in collections if item["collection_id"] == "meme")

    assert meme["status"] == "degraded"
    assert meme["integrity_ok"] is False
    assert meme["entries"] == 0
    assert not database_path.exists()


def test_missing_knowledge_root_stays_absent_after_status_check(tmp_path):
    knowledge_root = tmp_path / "absent-knowledge"
    service = open_knowledge(knowledge_root)

    collections = service.list_collections()

    assert all(item["status"] == "degraded" for item in collections)
    assert all(item["integrity_ok"] is False for item in collections)
    assert not knowledge_root.exists()


def test_valid_empty_database_is_ready(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = service.database_path("meme")
    KnowledgeStore(database_path).replace_source("source:fixture", ())

    status = service.list_collections()
    meme = next(item for item in status if item["collection_id"] == "meme")

    assert meme["status"] == "ready"
    assert meme["integrity_ok"] is True
    assert meme["entries"] == 0


def test_one_corrupt_database_does_not_degrade_another_collection(tmp_path):
    service = open_knowledge(tmp_path)
    meme_path = service.database_path("meme")
    corpora_path = service.database_path("corpora")
    meme_path.parent.mkdir(parents=True)
    meme_path.write_bytes(b"not a sqlite database")
    KnowledgeStore(corpora_path).replace_source("source:fixture", ())

    status = {
        item["collection_id"]: item
        for item in service.list_collections()
    }

    assert status["meme"]["status"] == "degraded"
    assert status["meme"]["integrity_ok"] is False
    assert status["corpora"]["status"] == "ready"
    assert status["corpora"]["integrity_ok"] is True


def test_database_directory_is_degraded_without_replacement(tmp_path):
    service = open_knowledge(tmp_path)
    database_path = service.database_path("meme")
    database_path.mkdir(parents=True)

    status = {
        item["collection_id"]: item
        for item in service.list_collections()
    }

    assert status["meme"]["status"] == "degraded"
    assert status["meme"]["integrity_ok"] is False
    assert database_path.is_dir()
