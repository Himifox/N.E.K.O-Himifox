from __future__ import annotations

from knowledge.moegirl_knowledge import MoegirlKnowledgeRetriever, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.sources.chime import (
    CHIME_COMMIT,
    CHIME_ENTRY_COUNT,
    CHIME_LICENSE,
    CHIME_SHA256,
    load_bundled_chime_dataset,
)


def test_bundled_chime_dataset_has_pinned_integrity_and_provenance():
    dataset = load_bundled_chime_dataset()

    assert dataset.commit == CHIME_COMMIT
    assert dataset.sha256 == CHIME_SHA256
    assert len(dataset.entries) == CHIME_ENTRY_COUNT
    assert all(entry.source_license == CHIME_LICENSE for entry in dataset.entries)
    assert all("source:chime" in entry.tags for entry in dataset.entries)
    assert len({entry.id for entry in dataset.entries}) == CHIME_ENTRY_COUNT


def test_bundled_chime_import_is_idempotent_and_searchable(tmp_path):
    dataset = load_bundled_chime_dataset()
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")

    first = store.upsert_many(dataset.entries)
    second = store.upsert_many(dataset.entries)

    assert sum(result.created for result in first) == CHIME_ENTRY_COUNT
    assert all(result.unchanged for result in second)
    assert store.count() == CHIME_ENTRY_COUNT
    hits = MoegirlKnowledgeRetriever(store).search("treetree", limit=1)
    assert hits and hits[0].entry.id.startswith("chime:")
