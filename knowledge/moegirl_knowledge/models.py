"""Typed records for the public Moegirl knowledge base."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from .filters import sanitize_external_text


DEFAULT_SOURCE_LICENSE = "CC BY-NC-SA 3.0 CN (verify page-specific terms)"


def _clean_values(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = sanitize_external_text(value, max_chars=300)
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class MoegirlKnowledgeEntry:
    """A source-attributed public knowledge record, never a character memory."""

    id: str
    title: str
    content: str
    source_url: str
    source_page_id: int | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    summary: str = ""
    source_license: str = DEFAULT_SOURCE_LICENSE
    content_hash: str = ""
    synced_at: str = ""
    status: str = "active"

    def __post_init__(self) -> None:
        entry_id = sanitize_external_text(self.id, max_chars=200)
        title = sanitize_external_text(self.title, max_chars=500)
        content = sanitize_external_text(self.content)
        source_url = sanitize_external_text(self.source_url, max_chars=2_000)
        if not entry_id or not title or not content or not source_url:
            raise ValueError("id, title, content, and source_url are required")
        object.__setattr__(self, "id", entry_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "source_url", source_url)
        object.__setattr__(self, "aliases", _clean_values(self.aliases))
        object.__setattr__(self, "tags", _clean_values(self.tags))
        object.__setattr__(self, "summary", sanitize_external_text(self.summary, max_chars=4_000))
        object.__setattr__(self, "source_license", sanitize_external_text(self.source_license, max_chars=1_000))
        object.__setattr__(self, "synced_at", sanitize_external_text(self.synced_at, max_chars=100))
        object.__setattr__(self, "status", sanitize_external_text(self.status, max_chars=40) or "active")
        digest = self.content_hash or sha256(content.encode("utf-8")).hexdigest()
        object.__setattr__(self, "content_hash", sanitize_external_text(digest, max_chars=128))


@dataclass(frozen=True, slots=True)
class MoegirlKnowledgeHit:
    """A compact result suitable for later tool rendering."""

    entry: MoegirlKnowledgeEntry
    score: float


@dataclass(frozen=True, slots=True)
class UpsertResult:
    entry_id: str
    created: bool = False
    updated: bool = False
    unchanged: bool = False
