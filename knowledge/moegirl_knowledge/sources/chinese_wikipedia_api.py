"""Read-only Chinese Wikipedia adapter for opt-in public knowledge fallback."""

from __future__ import annotations

from .moegirl_wiki_api import MoegirlWikiApiSource


class ChineseWikipediaApiSource(MoegirlWikiApiSource):
    """Use the same MediaWiki search-then-fetch safety flow on zh.wikipedia."""

    API_BASE = "https://zh.wikipedia.org/w/api.php"
    PAGE_BASE = "https://zh.wikipedia.org/wiki/"
    USER_AGENT = "N.E.K.O-PublicMemeKnowledge/0.1 (read-only Wikipedia fallback)"
