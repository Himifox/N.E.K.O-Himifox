"""Async MediaWiki adapter following mcp-server-moegirl-wiki's lookup flow."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from ..filters import is_relevant_source_page


API_BASE = "https://mzh.moegirl.org.cn/api.php"
PAGE_BASE = "https://mzh.moegirl.org.cn/"
USER_AGENT = "N.E.K.O-MoegirlKnowledge/0.1"


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """A ranked, lightweight search candidate before fetching a full page."""

    title: str
    source_url: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class SourcePage:
    title: str
    content: str
    source_url: str
    page_id: int | None


class MoegirlWikiApiSource:
    """Read public pages using the verified MCP search-then-fetch pattern."""

    API_BASE = API_BASE
    PAGE_BASE = PAGE_BASE
    USER_AGENT = USER_AGENT

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        request_delay_seconds: float = 0.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self._last_request_at = 0.0
        self._request_lock = asyncio.Lock()

    async def discover_candidates(self, query: str, *, limit: int = 5) -> list[SourceCandidate]:
        """Use ranked full-text results first, falling back to title fuzzy search."""
        candidates = await self._full_text_search(query, limit=limit)
        if candidates:
            return candidates
        return await self._title_search(query, limit=limit)

    async def find_relevant_page(self, query: str, *, limit: int = 5) -> SourcePage | None:
        """Validate ranked summaries before fetching a candidate's full page.

        Search results are discovery hints only.  A page is accepted only when
        the request is present in its title or rendered content.
        """
        for candidate in await self.discover_candidates(query, limit=limit):
            if not is_relevant_source_page(
                query, title=candidate.title, content=candidate.snippet
            ):
                continue
            page = await self.fetch_page(candidate.title)
            if page is not None and is_relevant_source_page(
                query, title=page.title, content=page.content
            ):
                return page
        return None

    async def recent_public_pages(self, *, limit: int = 20) -> list[SourcePage]:
        """Fetch a bounded batch of publicly changed pages for slow background growth.

        This path is intentionally unrelated to a user's current conversation.
        It provides eventual library expansion without turning ordinary chat into
        a sequence of live searches.
        """
        if limit <= 0:
            return []
        payload = await self._request({
            "action": "query", "list": "recentchanges", "rcnamespace": "0",
            "rctype": "edit|new", "rcprop": "title", "rclimit": str(limit),
            "format": "json",
        })
        changes = payload.get("query", {}).get("recentchanges", []) if isinstance(payload, dict) else []
        titles: list[str] = []
        seen: set[str] = set()
        for change in changes if isinstance(changes, list) else ():
            title = str(change.get("title") or "").strip() if isinstance(change, dict) else ""
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
        pages: list[SourcePage] = []
        for title in titles:
            page = await self.fetch_page(title)
            if page is not None:
                pages.append(page)
        return pages

    async def catalog_public_pages(
        self,
        seed_queries: Iterable[str],
        *,
        limit: int = 20,
    ) -> list[SourcePage]:
        """Expand source-defined categories around a small bootstrap catalog.

        Seed phrases only locate initial public pages.  Subsequent candidates
        come from the wiki's own category membership, rather than from user
        messages or a manually maintained list of every meme.
        """
        remaining = max(0, limit)
        pages: list[SourcePage] = []
        categories: list[str] = []
        seen_categories: set[str] = set()
        for query in seed_queries:
            if remaining <= 0:
                break
            page = await self.find_relevant_page(str(query), limit=3)
            if page is None:
                continue
            pages.append(page)
            remaining -= 1
            for category in await self._categories_for_title(page.title):
                if category not in seen_categories:
                    seen_categories.add(category)
                    categories.append(category)
        seen_titles = {page.title for page in pages}
        for category in categories:
            if remaining <= 0:
                break
            for title in await self._category_member_titles(category, limit=remaining):
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                page = await self.fetch_page(title)
                if page is not None:
                    pages.append(page)
                    remaining -= 1
                if remaining <= 0:
                    break
        return pages

    async def _categories_for_title(self, title: str) -> list[str]:
        payload = await self._request({
            "action": "query", "prop": "categories", "titles": title,
            "cllimit": "20", "format": "json",
        })
        pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
        if not isinstance(pages, dict):
            return []
        categories: list[str] = []
        for page in pages.values():
            raw_categories = page.get("categories", []) if isinstance(page, dict) else []
            for category in raw_categories if isinstance(raw_categories, list) else ():
                value = str(category.get("title") or "").strip() if isinstance(category, dict) else ""
                if value:
                    categories.append(value)
        return categories

    async def _category_member_titles(self, category: str, *, limit: int) -> list[str]:
        payload = await self._request({
            "action": "query", "list": "categorymembers", "cmtitle": category,
            "cmnamespace": "0", "cmlimit": str(limit), "format": "json",
        })
        members = payload.get("query", {}).get("categorymembers", []) if isinstance(payload, dict) else []
        return [
            title for member in members if isinstance(member, dict)
            if (title := str(member.get("title") or "").strip())
        ]

    async def _full_text_search(self, query: str, *, limit: int) -> list[SourceCandidate]:
        payload = await self._request({
            "action": "query", "generator": "search", "gsrsearch": query,
            "gsrlimit": str(limit), "gsrnamespace": "0", "prop": "info|extracts",
            "inprop": "url", "exintro": "1", "explaintext": "1", "format": "json",
        })
        if not isinstance(payload, dict):
            return []
        query_block = payload.get("query")
        if not isinstance(query_block, dict):
            return []
        pages = query_block.get("pages")
        if not isinstance(pages, dict):
            return []
        ranked_pages: list[dict[str, Any]] = []
        page_ids = query_block.get("pageids")
        if isinstance(page_ids, list):
            ranked_pages.extend(
                page for page_id in page_ids
                if isinstance(page := pages.get(str(page_id)), dict)
            )
        if not ranked_pages:
            ranked_pages = [page for page in pages.values() if isinstance(page, dict)]
        return [candidate for page in ranked_pages if (candidate := self._candidate_from_page(page))]

    async def _title_search(self, query: str, *, limit: int) -> list[SourceCandidate]:
        payload = await self._request({
            "action": "opensearch", "search": query, "limit": str(limit),
            "namespace": "0", "format": "json",
        })
        if not isinstance(payload, list) or len(payload) < 4:
            return []
        titles = payload[1] if isinstance(payload[1], list) else []
        urls = payload[3] if isinstance(payload[3], list) else []
        return [
            SourceCandidate(
                title=title.strip(),
                source_url=(urls[index].strip() if index < len(urls) and isinstance(urls[index], str) else self.PAGE_BASE + title),
            )
            for index, title in enumerate(titles)
            if isinstance(title, str) and title.strip()
        ]

    def _candidate_from_page(self, page: dict[str, Any]) -> SourceCandidate | None:
        title = str(page.get("title") or "").strip()
        if not title:
            return None
        snippet = re.sub(r"<[^>]+>", "", str(page.get("extract") or ""))
        return SourceCandidate(
            title=title,
            source_url=str(page.get("fullurl") or self.PAGE_BASE + title).strip(),
            snippet=html.unescape(snippet).strip(),
        )

    async def fetch_page(self, title: str) -> SourcePage | None:
        payload = await self._request({
            "action": "query", "prop": "extracts|info", "inprop": "url",
            "redirects": "1", "explaintext": "1", "titles": title, "format": "json",
        })
        pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
        if not isinstance(pages, dict) or not pages:
            return None
        page = next(iter(pages.values()))
        if not isinstance(page, dict) or "missing" in page:
            return None
        content = str(page.get("extract") or "").strip()
        resolved_title = str(page.get("title") or title).strip()
        if not content or not resolved_title:
            return None
        page_id = page.get("pageid")
        return SourcePage(
            title=resolved_title,
            content=content,
            source_url=str(page.get("fullurl") or "").strip(),
            page_id=page_id if isinstance(page_id, int) else None,
        )

    async def _request(self, params: dict[str, str]) -> Any:
        async with self._request_lock:
            delay = self.request_delay_seconds - (asyncio.get_running_loop().time() - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"User-Agent": self.USER_AGENT},
                follow_redirects=True,
            ) as client:
                response = await client.get(self.API_BASE, params=params)
                response.raise_for_status()
                self._last_request_at = asyncio.get_running_loop().time()
                return response.json()
