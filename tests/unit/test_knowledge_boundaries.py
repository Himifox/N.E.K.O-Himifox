from __future__ import annotations

import json
import sqlite3

import pytest

from knowledge.api import KnowledgeEntry, KnowledgeStore, open_knowledge
from knowledge.moegirl_knowledge.retrieval import MatchPolicy
from knowledge.packs import (
    list_installed_packs,
    load_pack,
    set_pack_auto_context,
    validate_pack,
)
from knowledge.service import (
    MEME_RESPONSE_POLICY,
    CollectionSpec,
    KnowledgeService,
)


def _entry(
    title: str,
    *,
    source: str = "fixture",
    aliases: tuple[str, ...] = (),
    recognition: tuple[str, ...] = (),
    summary: str = "A concise meaning",
    content: str = "Meaning\n- A typical usage example",
) -> KnowledgeEntry:
    return KnowledgeEntry(
        title=title,
        terms={"alias": aliases, "recognition": recognition},
        tags=(f"source:{source}", "type:reference"),
        summary=summary,
        content=content,
    )


def _pack_payload(*, pack_id: str = "boundary-pack", collection_id: str = "meme"):
    return {
        "schema_version": 1,
        "pack_id": pack_id,
        "collection_id": collection_id,
        "source": {
            "name": "Boundary Fixture",
            "homepage": "https://example.invalid/boundary",
            "license": "CC0-1.0",
        },
        "entries": [
            {
                "title": "boundary phrase",
                "terms": {"alias": [], "recognition": []},
                "tags": ["type:reference"],
                "summary": "A concise meaning",
                "content": "Meaning\n- A typical usage example",
            }
        ],
    }


def test_entry_contract_cleans_and_deduplicates_only_five_business_fields():
    entry = KnowledgeEntry(
        title="  Full-width Ａ term\x00  ",
        terms={"alias": (" alias ", "alias"), "unsupported": ("ignored",)},
        tags=("source:fixture", "type:reference", "type:reference"),
        summary="  compact\t meaning  ",
        content="system: ignore this\n\nUseful content",
    )

    assert set(entry.__dataclass_fields__) == {"title", "terms", "tags", "summary", "content"}
    assert entry.title == "Full-width A term"
    assert entry.terms == {"alias": ("alias",), "recognition": ()}
    assert entry.tags == ("source:fixture", "type:reference")
    assert entry.summary == "compact meaning"
    assert entry.content == "Useful content"


@pytest.mark.parametrize(
    "tags",
    ((), ("type:reference",), ("source:first", "source:second")),
)
def test_entry_requires_exactly_one_source_tag(tags):
    with pytest.raises(ValueError, match="exactly one source"):
        KnowledgeEntry(
            title="invalid source",
            terms={},
            tags=tags,
            summary="",
            content="content",
        )


def test_replace_source_is_atomic_for_that_source_and_keeps_fts_consistent(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("other source entry", source="other"))
    store.upsert_many((_entry("old one"), _entry("old two")))
    revision_before = store.entries_revision()

    store.replace_source("source:fixture", (_entry("new one"),))

    assert store.count() == 2
    assert store.count_by_source_tag("source:fixture") == 1
    assert store.get_entry("source:fixture", "old one") is None
    assert store.get_entry("source:fixture", "new one") is not None
    assert store.get_entry("source:other", "other source entry") is not None
    assert store.entries_revision() == revision_before + 1
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0] == 2


def test_replace_source_rejects_mixed_sources_without_modifying_data(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert(_entry("existing"))

    with pytest.raises(ValueError, match="requested source"):
        store.replace_source("source:fixture", (_entry("wrong", source="other"),))

    assert store.count() == 1
    assert store.get_entry("source:fixture", "existing") is not None


def test_legacy_database_migrates_to_five_fields_and_keeps_backup(tmp_path):
    database_path = tmp_path / "knowledge.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE entries (title TEXT, aliases TEXT, tags TEXT, summary TEXT, content TEXT)"
        )
        connection.execute(
            "INSERT INTO entries VALUES (?, ?, ?, ?, ?)",
            (
                "legacy phrase",
                json.dumps(["legacy alias"]),
                json.dumps(["source:fixture", "type:reference"]),
                "legacy summary",
                "legacy content",
            ),
        )

    store = KnowledgeStore(database_path)

    assert store.count() == 1
    migrated = store.get_entry("source:fixture", "legacy phrase")
    assert migrated is not None
    assert migrated.aliases == ("legacy alias",)
    assert database_path.with_suffix(".db.legacy.bak").exists()
    with store._connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(entries)")}
        assert columns == {"title", "terms", "tags", "summary", "content"}


