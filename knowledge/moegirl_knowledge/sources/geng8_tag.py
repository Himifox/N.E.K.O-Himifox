"""Bounded, read-only crawler for public Geng8 tag archives."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..filters import is_relevant_source_page
from .moegirl_wiki_api import SourcePage


BASE_URL = "https://www.geng8.com/"
USER_AGENT = "N.E.K.O-MemeKnowledge/0.1 (read-only background sync)"
SOURCE_LICENSE = "Geng8 public webpage (license not stated; preserve source URL)"


class Geng8TagSource:
    """Fetch only the first relevant public result for a queued meme keyword."""

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

    async def find_relevant_page(self, query: str, *, limit: int = 1) -> SourcePage | None:
        """Search one keyword and fetch only the first public post result."""
        if limit <= 0 or not query.strip():
            return None
        search_url = f"{BASE_URL}?s={quote_plus(query)}"
        article_urls = self._article_urls(await self._get_text(search_url))
        if not article_urls:
            return None
        page = self._page_from_html(article_urls[0], await self._get_text(article_urls[0]))
        if page is None or not is_relevant_source_page(query, title=page.title, content=page.content):
            return None
        return page

    @staticmethod
    def _article_urls(html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        return Geng8TagSource._urls_from_links(soup, ".html")

    @staticmethod
    def _urls_from_links(node, required_path: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for link in node.select("a[href]"):
            url = urljoin(BASE_URL, str(link.get("href") or ""))
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "www.geng8.com"
                or required_path not in parsed.path
                or (required_path == ".html" and not re.fullmatch(r"/\d+\.html", parsed.path))
                or url in seen
            ):
                continue
            seen.add(url)
            urls.append(url)
        return urls

    @staticmethod
    def _page_from_html(url: str, html: str) -> SourcePage | None:
        soup = BeautifulSoup(html, "html.parser")
        content_node = soup.select_one("article .entry-content, .entry-content")
        title_node = soup.select_one("article h1.entry-title, h1.entry-title, article h1")
        if content_node is None or title_node is None:
            return None
        for unwanted in content_node.select("script, style, noscript"):
            unwanted.decompose()
        title = title_node.get_text(" ", strip=True)
        content = content_node.get_text("\n", strip=True)
        if not title or not content:
            return None
        tag_values = tuple(
            tag.get_text(" ", strip=True)
            for tag in soup.select('a[rel~="tag"]')
            if tag.get_text(" ", strip=True)
        )
        match = re.search(r"/(\d+)\.html$", urlparse(url).path)
        return SourcePage(
            title=title,
            content=content,
            source_url=url,
            page_id=int(match.group(1)) if match else None,
            source_name="geng8",
            source_license=SOURCE_LICENSE,
            tags=tag_values,
        )

    async def _get_text(self, url: str) -> str:
        async with self._request_lock:
            delay = self.request_delay_seconds - (
                asyncio.get_running_loop().time() - self._last_request_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                self._last_request_at = asyncio.get_running_loop().time()
                return response.text
