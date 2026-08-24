from __future__ import annotations

import asyncio
import time

import pytest

from knowledge import indexer
from knowledge.vector_index import _KnowledgeInferenceCoordinator


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

    async def fake_drain(*, deadline_monotonic=None) -> bool:
        assert deadline_monotonic is not None
        cleanup_order.append("drain")
        return True

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
    assert await indexer.stop_knowledge_indexer() is True
    assert cleanup_order == ["drain", "release"]


@pytest.mark.asyncio
async def test_indexer_shutdown_abandons_cancellation_resistant_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    started = asyncio.Event()
    release_task = asyncio.Event()
    released_model = False

    async def stubborn_run(_root, _wake_event: asyncio.Event) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_task.wait()

    async def fake_release() -> None:
        nonlocal released_model
        released_model = True

    monkeypatch.setattr(indexer, "_run_indexer", stubborn_run)
    monkeypatch.setattr(indexer, "INDEXER_CANCEL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        "utils.local_embedding_runtime.release_local_embedding_service",
        fake_release,
    )

    assert indexer.start_knowledge_indexer(tmp_path) is True
    task = indexer._TASK
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)

    before = time.monotonic()
    assert await indexer.stop_knowledge_indexer(timeout_seconds=0.02) is False
    assert time.monotonic() - before < 0.5
    assert released_model is False
    assert not task.done()

    release_task.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_indexer_shutdown_skips_release_when_inference_does_not_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released_model = False

    async def fake_drain(*, deadline_monotonic=None) -> bool:
        assert deadline_monotonic is not None
        await asyncio.sleep(0)
        return False

    async def fake_release() -> None:
        nonlocal released_model
        released_model = True

    monkeypatch.setattr(
        "knowledge.vector_index.drain_knowledge_embedding_inference",
        fake_drain,
    )
    monkeypatch.setattr(
        "utils.local_embedding_runtime.release_local_embedding_service",
        fake_release,
    )

    assert await indexer.stop_knowledge_indexer(timeout_seconds=0.02) is False
    assert released_model is False


@pytest.mark.asyncio
async def test_indexer_shutdown_abandons_stuck_model_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def fake_drain(*, deadline_monotonic=None) -> bool:
        return True

    async def stuck_release() -> None:
        release_started.set()
        await allow_release.wait()

    monkeypatch.setattr(
        "knowledge.vector_index.drain_knowledge_embedding_inference",
        fake_drain,
    )
    monkeypatch.setattr(
        "utils.local_embedding_runtime.release_local_embedding_service",
        stuck_release,
    )

    before = time.monotonic()
    assert await indexer.stop_knowledge_indexer(timeout_seconds=0.02) is False
    assert time.monotonic() - before < 0.5
    assert release_started.is_set()

    allow_release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_inference_drain_timeout_keeps_native_task_running() -> None:
    coordinator = _KnowledgeInferenceCoordinator()
    inference_started = asyncio.Event()
    allow_inference = asyncio.Event()

    async def native_inference() -> object:
        inference_started.set()
        await allow_inference.wait()
        return object()

    task = coordinator._start(native_inference, kind="background")
    assert task is not None
    await asyncio.wait_for(inference_started.wait(), timeout=1.0)

    drained = await coordinator.drain(
        deadline_monotonic=time.monotonic() + 0.01,
    )

    assert drained is False
    assert not task.cancelled()
    assert coordinator.active_kind() == "background"

    allow_inference.set()
    await asyncio.wait_for(task, timeout=1.0)
    await asyncio.sleep(0)
    assert coordinator.active_kind() == ""


def test_indexer_work_limits_are_bounded() -> None:
    assert indexer.STARTUP_DELAY_SECONDS == 45.0
    assert indexer.BACKLOG_DELAY_SECONDS == 30.0
    assert indexer.EMBEDDING_BATCH_SIZE == 4
    assert indexer.MAX_CHUNKS_PER_ROUND == 8


@pytest.mark.asyncio
async def test_indexer_initialization_failure_is_retrieved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(indexer, "STARTUP_DELAY_SECONDS", 0.0)

    def fail_to_open(_root):
        raise ValueError("legacy migration conflict")

    monkeypatch.setattr(
        "knowledge.service.KnowledgeService.from_root",
        fail_to_open,
    )

    await indexer._run_indexer(tmp_path, asyncio.Event())
