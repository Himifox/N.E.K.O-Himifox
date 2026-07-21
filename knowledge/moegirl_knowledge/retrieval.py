"""Read-only retrieval over the local Moegirl knowledge database."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from .filters import make_fts_query, normalize_meme_phrase, normalize_search_text
from .models import MoegirlKnowledgeHit
from .store import MoegirlKnowledgeStore, _entry_from_row


_AUTO_MENTION_MIN_LENGTH = 3


@dataclass(slots=True)
class _TrieNode:
    children: dict[str, int] = field(default_factory=dict)
    entries: list[tuple[object, int]] = field(default_factory=list)


class MemeMentionMatcher:
    """Rebuildable multi-phrase matcher for complete conversational messages.

    This is deliberately a database-derived dictionary, not a list of hand-written
    sentence patterns.  Titles and verified aliases are scanned in one pass, so a
    user never needs to quote a meme for local turn context to find it.
    """

    def __init__(self, entries: Iterable[object]) -> None:
        self._nodes = [_TrieNode()]
        self._entry_terms: dict[str, int] = {}
        for entry in entries:
            for value in (entry.title, *entry.aliases):
                phrase = normalize_search_text(value)
                if len(phrase) < _AUTO_MENTION_MIN_LENGTH:
                    continue
                self._insert(phrase, entry)

    def _insert(self, phrase: str, entry: object) -> None:
        node_index = 0
        for character in phrase:
            node = self._nodes[node_index]
            node_index = node.children.setdefault(character, len(self._nodes))
            if node_index == len(self._nodes):
                self._nodes.append(_TrieNode())
        terminal = self._nodes[node_index].entries
        if not any(existing.id == entry.id for existing, _ in terminal):
            terminal.append((entry, len(phrase)))

    def find(self, text: str, *, limit: int) -> list[MoegirlKnowledgeHit]:
        if limit <= 0:
            return []
        best_by_id: dict[str, tuple[object, int]] = {}
        for start_index in range(len(text)):
            node_index = 0
            for character in text[start_index:]:
                next_index = self._nodes[node_index].children.get(character)
                if next_index is None:
                    break
                node_index = next_index
                for entry, length in self._nodes[node_index].entries:
                    previous = best_by_id.get(entry.id)
                    if previous is None or length > previous[1]:
                        best_by_id[entry.id] = (entry, length)
        hits = [
            MoegirlKnowledgeHit(entry=entry, score=float(length))
            for entry, length in best_by_id.values()
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.entry.title))
        return hits[:limit]


class MoegirlKnowledgeRetriever:
    """Retrieve compact, source-attributed candidates without prompt injection."""

    def __init__(self, store: MoegirlKnowledgeStore) -> None:
        self.store = store

    def search(self, query: str, *, limit: int = 3) -> list[MoegirlKnowledgeHit]:
        query_text = normalize_search_text(query)
        if not query_text or limit <= 0:
            return []
        candidate_limit = max(12, limit * 4)
        rows_by_id = {}
        for row in self.store.query_fts(make_fts_query(query), limit=candidate_limit):
            rows_by_id[row["id"]] = row
        for row in self.store.query_like(query_text, limit=candidate_limit):
            rows_by_id.setdefault(row["id"], row)

        hits: list[MoegirlKnowledgeHit] = []
        for row in rows_by_id.values():
            try:
                entry = _entry_from_row(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                # A damaged row must not make public-knowledge lookup block a
                # conversation.  Later management tooling can report it.
                continue
            score = _score(entry, query_text, float(row["rank"]) if "rank" in row.keys() else 0.0)
            hits.append(MoegirlKnowledgeHit(entry=entry, score=score))
        hits.sort(key=lambda hit: (-hit.score, hit.entry.title))
        return hits[:limit]

    def find_mentions(self, user_text: str, *, limit: int = 1) -> list[MoegirlKnowledgeHit]:
        """Find known phrases anywhere in a normal conversational sentence."""
        normalized_text = normalize_search_text(user_text)
        if len(normalized_text) < 2 or limit <= 0:
            return []
        phrase_text = normalize_meme_phrase(user_text)
        matcher = _get_cached_mention_matcher(self.store)
        results = matcher.find(normalized_text, limit=max(limit * 2, limit))
        if phrase_text and phrase_text != normalized_text:
            results.extend(matcher.find(phrase_text, limit=max(limit * 2, limit)))
        best_by_id: dict[str, MoegirlKnowledgeHit] = {}
        for hit in results:
            previous = best_by_id.get(hit.entry.id)
            if previous is None or hit.score > previous.score:
                best_by_id[hit.entry.id] = hit
        return sorted(best_by_id.values(), key=lambda hit: (-hit.score, hit.entry.title))[:limit]


@dataclass(slots=True)
class _CachedMentionMatcher:
    revision: int
    matcher: MemeMentionMatcher


_MENTION_MATCHER_CACHE: dict[str, _CachedMentionMatcher] = {}


def _get_cached_mention_matcher(store: MoegirlKnowledgeStore) -> MemeMentionMatcher:
    """Refresh the per-database matcher only after a committed upsert batch."""
    cache_key = str(store.database_path.resolve())
    revision = store.entries_revision()
    cached = _MENTION_MATCHER_CACHE.get(cache_key)
    if cached is None or cached.revision != revision:
        cached = _CachedMentionMatcher(
            revision=revision,
            matcher=MemeMentionMatcher(store.list_active_entries()),
        )
        _MENTION_MATCHER_CACHE[cache_key] = cached
    return cached.matcher


def _score(entry, normalized_query: str, fts_rank: float) -> float:
    title = normalize_search_text(entry.title)
    aliases = [normalize_search_text(value) for value in entry.aliases]
    tags = [normalize_search_text(value) for value in entry.tags]
    if normalized_query == title:
        return 1_000.0
    if normalized_query in aliases:
        return 950.0
    if normalized_query in title:
        return 850.0
    if any(normalized_query in alias for alias in aliases):
        return 800.0
    if any(normalized_query in tag for tag in tags):
        return 700.0
    return 100.0 - fts_rank
