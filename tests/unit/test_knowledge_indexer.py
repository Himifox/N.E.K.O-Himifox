from __future__ import annotations

import asyncio

import pytest

from knowledge import indexer


@pytest.mark.asyncio
async def test_indexer_lifecycle_is_idempotent_and_wakeable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    started = asyncio.Event()
    awakened = asyncio.Event()
    cleanup_order: list[str] = []

    async def fake_run(_root, wake_event: asyncio.Event) -> None:
        started.set()
        await wake_event.wait()
        awakened.set()
        await asyncio.Event().wait()

    async def fake_release() -> None:
        cleanup_order.append("release")

    async def fake_drain() -> None:
        cleanup_order.append("drain")

    monkeypatch.setattr(indexer, "_run_indexer", fake_run)
    monkeypatch.setattr(
        "utils.local_embedding_runtime.release_local_embedding_service",
        fake_release,
    )
    monkeypatch.setattr(
        "knowledge.vector_index.drain_knowledge_embedding_inference",
        fake_drain,
    )

    assert indexer.start_knowledge_indexer(tmp_path) is True
    assert indexer.start_knowledge_indexer(tmp_path) is False
    await asyncio.wait_for(started.wait(), timeout=1.0)

    indexer.notify_knowledge_index_changed()
    await asyncio.wait_for(awakened.wait(), timeout=1.0)
    await indexer.stop_knowledge_indexer()
    assert cleanup_order == ["drain", "release"]


def test_indexer_work_limits_are_bounded() -> None:
    assert indexer.STARTUP_DELAY_SECONDS == 45.0
    assert indexer.EMBEDDING_BATCH_SIZE == 4
    assert indexer.EMBEDDING_BATCH_SIZE <= indexer.MAX_CHUNKS_PER_ROUND <= 64
