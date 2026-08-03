"""Local, source-attributed Moegirl knowledge-base primitives.

Networking, scheduling, and conversation-tool registration intentionally land
in later phases.  This package only owns the offline data model and retrieval.
"""

from ..engine.models import (
    KnowledgeEntry as MoegirlKnowledgeEntry,
    KnowledgeHit as MoegirlKnowledgeHit,
    UpsertResult,
)
from .retrieval import MoegirlKnowledgeRetriever
from .store import MoegirlKnowledgeStore, KnowledgeStoreError

__all__ = [
    "KnowledgeStoreError",
    "MoegirlKnowledgeEntry",
    "MoegirlKnowledgeHit",
    "MoegirlKnowledgeRetriever",
    "MoegirlKnowledgeStore",
    "UpsertResult",
]
