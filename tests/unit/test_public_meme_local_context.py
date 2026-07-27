from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge.moegirl_knowledge.turn_context import MemeTurnContext
from main_logic.core.streaming import StreamingMixin


class _Manager(StreamingMixin):
    pass


@pytest.mark.asyncio
async def test_local_miss_never_invokes_a_model_or_network_fallback(monkeypatch):
    """Ordinary chat must stay on the local path after a knowledge miss."""
    import knowledge.api as knowledge_api

    manager = _Manager()
    monkeypatch.setattr(
        knowledge_api,
        "open_knowledge",
        lambda *_args, **_kwargs: SimpleNamespace(
            build_conversation_context=lambda *_args, **_kwargs: MemeTurnContext(),
        ),
    )

    context = await manager._build_public_meme_turn_context("这是一个暂未入库的新梗")

    assert context == ""


@pytest.mark.asyncio
async def test_weak_short_context_logs_its_match_mode(monkeypatch):
    import knowledge.api as knowledge_api
    import main_logic.core.streaming as streaming_module

    manager = _Manager()
    log_calls = []
    monkeypatch.setattr(
        knowledge_api,
        "open_knowledge",
        lambda *_args, **_kwargs: SimpleNamespace(
            build_conversation_context=lambda *_args, **_kwargs: MemeTurnContext(
                text="temporary card",
                hit_count=1,
                match_mode="weak_short",
                collection_id="meme",
            ),
        ),
    )
    monkeypatch.setattr(
        streaming_module.logger,
        "info",
        lambda message, *args: log_calls.append((message, args)),
    )

    context = await manager._build_public_meme_turn_context("越改越上头")

    assert context == "temporary card"
    assert log_calls == [(
        "[public-knowledge] automatic turn context hits=%d mode=%s collection=%s",
        (1, "weak_short", "meme"),
    )]
