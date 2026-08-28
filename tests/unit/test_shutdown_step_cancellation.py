"""Cancellation during one shutdown step must not skip the remaining cleanups."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_step_defers_cancellation_and_lets_later_steps_run() -> None:
    from app.main_server import _run_shutdown_step

    ran: list[str] = []

    async def cancelled_step() -> None:
        raise asyncio.CancelledError()

    async def later_step() -> None:
        ran.append("later")

    pending = await _run_shutdown_step(cancelled_step(), what="first")
    assert isinstance(pending, asyncio.CancelledError)

    # The whole point: the next cleanup still runs.
    pending = await _run_shutdown_step(
        later_step(), what="second", pending_cancellation=pending
    )
    assert ran == ["later"]
    assert isinstance(pending, asyncio.CancelledError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_step_keeps_the_first_cancellation() -> None:
    from app.main_server import _run_shutdown_step

    first = asyncio.CancelledError()

    async def cancelled_step() -> None:
        raise asyncio.CancelledError()

    pending = await _run_shutdown_step(
        cancelled_step(), what="second", pending_cancellation=first
    )
    assert pending is first


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_step_passes_through_success() -> None:
    from app.main_server import _run_shutdown_step

    async def ok_step() -> None:
        return None

    assert await _run_shutdown_step(ok_step(), what="ok") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_step_clears_the_cancelling_counter() -> None:
    """After absorbing a real cancel, ``cancelling()`` must be back to zero.

    This is not an incidental detail: ``_cancel_task_if_running`` decides whether
    to re-raise by reading exactly this counter
    (``if current is not None and current.cancelling(): raise``). Leave it above
    zero and every later shutdown step that goes through that helper re-raises
    immediately, which is the same skipped-cleanup bug from the other direction.
    """
    from app.main_server import _run_shutdown_step

    entered = asyncio.Event()

    async def shutdown_like() -> int:
        async def blocking_step() -> None:
            entered.set()
            await asyncio.sleep(3600)

        await _run_shutdown_step(blocking_step(), what="blocking")
        current = asyncio.current_task()
        assert current is not None
        return current.cancelling()

    task = asyncio.create_task(shutdown_like())
    await entered.wait()
    task.cancel()
    assert await task == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_later_cancel_helper_still_runs_after_an_absorbed_cancel() -> None:
    """The counter reset must actually keep the *next* helper call working.

    Dual of the test above, at the call site rather than on the predicate: a
    second ``_cancel_task_if_running`` after an absorbed cancellation has to
    complete its own cleanup instead of re-raising straight back out.
    """
    from app.main_server import _cancel_task_if_running, _run_shutdown_step

    ran: list[str] = []
    entered = asyncio.Event()

    async def shutdown_like() -> None:
        async def blocking_step() -> None:
            entered.set()
            await asyncio.sleep(3600)

        pending = await _run_shutdown_step(blocking_step(), what="blocking")

        victim_started = asyncio.Event()

        async def victim() -> None:
            victim_started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                ran.append("victim-stopped")

        victim_task = asyncio.create_task(victim())
        await victim_started.wait()
        await _cancel_task_if_running(victim_task, name="victim", timeout=1.0)
        ran.append("reached-end")
        assert pending is not None

    task = asyncio.create_task(shutdown_like())
    await entered.wait()
    task.cancel()
    await task
    assert ran == ["victim-stopped", "reached-end"]


@pytest.mark.unit
def test_on_shutdown_routes_every_await_through_the_guard() -> None:
    """No bare ``await`` of a cleanup helper may remain in ``on_shutdown``.

    The regression this covers: ``_cancel_task_if_running`` grew a re-raise, and
    the two *new* call sites were guarded while the pre-existing game-cleanup
    await was not, so a cancellation there skipped connector, ZMQ, Cloud Save,
    HTTP-pool and knowledge-indexer cleanup.
    """
    import ast
    import inspect
    import textwrap

    from app.main_server import on_shutdown

    source = textwrap.dedent(inspect.getsource(on_shutdown))
    tree = ast.parse(source)

    guarded_calls = {
        "_cancel_task_if_running",
        "close_voice_identity_runtime",
        "join_sync_connector_threads",
        "_stop_neko_servers_integration_workers",
    }
    unguarded: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name in guarded_calls:
            unguarded.append(name)

    assert unguarded == [], (
        "these cleanups are awaited directly instead of through "
        "_run_shutdown_step, so a cancellation there skips the rest of "
        f"shutdown: {sorted(set(unguarded))}"
    )
