from __future__ import annotations

import asyncio

from knowledge.moegirl_knowledge.sources.first_relevant import FirstRelevantPageSource
from knowledge.moegirl_knowledge.sources.moegirl_wiki_api import SourcePage


def test_first_relevant_result_stops_before_later_sources():
    later_source_started = False

    class FastSource:
        async def find_relevant_page(self, _query: str, *, limit: int):
            assert limit == 1
            await asyncio.sleep(0)
            return SourcePage("Fast", "content", "https://example.test/fast", 1)

    class SlowSource:
        async def find_relevant_page(self, _query: str, *, limit: int):
            nonlocal later_source_started
            later_source_started = True
            return None

    page = asyncio.run(
        FirstRelevantPageSource((FastSource(), SlowSource())).find_relevant_page("term")
    )

    assert page is not None
    assert page.title == "Fast"
    assert later_source_started is False
