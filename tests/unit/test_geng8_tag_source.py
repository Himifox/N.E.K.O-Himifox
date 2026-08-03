from __future__ import annotations

import asyncio

from knowledge.moegirl_knowledge.sources.geng8_tag import Geng8TagSource


def test_keyword_search_reads_only_the_first_relevant_post_and_preserves_metadata():
    source = Geng8TagSource()
    pages_by_url = {
        "https://www.geng8.com/?s=Test+meme": (
            '<article><a href="/6481.html">first</a></article>'
            '<article><a href="/6482.html">second</a></article>'
        ),
        "https://www.geng8.com/6481.html": (
            '<article><h1 class="entry-title">Test meme</h1>'
            '<div class="entry-content">Meaning text<script>ignore()</script></div>'
            '<a rel="tag" href="/tag/network-memes/">network memes</a></article>'
        ),
    }

    async def fake_get_text(url: str) -> str:
        return pages_by_url[url]

    source._get_text = fake_get_text  # type: ignore[method-assign]
    page = asyncio.run(source.find_relevant_page("Test meme"))

    assert page is not None
    assert page.title == "Test meme"
    assert page.content == "Meaning text"
    assert page.source_name == "geng8"
    assert page.page_id == 6481
    assert page.tags == ("network memes",)