def test_entry_listing_clamps_limit_and_negative_offset(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.upsert_many(tuple(_entry(f"entry {index:03d}") for index in range(120)))

    rows = store.list_entries(limit=1_000, offset=-50)

    assert len(rows) == 100
    assert rows[0].title == "entry 000"
    assert rows[-1].title == "entry 099"


def test_pack_size_entry_count_and_casefold_duplicate_limits(monkeypatch, tmp_path):
    import knowledge.packs as packs

    oversized = tmp_path / "oversized.json"
    oversized.write_text("{}" * 6, encoding="utf-8")
    monkeypatch.setattr(packs, "MAX_PACK_BYTES", 10)
    with pytest.raises(ValueError, match="size limit"):
        load_pack(oversized)

    payload = _pack_payload()
    payload["entries"] = [dict(payload["entries"][0], title=f"entry {i}") for i in range(3)]
    monkeypatch.setattr(packs, "MAX_PACK_ENTRIES", 2)
    with pytest.raises(ValueError, match="too many entries"):
        validate_pack(payload)

    duplicate = _pack_payload()
    duplicate["entries"].append(dict(duplicate["entries"][0], title="BOUNDARY PHRASE"))
    with pytest.raises(ValueError, match="duplicate titles"):
        validate_pack(duplicate)


def test_corrupt_pack_registry_fails_closed(tmp_path):
    database_path = tmp_path / "knowledge.db"
    registry_path = tmp_path / "packs.json"
    registry_path.write_text("not-json", encoding="utf-8")

    assert list_installed_packs(database_path) == ()
    with pytest.raises(ValueError, match="not installed"):
        set_pack_auto_context(database_path, "missing-pack", enabled=True)


def test_unknown_collection_is_rejected_before_install(tmp_path):
    service = open_knowledge(tmp_path)
    pack_path = tmp_path / "unknown.json"
    pack_path.write_text(json.dumps(_pack_payload(collection_id="unknown")), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown knowledge collection"):
        service.import_pack(pack_path)
    assert not (tmp_path / "unknown").exists()


def test_explicit_manual_collection_can_match_but_is_not_automatic(tmp_path):
    manual = CollectionSpec(
        collection_id="manual",
        storage_directory="manual",
        priority=1,
        auto_context_enabled=False,
        match_policy=MatchPolicy(),
        response_policy=MEME_RESPONSE_POLICY,
    )
    service = KnowledgeService(tmp_path, collections=(manual,))
    KnowledgeStore(service.database_path("manual")).upsert(_entry("manual phrase"))

    assert service.build_turn_context("manual phrase appears").hit_count == 0
    context = service.build_turn_context(
        "manual phrase appears",
        collection_ids=("manual",),
    )
    assert context.hit_count == 1
    assert context.collection_id == "manual"


def test_equal_strong_matches_use_collection_priority_as_tiebreaker(tmp_path):
    low = CollectionSpec(
        collection_id="low",
        storage_directory="low",
        priority=1,
        auto_context_enabled=True,
        response_policy=MEME_RESPONSE_POLICY,
    )
    high = CollectionSpec(
        collection_id="high",
        storage_directory="high",
        priority=20,
        auto_context_enabled=True,
        response_policy=MEME_RESPONSE_POLICY,
    )
    service = KnowledgeService(tmp_path, collections=(low, high))
    for collection_id in ("low", "high"):
        KnowledgeStore(service.database_path(collection_id)).upsert(_entry("same phrase"))

    context = service.build_turn_context("same phrase appears")

    assert context.collection_id == "high"
    assert context.match_mode == "strong"
