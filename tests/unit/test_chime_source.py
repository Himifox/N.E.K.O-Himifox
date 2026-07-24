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
    assert CHIME_LICENSE
    assert all("source:chime" in entry.tags for entry in dataset.entries)
    assert len({entry.content_hash for entry in dataset.entries}) == CHIME_ENTRY_COUNT


def test_bundled_chime_import_is_idempotent_and_searchable(tmp_path):
    dataset = load_bundled_chime_dataset()
    store = MoegirlKnowledgeStore(tmp_path / "knowledge.db")

    first = store.replace_source("source:chime", dataset.entries)
    second = store.replace_source("source:chime", dataset.entries)

    assert sum(result.created for result in first) == CHIME_ENTRY_COUNT
    assert sum(result.created for result in second) == CHIME_ENTRY_COUNT
    assert store.count() == CHIME_ENTRY_COUNT
    hits = MoegirlKnowledgeRetriever(store).search("treetree", limit=1)
    assert hits and hits[0].entry.source_tag == "source:chime"
