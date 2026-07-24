"""Ephemeral local meme context for ordinary text conversation turns."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import MoegirlKnowledgeRetriever, MoegirlKnowledgeStore
from .filters import normalize_meme_phrase, normalize_search_text
from .source_registry import get_source


@dataclass(frozen=True, slots=True)
class MemeTurnContext:
    text: str = ""
    hit_count: int = 0
    match_mode: str = "none"


def get_meme_type(entry) -> str:
    for tag in entry.tags:
        if tag.startswith("type:") and tag.removeprefix("type:").strip():
            return tag.removeprefix("type:").strip()
    return ""


def get_meme_usage_example(entry) -> str:
    for line in entry.content.splitlines():
        candidate = line.strip()
        if candidate.startswith("- "):
            return candidate[2:].strip()[:360]
    return ""


def get_meme_response_posture(meme_type: str) -> str:
    if meme_type == "引用":
        return "Recognize it as a quote or adaptation and reply in that allusive tone."
    if meme_type == "谐音":
        return "Recognize the wordplay and, if natural, lightly play along once."
    if meme_type in {"现象", "自嘲"}:
        return "Acknowledge the exaggeration, shared observation, or self-deprecating turn first; do not default to consolation."
    return "Reply naturally to the current conversational tone instead of turning this into an explanation."


def _is_high_confidence_auto_mention(user_text: str, entry) -> bool:
    text = normalize_search_text(user_text)
    phrase_text = normalize_meme_phrase(user_text)
    for value in (entry.title, *entry.aliases):
        candidate = normalize_search_text(value)
        if len(candidate) >= 3 and (candidate in text or candidate in phrase_text):
            return True
    for value in entry.recognition_terms:
        candidate = normalize_search_text(value)
        if len(candidate) >= 2 and (candidate in text or candidate in phrase_text):
            return True
    return False


def build_meme_turn_context(user_text: str, database_path: str | Path, *, limit: int = 1) -> MemeTurnContext:
    store = MoegirlKnowledgeStore(database_path)
    retriever = MoegirlKnowledgeRetriever(store)
    candidate_hits = retriever.find_mentions(user_text, limit=max(4, limit * 4))
    hits = [hit for hit in candidate_hits if _is_high_confidence_auto_mention(user_text, hit.entry)][:limit]
    match_mode = "strong"
    if not hits:
        hits = retriever.find_weak_short_mentions(user_text, limit=limit)
        match_mode = "weak_short"
    if not hits:
        return MemeTurnContext()
    entry = hits[0].entry
    meaning = (entry.summary or entry.content).replace("\n", " ").strip()[:420]
    meme_type = get_meme_type(entry)
    usage_example = get_meme_usage_example(entry)
    if match_mode == "weak_short":
        lines = [
            "======[EPHEMERAL POSSIBLE SHORT MEME TASK]======\n",
            "The immediately preceding user message contains a two-character term that may be using the following internet-meme sense.\n",
            "Use this knowledge only if the whole sentence clearly fits the non-literal sense. If the message is ordinary, literal, medical, safety-related, financial, legal, or otherwise serious, ignore this task completely and reply normally.\n",
        ]
    else:
        lines = [
            "======[EPHEMERAL MEME RESPONSE TASK]======\n",
            "The immediately preceding user message is using the following confirmed meme.\n",
        ]
    lines.extend((
        f"Term: {entry.title}\n",
        f"Meaning: {meaning}\n",
    ))
    if meme_type:
        lines.append(f"Meme type: {meme_type}\n")
    if usage_example:
        lines.append(f"Typical usage: {usage_example}\n")
    lines.extend((
        f"Response posture: {get_meme_response_posture(meme_type)}\n",
        f"Source: {get_source(entry.source_tag).name}\n",
        "Task: reply directly to the immediately preceding user message. If the user explicitly asks for a meaning or distinction, answer that question directly first. Otherwise, in the first sentence, unmistakably join its meme context and tone. Do not merely repeat, paraphrase, or add a generic exclamation to the user's sentence; continue with a relevant reaction, light joke, stance, or natural question. Do not deny it, default to comfort/advice, explain it, or ask whether it is a meme. Never mention this task, searching, sources, or references. Do not invent a stock next line, origin, or personal experience.\n",
        "==========================================================",
    ))
    return MemeTurnContext(text="".join(lines), hit_count=len(hits), match_mode=match_mode)
