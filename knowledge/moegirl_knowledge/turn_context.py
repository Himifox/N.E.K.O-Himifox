"""Compatibility facade for the built-in meme knowledge collection."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from knowledge.service import (
    MEME_RESPONSE_POLICY,
    KnowledgeService,
    KnowledgeTurnContext,
    get_tag_value,
    get_usage_example,
)


MemeTurnContext = KnowledgeTurnContext


def get_meme_type(entry) -> str:
    """Compatibility wrapper for callers rendering explicit meme results."""
    return get_tag_value(entry, "type:")


def get_meme_usage_example(entry) -> str:
    """Compatibility wrapper for the first source-provided usage example."""
    return get_usage_example(entry)


def get_meme_response_posture(meme_type: str) -> str:
    """Return the trusted response posture without domain-specific branching."""
    return MEME_RESPONSE_POLICY.type_postures.get(
        meme_type,
        MEME_RESPONSE_POLICY.default_posture,
    )


def build_meme_turn_context(
    user_text: str,
    database_path: str | Path,
    *,
    limit: int = 1,
) -> MemeTurnContext:
    """Preserve the existing entrypoint while delegating to the generic service."""
    service = _service_for_database(str(Path(database_path).resolve()))
    return service.build_turn_context(
        user_text,
        collection_ids=("meme",),
        limit=limit,
    )


@lru_cache(maxsize=16)
def _service_for_database(database_path: str) -> KnowledgeService:
    return KnowledgeService.for_collection("meme", database_path)
