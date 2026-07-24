"""Compact, local-only rendering for public meme knowledge retrieval."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from knowledge.moegirl_knowledge import MoegirlKnowledgeRetriever, MoegirlKnowledgeStore
from knowledge.moegirl_knowledge.source_registry import get_source
from knowledge.moegirl_knowledge.turn_context import get_meme_type, get_meme_usage_example
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

PUBLIC_MEME_KNOWLEDGE_TOOL_DESCRIPTION = {
    "zh": "检索本地公共梗知识库，用于理解 ACG、网络梗和作品文化背景。该工具不会联网，结果不是用户或角色记忆。",
    "en": "Search the local public meme knowledge base for ACG, internet-meme, and fandom context. This tool never accesses the network or user memory.",
    "ja": "ローカルの公共ミーム知識を検索します。このツールはネットワークやユーザー記憶にアクセスしません。",
    "ko": "로컬 공용 밈 지식을 검색합니다. 이 도구는 네트워크나 사용자 기억에 접근하지 않습니다.",
    "es": "Busca la base local de memes públicos. Esta herramienta no accede a la red ni a la memoria del usuario.",
    "pt": "Pesquisa a base local de memes públicos. Esta ferramenta não acessa a rede nem a memória do usuário.",
    "ru": "Ищет в локальной базе публичных мемов без доступа к сети или памяти пользователя.",
    "zh-TW": "檢索本機公共梗知識庫；此工具不會連網，也不會存取使用者記憶。",
}
PUBLIC_MEME_KNOWLEDGE_QUERY_DESCRIPTION = MOEGIRL_KNOWLEDGE_QUERY_DESCRIPTION


async def handle_moegirl_knowledge_call(
    arguments: dict,
    *,
    language: str,
    deadline_monotonic: float | None = None,
) -> str:
    """Search only the local SQLite database and render at most three cards."""
    del language, deadline_monotonic
    started_at = time.perf_counter()
    query = str((arguments or {}).get("query") or "").strip()
    if not query:
        return "No public knowledge query was provided."
    try:
        requested_limit = int((arguments or {}).get("limit", 3))
    except (TypeError, ValueError):
        requested_limit = 3
    limit = min(max(requested_limit, 1), 3)
    database_path = (
        Path(get_config_manager().knowledge_dir) / "moegirl-knowledge" / "knowledge.db"
    )
    retriever = MoegirlKnowledgeRetriever(MoegirlKnowledgeStore(database_path))
    hits = await asyncio.to_thread(retriever.search, query, limit=limit)
    logger.info(
        "[moegirl-knowledge] tool lookup source=local hits=%d elapsed_ms=%d",
        len(hits),
        int((time.perf_counter() - started_at) * 1000),
    )
    if not hits:
        return "No relevant public knowledge is available locally."

    lines = ["Public meme knowledge (local reference only; not a memory):"]
    for hit in hits:
        entry = hit.entry
        summary = (entry.summary or entry.content[:420]).replace("\n", " ").strip()[:500]
        meme_type = get_meme_type(entry)
        usage_example = get_meme_usage_example(entry)
        risk_note = " | caution: may include profane or offensive usage" if any(
            tag in {"risk:profanity", "risk:offense"} for tag in entry.tags
        ) else ""
        quality_note = (
            " | caution: usage may be outdated"
            if "quality:stale-usage" in entry.tags
            else ""
        )
        source = get_source(entry.source_tag)
        details = f"- {entry.title}: {summary}"
        if meme_type:
            details += f"\n  Type: {meme_type}"
        if usage_example:
            details += f"\n  Typical usage: {usage_example}"
        details += (
            f"\n  Source: {source.name} | license: {source.license}"
            f"{risk_note}{quality_note}"
        )
        lines.append(details)
    return "\n".join(lines)


handle_public_meme_knowledge_call = handle_moegirl_knowledge_call
