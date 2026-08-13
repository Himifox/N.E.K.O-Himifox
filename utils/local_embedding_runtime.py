"""Shared local ONNX text-embedding capability.

This module is the non-business API used outside the memory domain.  The
existing implementation remains behind a compatibility import in v1 so the
knowledge runtime does not couple itself to Memory Server APIs, storage, or
recall logic.  A later mechanical relocation can preserve this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LocalEmbeddingService(Protocol):
    def is_available(self) -> bool: ...
    def is_disabled(self) -> bool: ...
    def disable_reason(self) -> str: ...
    def model_id(self) -> str | None: ...
    def dim(self) -> int | None: ...
    async def request_load(self) -> bool: ...
    async def embed(self, text: str) -> list[float] | None: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalEmbeddingStatus:
    state: str
    model_id: str = ""
    dimensions: int = 0
    disable_reason: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ready" and bool(self.model_id) and self.dimensions > 0


def get_local_embedding_service() -> LocalEmbeddingService:
    """Return this process's local service without touching memory data."""
    try:
        from memory.embeddings import get_embedding_service
    except Exception:
        from memory.embeddings_fallback import get_embedding_service
    return get_embedding_service()


def get_local_embedding_status() -> LocalEmbeddingStatus:
    service = get_local_embedding_service()
    if service.is_available():
        state = "ready"
    elif service.is_disabled():
        state = "disabled"
    else:
        state = "not_ready"
    return LocalEmbeddingStatus(
        state=state,
        model_id=service.model_id() or "",
        dimensions=int(service.dim() or 0),
        disable_reason=service.disable_reason() if service.is_disabled() else "",
    )


async def release_local_embedding_service() -> None:
    """Release this process's singleton without exposing its legacy owner."""
    try:
        from memory.embeddings import release_embedding_service
    except Exception:
        service = get_local_embedding_service()
        close = getattr(service, "close", None)
        if close is not None:
            await close()
        return
    await release_embedding_service()
