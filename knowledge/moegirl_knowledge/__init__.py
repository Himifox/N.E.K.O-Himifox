"""Local, source-attributed Moegirl knowledge-base primitives.

Networking, scheduling, and conversation-tool registration intentionally land
in later phases.  This package only owns the offline data model and retrieval.
"""

from .models import MoegirlKnowledgeEntry, MoegirlKnowledgeHit, UpsertResult
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
