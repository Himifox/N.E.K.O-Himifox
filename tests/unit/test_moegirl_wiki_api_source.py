from __future__ import annotations

import asyncio

from knowledge.moegirl_knowledge.sources.moegirl_wiki_api import MoegirlWikiApiSource


def test_prefers_ranked_full_text_candidates_and_fetches_only_a_relevant_one():
    source = MoegirlWikiApiSource()
    calls: list[dict[str, str]] = []

    async def fake_request(params: dict[str, str]):
        calls.append(params)
        if params.get("generator") == "search":
            return {
                "query": {
                    "pageids": ["1", "2"],
                    "pages": {
                        "1": {"title": "Unrelated", "extract": "A different topic.", "fullurl": "https://example.invalid/1"},
                        "2": {"title": "Target meme", "extract": "Target meme is explained here.", "fullurl": "https://example.invalid/2"},
                    },
                }
            }
        assert params["titles"] == "Target meme"
        return {
            "query": {"pages": {"2": {
                "title": "Target meme", "extract": "Target meme full public explanation.",
                "fullurl": "https://example.invalid/2", "pageid": 2,
            }}}
        }

    source._request = fake_request  # type: ignore[method-assign]
    page = asyncio.run(source.find_relevant_page("Target meme"))

    assert page is not None
    assert page.title == "Target meme"
    assert len(calls) == 2
    assert calls[0]["generator"] == "search"


def test_falls_back_to_title_search_when_full_text_has_no_candidates():
    source = MoegirlWikiApiSource()
    calls: list[dict[str, str]] = []

    async def fake_request(params: dict[str, str]):
        calls.append(params)
        if params.get("generator") == "search":
            return {"query": {"pages": {}}}
        if params["action"] == "opensearch":
            return ["Target", ["Target"], [], ["https://example.invalid/target"]]
        return {
            "query": {"pages": {"9": {
                "title": "Target", "extract": "Target is documented.",
                "fullurl": "https://example.invalid/target", "pageid": 9,
            }}}
        }

    source._request = fake_request  # type: ignore[method-assign]
    page = asyncio.run(source.find_relevant_page("Target"))

    assert page is not None
    assert [call["action"] for call in calls] == ["query", "opensearch", "query"]


def test_recent_public_pages_uses_the_catalog_not_a_user_query():
    source = MoegirlWikiApiSource()
    calls: list[dict[str, str]] = []

    async def fake_request(params: dict[str, str]):
        calls.append(params)
        if params.get("list") == "recentchanges":
            return {"query": {"recentchanges": [
                {"title": "Fresh meme"}, {"title": "Fresh meme"}, {"title": "Another page"},
            ]}}
        title = params["titles"]
        return {"query": {"pages": {"1": {
            "title": title, "extract": f"{title} explanation.",
            "fullurl": f"https://example.invalid/{title}", "pageid": len(calls),
        }}}}

    source._request = fake_request  # type: ignore[method-assign]
    pages = asyncio.run(source.recent_public_pages(limit=10))

    assert [page.title for page in pages] == ["Fresh meme", "Another page"]
    assert calls[0]["list"] == "recentchanges"
    assert all("gsrsearch" not in call for call in calls)


def test_seed_catalog_expands_wiki_category_members_not_chat_text():
    source = MoegirlWikiApiSource()
    calls: list[dict[str, str]] = []

    async def fake_find(query: str, *, limit: int):
        from knowledge.moegirl_knowledge.sources import SourcePage

        assert query == "bootstrap"
        assert limit == 3
        return SourcePage("Bootstrap", "seed explanation", "https://example.invalid/seed", 1)

    async def fake_request(params: dict[str, str]):
        calls.append(params)
        if params.get("prop") == "categories":
            return {"query": {"pages": {"1": {"categories": [{"title": "Category:Memes"}]}}}}
        if params.get("list") == "categorymembers":
            return {"query": {"categorymembers": [{"title": "Category member"}]}}
        title = params["titles"]
        return {"query": {"pages": {"2": {
            "title": title, "extract": "member explanation", "fullurl": "https://example.invalid/member", "pageid": 2,
        }}}}

    source.find_relevant_page = fake_find  # type: ignore[method-assign]
    source._request = fake_request  # type: ignore[method-assign]
    pages = asyncio.run(source.catalog_public_pages(("bootstrap",), limit=2))

    assert [page.title for page in pages] == ["Bootstrap", "Category member"]
    assert any(call.get("list") == "categorymembers" for call in calls)
