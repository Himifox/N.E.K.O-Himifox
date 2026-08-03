"""Stable public prefix for local conversational knowledge."""

from __future__ import annotations

from pathlib import Path

from .collection_specs import (
    CORPORA_RESPONSE_POLICY,
    MEME_RESPONSE_POLICY,
    CollectionSpec,
    MaterialRoute,
    ResponsePolicy,
    get_reference_details,
    get_tag_value,
    get_usage_example,
)
from .engine.models import KnowledgeEntry, KnowledgeHit, UpsertResult
from .engine.retrieval import (
    KnowledgeMentionMatcher,
    KnowledgeRetriever,
    MatchPolicy,
)
from .engine.routing import ContextHint
from .engine.store import KnowledgeStore, KnowledgeStoreError
from .packs import KnowledgePack, KnowledgePackSource, PackInstallResult
from .service import KnowledgeService, KnowledgeTurnContext
from .subscriptions import (
    SUBSCRIPTION_PROTOCOL_VERSION,
    KnowledgeSubscription,
    canonical_pack_bytes,
    load_canonical_pack_artifact,
    validate_subscription,
)


def open_knowledge(knowledge_root: str | Path) -> KnowledgeService:
    """Open the local service without starting tasks or accessing the network."""
    return KnowledgeService.from_root(knowledge_root)


__all__ = [
    "CORPORA_RESPONSE_POLICY",
    "CollectionSpec",
    "ContextHint",
    "KnowledgeEntry",
    "KnowledgeHit",
    "KnowledgeMentionMatcher",
    "KnowledgePack",
    "KnowledgePackSource",
    "KnowledgeRetriever",
    "KnowledgeService",
    "KnowledgeStore",
    "KnowledgeStoreError",
    "KnowledgeSubscription",
    "KnowledgeTurnContext",
    "MEME_RESPONSE_POLICY",
    "MaterialRoute",
    "MatchPolicy",
    "PackInstallResult",
    "ResponsePolicy",
    "SUBSCRIPTION_PROTOCOL_VERSION",
    "UpsertResult",
    "canonical_pack_bytes",
    "get_reference_details",
    "get_tag_value",
    "get_usage_example",
    "load_canonical_pack_artifact",
    "open_knowledge",
    "validate_subscription",
]
