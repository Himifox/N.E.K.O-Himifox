"""Stable public API for local, data-only knowledge collections."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .collection_specs import CollectionSpec
from .engine.models import KnowledgeEntry, KnowledgeHit
from .packs import (
    KnowledgeCollectionManifest,
    KnowledgePack,
    KnowledgePackSource,
    KnowledgePackValidationError,
    KnowledgeValidationIssue,
    PackInstallResult,
    canonical_pack_bytes,
    load_pack,
    validate_pack,
)
from .service import KnowledgeService, KnowledgeTurnContext
from .subscriptions import (
    SUBSCRIPTION_PROTOCOL_VERSION,
    KnowledgeSubscription,
    load_canonical_pack_artifact,
    validate_subscription,
)


def open_knowledge(
    knowledge_root: str | Path,
    *,
    collections: Iterable[CollectionSpec] = (),
) -> KnowledgeService:
    """Open the local service without starting tasks or accessing the network."""
    return KnowledgeService.from_root(knowledge_root, collections=collections)


__all__ = [
    "KnowledgeCollectionManifest",
    "KnowledgeEntry",
    "KnowledgeHit",
    "KnowledgePack",
    "KnowledgePackSource",
    "KnowledgePackValidationError",
    "KnowledgeService",
    "KnowledgeSubscription",
    "KnowledgeTurnContext",
    "KnowledgeValidationIssue",
    "PackInstallResult",
    "SUBSCRIPTION_PROTOCOL_VERSION",
    "canonical_pack_bytes",
    "load_canonical_pack_artifact",
    "load_pack",
    "open_knowledge",
    "validate_pack",
    "validate_subscription",
]
