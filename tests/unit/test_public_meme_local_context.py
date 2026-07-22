from __future__ import annotations

import pytest

from knowledge.moegirl_knowledge.turn_context import MemeTurnContext
from main_logic.core.streaming import StreamingMixin


class _Manager(StreamingMixin):
    pass


@pytest.mark.asyncio
async def test_local_miss_never_invokes_a_model_or_network_fallback(monkeypatch):
    """Ordinary chat must stay on the local path after a knowledge miss."""
    import knowledge.moegirl_knowledge.turn_context as turn_context

    manager = _Manager()
    monkeypatch.setattr(
        turn_context,
        "build_meme_turn_context",
        lambda *_args, **_kwargs: MemeTurnContext(),
    )

    context = await manager._build_public_meme_turn_context("这是一个暂未入库的新梗")

    assert context == ""
