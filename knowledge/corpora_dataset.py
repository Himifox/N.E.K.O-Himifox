"""Validated loader for the small bundled Corpora demonstration collection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from .moegirl_knowledge.models import MoegirlKnowledgeEntry


CORPORA_COMMIT = "cf30ca27ab176b63623af1ddcfa2447ac07305ba"
CORPORA_HOMEPAGE = "https://github.com/dariusk/corpora"
CORPORA_LICENSE = "CC0 1.0"
CORPORA_ENTRY_COUNT = 229
CORPORA_SHA256 = "a0edfbec31136c80480095affba34d84a9638cc46315f31dc15abc4c603befda"
_FIELDS = frozenset(("title", "terms", "tags", "summary", "content"))


@dataclass(frozen=True, slots=True)
class CorporaDataset:
    entries: tuple[MoegirlKnowledgeEntry, ...]
    sha256: str
    commit: str


def load_bundled_corpora_dataset() -> CorporaDataset:
    """Load the pinned JSONL asset without networking or third-party code."""
    raw = files("knowledge.data").joinpath("corpora_demo.jsonl").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CORPORA_SHA256:
        raise ValueError("bundled Corpora dataset hash mismatch")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("bundled Corpora dataset is not valid UTF-8 JSONL") from exc
    if len(lines) != CORPORA_ENTRY_COUNT:
        raise ValueError("bundled Corpora dataset has an unexpected record count")

    entries: list[MoegirlKnowledgeEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines, start=1):
        if not line:
            raise ValueError("bundled Corpora dataset contains a blank record")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"bundled Corpora dataset has invalid JSON at line {index}") from exc
        entry = _entry_from_record(record, index=index)
        key = (entry.source_tag, entry.title.casefold())
        if key in seen:
            raise ValueError("bundled Corpora dataset contains duplicate titles")
        seen.add(key)
        entries.append(entry)
    return CorporaDataset(tuple(entries), digest, CORPORA_COMMIT)


def _entry_from_record(record: Any, *, index: int) -> MoegirlKnowledgeEntry:
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError(f"bundled Corpora record {index} has invalid fields")
    terms = record.get("terms")
    tags = record.get("tags")
    if not isinstance(terms, dict) or set(terms) != {"alias", "recognition"}:
        raise ValueError(f"bundled Corpora record {index} has invalid terms")
    if not isinstance(tags, list) or "source:corpora" not in tags:
        raise ValueError(f"bundled Corpora record {index} has invalid source")
    return MoegirlKnowledgeEntry(
        title=str(record.get("title") or ""),
        terms=terms,
        tags=tuple(tags),
        summary=str(record.get("summary") or ""),
        content=str(record.get("content") or ""),
    )
