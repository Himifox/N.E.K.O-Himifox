from __future__ import annotations

import pytest

import knowledge.api as public_api
from knowledge.collection_specs import (
    GENERIC_REFERENCE_RESPONSE_POLICY,
    CollectionSpec,
)
from knowledge.engine.models import KnowledgeEntry
from knowledge.engine.retrieval import MatchPolicy
from knowledge.engine.source_registry import KnowledgeSource
from knowledge.engine.store import KnowledgeStore
from knowledge.service import KnowledgeService


def _spec(*, automatic: bool = True, restricted: bool = False) -> CollectionSpec:
    return CollectionSpec(
        collection_id="reference",
        storage_directory="reference",
        display_name="Reference",
        priority=5,
        auto_context_enabled=automatic,
        restrict_auto_context_to_registered_sources=restricted,
        sources=(KnowledgeSource("source:fixture", "Fixture", license="CC0-1.0"),),
        match_policy=MatchPolicy(title_min_length=3, alias_min_length=3),
        response_policy=GENERIC_REFERENCE_RESPONSE_POLICY,
    )


def _entry(title: str, *, source: str = "fixture") -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": [f"{title} alias"], "recognition": []},
        tags=(f"source:{source}", "type:reference"),
        summary=f"Meaning of {title}",
        content=f"Details\n- {title} example",
    )


def _service(tmp_path, *, automatic: bool = True, restricted: bool = False):
    spec = _spec(automatic=automatic, restricted=restricted)
    service = KnowledgeService(tmp_path, collections=(spec,))
    KnowledgeStore(service.database_path("reference")).upsert_many(
        (_entry("known phrase"), _entry("second phrase"))
    )
    return service


def test_public_api_is_small_and_does_not_export_engine_primitives() -> None:
    assert "KnowledgeService" in public_api.__all__
    assert "validate_pack" in public_api.__all__
    assert "KnowledgeStore" not in public_api.__all__
    assert "MatchPolicy" not in public_api.__all__


def test_service_has_no_implicit_builtin_domains(tmp_path) -> None:
    service = public_api.open_knowledge(tmp_path)

    assert service.list_collections() == ()
    with pytest.raises(ValueError, match="unknown knowledge collection"):
        service.search("missing", "anything")


def test_trusted_collection_search_pagination_and_status(tmp_path) -> None:
    service = _service(tmp_path)

    assert service.search("reference", "known phrase")[0].entry.title == "known phrase"
    assert len(service.search_page("reference", "phrase", limit=1)) == 2
    assert service.list_entries("reference", limit=1)[0].title == "known phrase"
    status = service.get_status("reference")
    assert status["entries"] == 2
    assert status["integrity_ok"] is True
    assert status["sources"] == ({"tag": "source:fixture", "entries": 2},)


def test_disable_and_restore_affects_search_and_routing(tmp_path) -> None:
    service = _service(tmp_path)
    assert service.build_turn_context("known phrase appears").hit_count == 1

    service.set_entry_disabled(
        "reference",
        source_tag="source:fixture",
        title="known phrase",
        disabled=True,
    )
    assert service.search("reference", "known phrase") == []
    assert service.build_turn_context("known phrase appears").hit_count == 0

    service.set_entry_disabled(
        "reference",
        source_tag="source:fixture",
        title="known phrase",
        disabled=False,
    )
    assert service.build_turn_context("known phrase appears").hit_count == 1


def test_collection_override_only_changes_automatic_context(tmp_path) -> None:
    service = _service(tmp_path)
    service.set_collection_auto_context("reference", enabled=False)

    assert service.build_turn_context("known phrase appears").hit_count == 0
    assert service.search("reference", "known phrase")

    restarted = KnowledgeService(tmp_path, collections=(_spec(),))
    assert restarted.build_turn_context("known phrase appears").hit_count == 0


def test_registered_source_restriction_excludes_untrusted_source_from_context(tmp_path) -> None:
    spec = _spec(restricted=True)
    service = KnowledgeService(tmp_path, collections=(spec,))
    store = KnowledgeStore(service.database_path("reference"))
    store.upsert(_entry("trusted phrase"))
    store.upsert(_entry("other phrase", source="other"))

    assert service.build_turn_context("trusted phrase appears").hit_count == 1
    assert service.build_turn_context("other phrase appears").hit_count == 0
    assert service.search("reference", "other phrase")


def test_context_card_uses_collection_source_metadata(tmp_path) -> None:
    service = _service(tmp_path)

    context = service.build_turn_context("known phrase appears")

    assert context.hit_count == 1
    assert "Meaning of known phrase" in context.text
    assert "Source: Fixture" in context.text
    assert "CC0" not in context.text
