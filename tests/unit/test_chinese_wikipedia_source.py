from __future__ import annotations

import asyncio

from knowledge.moegirl_knowledge.sources import ChineseWikipediaApiSource


def test_chinese_wikipedia_source_uses_its_own_read_only_endpoint():
    source = ChineseWikipediaApiSource()
    calls: list[dict[str, str]] = []

    async def fake_request(params: dict[str, str]):
        calls.append(params)
        if params.get("generator") == "search":
            return {
                "query": {"pageids": ["8"], "pages": {"8": {
                    "title": "Target meme", "extract": "Target meme is explained here.",
                    "fullurl": "https://zh.wikipedia.org/wiki/Target_meme",
                }}}
            }
        return {"query": {"pages": {"8": {
            "title": "Target meme", "extract": "Target meme full explanation.",
            "fullurl": "https://zh.wikipedia.org/wiki/Target_meme", "pageid": 8,
        }}}}

    source._request = fake_request  # type: ignore[method-assign]
    page = asyncio.run(source.find_relevant_page("Target meme"))

    assert page is not None
    assert source.API_BASE == "https://zh.wikipedia.org/w/api.php"
    assert calls[0]["generator"] == "search"
