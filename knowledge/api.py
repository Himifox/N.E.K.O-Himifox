"""Stable public prefix for local conversational knowledge."""

from __future__ import annotations

from pathlib import Path

from .engine.models import KnowledgeEntry, KnowledgeHit, UpsertResult
from .moegirl_knowledge.retrieval import (
    KnowledgeMentionMatcher,
    KnowledgeRetriever,
    MatchPolicy,
)
from .moegirl_knowledge.store import (
    KnowledgeStoreError,
    MoegirlKnowledgeStore as KnowledgeStore,
)
from .packs import KnowledgePack, KnowledgePackSource, PackInstallResult
from .routing import ContextHint
from .subscriptions import (
    SUBSCRIPTION_PROTOCOL_VERSION,
    KnowledgeSubscription,
    canonical_pack_bytes,
    load_canonical_pack_artifact,
    validate_subscription,
)
from .service import (
    CollectionSpec,
    KnowledgeService,
    KnowledgeTurnContext,
    MaterialRoute,
    ResponsePolicy,
)


def open_knowledge(knowledge_root: str | Path) -> KnowledgeService:
    """Open the local service without starting tasks or accessing the network."""
    return KnowledgeService.from_root(knowledge_root)


__all__ = [
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
    "MaterialRoute",
    "MatchPolicy",
    "PackInstallResult",
    "ResponsePolicy",
    "SUBSCRIPTION_PROTOCOL_VERSION",
    "UpsertResult",
    "canonical_pack_bytes",
    "load_canonical_pack_artifact",
    "open_knowledge",
    "validate_subscription",
]
