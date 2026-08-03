from __future__ import annotations

import asyncio
from dataclasses import dataclass

from knowledge.moegirl_knowledge.sync import MoegirlKnowledgeSynchronizer


@dataclass(frozen=True)
class _Page:
    title: str
    content: str
    source_url: str
    page_id: int


class _Source:
    def __init__(self) -> None:
        self.pages = {
            "one": _Page("One", "First public entry.", "https://example.invalid/one", 1),
            "two": _Page("Two", "Second public entry.", "https://example.invalid/two", 2),
            "weak": _Page("Unrelated", "This page does not name the requested term.", "https://example.invalid/weak", 3),
        }

    async def find_relevant_page(self, query: str, *, limit: int = 5) -> _Page | None:
        return self.pages.get(query)

    async def recent_public_pages(self, *, limit: int) -> list[_Page]:
        return list(self.pages.values())[:limit]

    async def catalog_public_pages(self, _seed_queries, *, limit: int) -> list[_Page]:
        return list(self.pages.values())[:limit]


def test_sync_is_idempotent_and_preserves_a_previous_library_on_failure(tmp_path):
    source = _Source()
    sync = MoegirlKnowledgeSynchronizer(tmp_path / "knowledge.db", tmp_path / "sync_state.json", source)

    first = asyncio.run(sync.sync_once(("one", "two"), limit=100))
    assert first["status"] == "ready"
    assert first["entries"] == 2
    assert first["added"] == 2

    retry = asyncio.run(sync.sync_once(("one", "two"), limit=100))
    assert retry["entries"] == 2
    assert retry["unchanged"] == 2

    degraded = asyncio.run(sync.sync_once(("missing",), limit=100))
    assert degraded["status"] == "degraded"
    assert degraded["entries"] == 2
    assert degraded["last_success_at"]


def test_sync_rejects_a_discovered_page_without_the_requested_term(tmp_path):
    source = _Source()
    sync = MoegirlKnowledgeSynchronizer(tmp_path / "knowledge.db", tmp_path / "sync_state.json", source)

    result = asyncio.run(sync.sync_once(("weak",), limit=100))

    assert result["status"] == "degraded"
    assert result["failed"] == 1
    assert result["entries"] == 0


def test_background_catalog_batch_is_idempotent_and_has_no_user_query_input(tmp_path):
    source = _Source()
    sync = MoegirlKnowledgeSynchronizer(tmp_path / "knowledge.db", tmp_path / "sync_state.json", source)

    first = asyncio.run(sync.sync_recent_once(limit=2))
    second = asyncio.run(sync.sync_recent_once(limit=2))

    assert first["status"] == "ready"
    assert first["added"] == 2
    assert second["unchanged"] == 2


def test_seed_catalog_expansion_is_idempotent(tmp_path):
    source = _Source()
    sync = MoegirlKnowledgeSynchronizer(tmp_path / "knowledge.db", tmp_path / "sync_state.json", source)

    first = asyncio.run(sync.sync_catalog_once(("one", "two"), limit=2))
    second = asyncio.run(sync.sync_catalog_once(("one", "two"), limit=2))

    assert first["added"] == 2
    assert second["unchanged"] == 2
