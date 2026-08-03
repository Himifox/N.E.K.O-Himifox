"""Query a small fixed set of attributed sources in source-policy order."""

from __future__ import annotations

from collections.abc import Sequence


class FirstRelevantPageSource:
    """Return the first relevant result without concurrently hitting websites."""

    def __init__(self, sources: Sequence[object]) -> None:
        self.sources = tuple(sources)

    async def find_relevant_page(self, query: str, *, limit: int = 1):
        for source in self.sources:
            try:
                page = await source.find_relevant_page(query, limit=limit)
            except Exception:
                continue
            if page is not None:
                return page
        return None
