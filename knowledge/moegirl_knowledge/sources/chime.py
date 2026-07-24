"""Offline importer for the bundled CHIME Chinese Internet-meme dataset."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from ..filters import normalize_meme_phrase, normalize_search_text
from ..models import MoegirlKnowledgeEntry


CHIME_COMMIT = "865ef186a0e797ec5ac242524a3c45b30a429542"
CHIME_DATASET_URL = (
    "https://github.com/yuboxie/chime/blob/"
    f"{CHIME_COMMIT}/data/chime_full.json"
)
CHIME_LICENSE = "MIT (CHIME dataset; Copyright (c) 2025 Yubo Xie)"
# The bundled JSONL is checked out with LF line endings by Git.  Keep the
# integrity check aligned with the bytes the application actually packages.
CHIME_SHA256 = "e7eaea229d6d8a7af0c3273067bca8220d89c85bc1865b7907cc397e279e8e75"
CHIME_ENTRY_COUNT = 1_458
_STALE_USAGE_TERMS = frozenset({"水灵灵"})


@dataclass(frozen=True, slots=True)
class ChimeDataset:
    """Validated bundled records and immutable provenance metadata."""

    entries: tuple[MoegirlKnowledgeEntry, ...]
    sha256: str
    commit: str


def load_bundled_chime_dataset() -> ChimeDataset:
    """Load one fixed JSONL asset without executing third-party code or networking."""
    raw = (
        files("knowledge.moegirl_knowledge.data")
        .joinpath("chime_full.jsonl")
        .read_bytes()
    )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CHIME_SHA256:
        raise ValueError("bundled CHIME dataset hash mismatch")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("bundled CHIME dataset is not valid UTF-8 JSONL") from exc
    if len(lines) != CHIME_ENTRY_COUNT:
        raise ValueError("bundled CHIME dataset has an unexpected record count")

    entries: list[MoegirlKnowledgeEntry] = []
    seen_records: set[str] = set()
    for record_index, line in enumerate(lines):
        if not line:
            raise ValueError("bundled CHIME dataset contains a blank record")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"bundled CHIME dataset has invalid JSON at line {record_index + 1}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError("bundled CHIME record is not an object")
        entry = _entry_from_record(record, record_index=record_index)
        if entry.content_hash in seen_records:
            raise ValueError("bundled CHIME dataset has duplicate records")
        seen_records.add(entry.content_hash)
        entries.append(entry)
    return ChimeDataset(entries=tuple(entries), sha256=digest, commit=CHIME_COMMIT)


def _entry_from_record(record: Any, *, record_index: int) -> MoegirlKnowledgeEntry:
    if not isinstance(record, dict):
        raise ValueError("bundled CHIME record is not an object")
    meme = _required_text(record, "meme")
    meaning = _required_text(record, "meaning")
    origin = _optional_text(record.get("origin"))
    examples = _text_values(record.get("examples"))
    type_cn = _optional_text(record.get("type_cn"))
    normalized = normalize_search_text(meme)
    if not normalized:
        raise ValueError("bundled CHIME record has an invalid meme term")
    content_sections = [f"含义：{meaning}"]
    if origin:
        content_sections.append(f"出处：{origin}")
    if examples:
        content_sections.append("例句：\n" + "\n".join(f"- {example}" for example in examples))
    tags = ["source:chime", "scope:public"]
    if type_cn:
        tags.append(f"type:{type_cn}")
    if record.get("profanity") is True:
        tags.append("risk:profanity")
    if record.get("offense") is True:
        tags.append("risk:offense")
    if meme in _STALE_USAGE_TERMS:
        tags.append("quality:stale-usage")
    phrase_alias = normalize_meme_phrase(meme)
    aliases = (phrase_alias,) if phrase_alias and phrase_alias != normalized else ()
    content = "\n\n".join(content_sections)
    # Aliases participate in FTS/index updates.  Include them in the fixed
    # asset's hash so a manual/startup reimport upgrades existing local rows.
    return MoegirlKnowledgeEntry(
        # A displayed term can legitimately have multiple dataset definitions.
        # Keep each fixed source record distinct instead of guessing that they
        # are aliases with the same meaning.
        title=meme,
        terms={"alias": aliases, "recognition": ()},
        tags=tuple(tags),
        summary=meaning,
        content=content,
    )


def _required_text(record: dict[str, Any], key: str) -> str:
    value = _optional_text(record.get(key))
    if not value:
        raise ValueError(f"bundled CHIME record is missing {key}")
    return value


def _optional_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
