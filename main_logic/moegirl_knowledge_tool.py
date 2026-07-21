"""Safe, compact rendering for public Moegirl knowledge retrieval."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from knowledge.moegirl_knowledge import MoegirlKnowledgeRetriever, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.filters import is_relevant_source_page
from knowledge.moegirl_knowledge.models import MoegirlKnowledgeEntry
from knowledge.moegirl_knowledge.sources import ChineseWikipediaApiSource, MoegirlWikiApiSource
from config.moegirl_knowledge_settings import (
    MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_FALLBACK_ENABLED,
    MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS,
)
from utils.config_manager import get_config_manager
from utils.logger_config import get_module_logger


logger = get_module_logger(__name__, "Main")


MOEGIRL_KNOWLEDGE_TOOL_DESCRIPTION = {
    "zh": "查询本地萌娘梗知识库，用于解释 ACG、网络梗和作品文化背景。不要把结果当作用户经历或角色记忆。",
    "en": "Search local Moegirl public knowledge for ACG, meme, and fandom context. Never treat results as user or character memories.",
    "ja": "ローカルの萌娘百科由来の公開知識を検索します。結果をユーザーやキャラクターの記憶として扱わないでください。",
    "ko": "ACG·밈·팬덤 맥락을 위해 로컬 공개 지식을 검색합니다. 결과를 사용자나 캐릭터의 기억으로 취급하지 마세요.",
    "es": "Busca conocimiento público local de Moegirl para memes, ACG y contexto de fandom. Nunca lo trates como memoria del usuario o personaje.",
    "pt": "Pesquisa conhecimento público local do Moegirl para memes, ACG e contexto de fandom. Nunca trate o resultado como memória do usuário ou personagem.",
    "ru": "Ищет локальные публичные знания Moegirl о мемах, ACG и фэндоме. Не считайте результат памятью пользователя или персонажа.",
    "zh-TW": "查詢本地萌娘梗知識庫，用於解釋 ACG、網路梗和作品文化背景。不要把結果當作使用者或角色記憶。",
}
MOEGIRL_KNOWLEDGE_QUERY_DESCRIPTION = {
    "zh": "要查询的梗、术语或作品文化背景。",
    "en": "The meme, term, or fandom context to look up.",
    "ja": "調べるミーム、用語、またはファンダムの文脈。",
    "ko": "조회할 밈, 용어 또는 팬덤 맥락입니다.",
    "es": "El meme, término o contexto de fandom que se busca.",
    "pt": "O meme, termo ou contexto de fandom a pesquisar.",
    "ru": "Мем, термин или контекст фэндома для поиска.",
    "zh-TW": "要查詢的梗、術語或作品文化背景。",
}

# The legacy tool name remains available for already-configured roles.  New
# sessions use these source-neutral descriptions because local results can now
# come from the bundled CHIME dataset as well as Moegirl Wiki.
PUBLIC_MEME_KNOWLEDGE_TOOL_DESCRIPTION = {
    "zh": "检索本地公共梗知识库，用于理解 ACG、网络梗和作品文化背景。结果不是用户或角色记忆。",
    "en": "Search local public meme knowledge for ACG, internet-meme, and fandom context. Never treat results as user or character memories.",
    "ja": "ローカルの公共ミーム知識を検索し、ACG、ネットミーム、作品文化の文脈を確認します。結果をユーザーやキャラクターの記憶として扱わないでください。",
    "ko": "ACG, 인터넷 밈, 팬덤 맥락을 위한 로컬 공용 밈 지식을 검색합니다. 결과를 사용자 또는 캐릭터의 기억으로 취급하지 마세요.",
    "es": "Busca conocimiento público local sobre memes, ACG y contexto de fandom. Nunca lo trates como memoria del usuario o personaje.",
    "pt": "Pesquisa conhecimento público local sobre memes, ACG e contexto de fandom. Nunca trate o resultado como memória do usuário ou personagem.",
    "ru": "Ищет локальные публичные знания о мемах, ACG и фэндомном контексте. Не считайте результат памятью пользователя или персонажа.",
    "zh-TW": "檢索本機公共梗知識庫，用於理解 ACG、網路梗和作品文化背景。結果不是使用者或角色記憶。",
}
PUBLIC_MEME_KNOWLEDGE_QUERY_DESCRIPTION = {
    "zh": "要查询的梗、术语或作品文化背景。",
    "en": "The meme, term, or fandom context to look up.",
    "ja": "調べるミーム、用語、またはファンダムの文脈です。",
    "ko": "조회할 밈, 용어 또는 팬덤 맥락입니다.",
    "es": "El meme, término o contexto de fandom que se busca.",
    "pt": "O meme, termo ou contexto de fandom a pesquisar.",
    "ru": "Мем, термин или контекст фэндома для поиска.",
    "zh-TW": "要查詢的梗、術語或作品文化背景。",
}

_fallback_lock = asyncio.Lock()


async def handle_moegirl_knowledge_call(
    arguments: dict, *, language: str, deadline_monotonic: float | None = None,
) -> str:
    started_at = time.perf_counter()
    query = str((arguments or {}).get("query") or "").strip()
    if not query:
        return "No public knowledge query was provided."
    try:
        requested_limit = int((arguments or {}).get("limit", 3))
    except (TypeError, ValueError):
        requested_limit = 3
    limit = min(max(requested_limit, 1), 3)
    config_manager = get_config_manager()
    database_path = Path(config_manager.knowledge_dir) / "moegirl-knowledge" / "knowledge.db"
    store = MoegirlKnowledgeStore(database_path)
    retriever = MoegirlKnowledgeRetriever(store)
    hits = await asyncio.to_thread(retriever.search, query, limit=limit)
    lookup_source = "local"
    if not hits:
        if MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_FALLBACK_ENABLED:
            hits, lookup_source = await _fetch_and_store_on_miss(
                query, store, retriever, limit=limit, deadline_monotonic=deadline_monotonic,
            )
        else:
            lookup_source = "local_miss_encyclopedia_disabled"
    logger.info(
        "[moegirl-knowledge] tool lookup source=%s hits=%d elapsed_ms=%d",
        lookup_source,
        len(hits),
        int((time.perf_counter() - started_at) * 1000),
    )
    if not hits:
        if MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_FALLBACK_ENABLED:
            return (
                "No relevant public knowledge was found in the local database or encyclopedia sources. "
                "If an enabled web_search plugin is available, use it once as the final fallback."
            )
        return "No relevant public knowledge is available locally."
    lines = ["Public meme knowledge (reference only; not a memory):"]
    for hit in hits:
        entry = hit.entry
        summary = entry.summary or entry.content[:420]
        summary = summary.replace("\n", " ").strip()[:500]
        if "source:chime" in entry.tags:
            source_name = "CHIME (MIT dataset)"
        elif "wikipedia" in entry.tags:
            source_name = "Chinese Wikipedia"
        else:
            source_name = "Moegirl Wiki"
        risk_note = " | caution: may include profane or offensive usage" if any(
            tag in {"risk:profanity", "risk:offense"} for tag in entry.tags
        ) else ""
        lines.append(
            f"- {entry.title}: {summary}\n"
            f"  Source: {source_name} | {entry.source_url} | synced: {entry.synced_at or 'unknown'}{risk_note}"
        )
    return "\n".join(lines)


handle_public_meme_knowledge_call = handle_moegirl_knowledge_call


async def _fetch_and_store_on_miss(
    query: str,
    store: MoegirlKnowledgeStore,
    retriever: MoegirlKnowledgeRetriever,
    *,
    limit: int,
    deadline_monotonic: float | None = None,
) -> tuple[list, str]:
    """Query attributed encyclopedia sources serially within one turn budget."""
    try:
        timeout_seconds = MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS
        if deadline_monotonic is not None:
            timeout_seconds = min(timeout_seconds, deadline_monotonic - time.monotonic())
        if timeout_seconds <= 0:
            return [], "encyclopedia_deadline_exhausted"
        deadline = time.monotonic() + timeout_seconds
        async with asyncio.timeout(timeout_seconds):
            async with _fallback_lock:
                already_available = await asyncio.to_thread(retriever.search, query, limit=limit)
                if already_available:
                    return already_available, "local_after_lock"
                sources = (
                    (
                        "moegirl",
                        MoegirlWikiApiSource(timeout_seconds=MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS),
                        "CC BY-NC-SA 3.0 CN (verify page-specific terms)",
                    ),
                    (
                        "wikipedia",
                        ChineseWikipediaApiSource(timeout_seconds=MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS),
                        "CC BY-SA 4.0",
                    ),
                )
                for source_index, (source_name, source, source_license) in enumerate(sources):
                    remaining_seconds = deadline - time.monotonic()
                    sources_remaining = len(sources) - source_index
                    if remaining_seconds <= 0:
                        return [], "encyclopedia_deadline_exhausted"
                    try:
                        # Sequential sources receive an equal share of the
                        # remaining request budget.  A slow first source must
                        # not prevent the second encyclopedia from running.
                        async with asyncio.timeout(remaining_seconds / sources_remaining):
                            page = await source.find_relevant_page(query, limit=5)
                    except TimeoutError:
                        logger.info("[moegirl-knowledge] encyclopedia source timed out source=%s", source_name)
                        continue
                    except Exception:
                        logger.warning("[moegirl-knowledge] encyclopedia source failed source=%s", source_name)
                        continue
                    if page is None or not page.source_url or not is_relevant_source_page(
                        query, title=page.title, content=page.content
                    ):
                        continue
                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    entry_id = (
                        f"{source_name}:{page.page_id}"
                        if page.page_id is not None else f"{source_name}:query:{query}"
                    )
                    entry = MoegirlKnowledgeEntry(
                        id=entry_id, title=page.title, content=page.content,
                        summary=page.content[:600], source_url=page.source_url,
                        source_page_id=page.page_id, tags=(source_name, "on-demand"),
                        source_license=source_license, synced_at=now,
                    )
                    await asyncio.to_thread(store.upsert, entry)
                    hits = await asyncio.to_thread(retriever.search, query, limit=limit)
                    if hits:
                        return hits, f"{source_name}_stored"
                return [], "encyclopedia_miss"
    except TimeoutError:
        logger.info("[moegirl-knowledge] encyclopedia lookup timed out")
        return [], "encyclopedia_timeout"
