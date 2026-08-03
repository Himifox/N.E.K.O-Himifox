"""Local, source-attributed Moegirl knowledge-base primitives.

Networking, scheduling, and conversation-tool registration intentionally land
in later phases.  This package only owns the offline data model and retrieval.
"""

from ..engine import (
    KnowledgeEntry as MoegirlKnowledgeEntry,
    KnowledgeHit as MoegirlKnowledgeHit,
    KnowledgeRetriever as MoegirlKnowledgeRetriever,
    KnowledgeStore as MoegirlKnowledgeStore,
    KnowledgeStoreError,
    UpsertResult,
)

__all__ = [
    "KnowledgeStoreError",
    "MoegirlKnowledgeEntry",
    "MoegirlKnowledgeHit",
    "MoegirlKnowledgeRetriever",
    "MoegirlKnowledgeStore",
    "UpsertResult",
]
