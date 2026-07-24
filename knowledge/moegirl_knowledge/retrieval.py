"""Read-only retrieval over the local Moegirl knowledge database."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from .catalog_overrides import entry_key, get_catalog_override_path, load_disabled_entries
from .filters import make_fts_query, normalize_meme_phrase, normalize_search_text
from .models import MoegirlKnowledgeHit
from .store import MoegirlKnowledgeStore, _entry_from_row


_AUTO_MENTION_MIN_LENGTH = 3
_AUTO_RECOGNITION_MIN_LENGTH = 2
_WEAK_SHORT_TERM_LENGTH = 2
_STALE_USAGE_TAG = "quality:stale-usage"


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
        self._weak_short_terms: list[tuple[str, object, int]] = []
        for entry in entries:
            for term_kind, value in enumerate((entry.title, *entry.aliases)):
                phrase = normalize_search_text(value)
                if len(phrase) >= _AUTO_MENTION_MIN_LENGTH:
                    self._insert(phrase, entry)
                elif len(phrase) == _WEAK_SHORT_TERM_LENGTH and _is_weak_short_entry(entry):
                    # Title wins over aliases at the same position.  All aliases
                    # share the same secondary priority because their stored order
                    # is not semantic evidence.
                    self._weak_short_terms.append((phrase, entry, min(term_kind, 1)))
            for value in entry.recognition_terms:
                phrase = normalize_search_text(value)
                if len(phrase) < _AUTO_RECOGNITION_MIN_LENGTH:
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
        if not any(existing.content_hash == entry.content_hash for existing, _ in terminal):
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
                    entry_key = entry.content_hash
                    previous = best_by_id.get(entry_key)
                    if previous is None or length > previous[1]:
                        best_by_id[entry_key] = (entry, length)
        hits = [
            MoegirlKnowledgeHit(entry=entry, score=float(length))
            for entry, length in best_by_id.values()
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.entry.title))
        return hits[:limit]

    def find_weak_short(self, text: str, *, limit: int) -> list[MoegirlKnowledgeHit]:
        """Find eligible two-character CHIME terms by exact continuous text."""
        if limit <= 0:
            return []
        best_by_id: dict[str, tuple[int, int, str, object]] = {}
        for phrase, entry, term_priority in self._weak_short_terms:
            position = text.find(phrase)
            if position < 0:
                continue
            candidate = (position, term_priority, entry.title, entry)
            previous = best_by_id.get(entry.content_hash)
            if previous is None or candidate[:3] < previous[:3]:
                best_by_id[entry.content_hash] = candidate
        ordered = sorted(best_by_id.values(), key=lambda value: value[:3])
        return [
            MoegirlKnowledgeHit(entry=entry, score=float(_WEAK_SHORT_TERM_LENGTH))
            for _, _, _, entry in ordered[:limit]
        ]

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
            rows_by_id[row["rowid"]] = row
        for row in self.store.query_like(query_text, limit=candidate_limit):
            rows_by_id.setdefault(row["rowid"], row)

        disabled = load_disabled_entries(get_catalog_override_path(self.store.database_path))
        hits: list[MoegirlKnowledgeHit] = []
        for row in rows_by_id.values():
            try:
                entry = _entry_from_row(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                # A damaged row must not make public-knowledge lookup block a
                # conversation.  Later management tooling can report it.
                continue
            if entry_key(entry) in disabled:
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
            entry_key = hit.entry.content_hash
            previous = best_by_id.get(entry_key)
            if previous is None or hit.score > previous.score:
                best_by_id[entry_key] = hit
        return sorted(best_by_id.values(), key=lambda hit: (-hit.score, hit.entry.title))[:limit]

    def find_weak_short_mentions(
        self,
        user_text: str,
        *,
        limit: int = 1,
    ) -> list[MoegirlKnowledgeHit]:
        """Find cautious two-character candidates after strong matching misses."""
        normalized_text = normalize_search_text(user_text)
        if len(normalized_text) < _WEAK_SHORT_TERM_LENGTH or limit <= 0:
            return []
        return _get_cached_mention_matcher(self.store).find_weak_short(
            normalized_text,
            limit=limit,
        )

@dataclass(slots=True)
class _CachedMentionMatcher:
    revision: int
    disabled: frozenset[tuple[str, str]]
    matcher: MemeMentionMatcher


_MENTION_MATCHER_CACHE: dict[str, _CachedMentionMatcher] = {}


def _get_cached_mention_matcher(store: MoegirlKnowledgeStore) -> MemeMentionMatcher:
    """Refresh the per-database matcher only after a committed upsert batch."""
    cache_key = str(store.database_path.resolve())
    revision = store.entries_revision()
    disabled = load_disabled_entries(get_catalog_override_path(store.database_path))
    cached = _MENTION_MATCHER_CACHE.get(cache_key)
    if cached is None or cached.revision != revision or cached.disabled != disabled:
        cached = _CachedMentionMatcher(
            revision=revision,
            disabled=disabled,
            matcher=MemeMentionMatcher(
                entry
                for entry in store.list_active_entries()
                if entry_key(entry) not in disabled and _STALE_USAGE_TAG not in entry.tags
            ),
        )
        _MENTION_MATCHER_CACHE[cache_key] = cached
    return cached.matcher


def _is_weak_short_entry(entry: object) -> bool:
    """Return whether an entry has enough local evidence for a weak hint."""
    tags = tuple(entry.tags)
    if "source:chime" not in tags or _STALE_USAGE_TAG in tags:
        return False
    if not any(tag.startswith("type:") and tag.removeprefix("type:").strip() for tag in tags):
        return False
    return any(line.strip().startswith("- ") for line in entry.content.splitlines())


def _score(entry, normalized_query: str, fts_rank: float) -> float:
    title = normalize_search_text(entry.title)
    aliases = [normalize_search_text(value) for value in entry.aliases]
    recognition_terms = [normalize_search_text(value) for value in entry.recognition_terms]
    tags = [normalize_search_text(value) for value in entry.tags]
    if normalized_query == title:
        return 1_000.0
    if normalized_query in aliases:
        return 950.0
    if normalized_query in title:
        return 850.0
    if any(normalized_query in alias for alias in aliases):
        return 800.0
    if normalized_query in recognition_terms:
        return 900.0
    if any(normalized_query in value for value in recognition_terms):
        return 780.0
    if any(normalized_query in tag for tag in tags):
        return 700.0
    return 100.0 - fts_rank
