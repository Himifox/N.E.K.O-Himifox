"""Admission, tracking, and cross-process fencing for knowledge writers."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import portalocker


_T = TypeVar("_T")
_STATE_LOCK = threading.Lock()
_ADMISSION_OPEN = True
_WRITERS: set[asyncio.Task[object]] = set()
_BARRIERS: dict[str, threading.RLock] = {}
ROOT_BARRIER_TIMEOUT_SECONDS = 30.0


class KnowledgeMutationAdmissionClosed(RuntimeError):
    """Raised when a writer races with the shutdown admission barrier."""


def _canonical_root_key(knowledge_root: str | Path) -> str:
    root = Path(knowledge_root).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.abspath(str(root)))


def _root_lock_path(knowledge_root: str | Path) -> Path:
    root = Path(_canonical_root_key(knowledge_root))
    root_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return root.parent / "state" / "knowledge-root-locks" / f"{root_id}.lock"


@contextmanager
def knowledge_root_barrier(
    knowledge_root: str | Path,
    *,
    timeout: float = ROOT_BARRIER_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the stable lock that serializes writers with root migration.

    The lock lives beside, rather than inside, ``knowledge/`` so moving or
    replacing the knowledge directory cannot silently replace the lock.
    """

    key = _canonical_root_key(knowledge_root)
    with _STATE_LOCK:
        thread_lock = _BARRIERS.setdefault(key, threading.RLock())
    lock_path = _root_lock_path(key)
    with thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(
            lock_path,
            mode="a",
            timeout=max(float(timeout), 0.0),
        ):
            yield


def _run_under_root_barrier(
    knowledge_root: str | Path,
    action: Callable[..., _T],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> _T:
    with knowledge_root_barrier(knowledge_root):
        return action(*args, **kwargs)


def _writer_finished(task: asyncio.Task[object]) -> None:
    with _STATE_LOCK:
        _WRITERS.discard(task)
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def run_knowledge_writer(
    knowledge_root: str | Path,
    action: Callable[..., _T],
    /,
    *args: object,
    **kwargs: object,
) -> _T:
    """Admit one off-loop writer and retain it until its real thread returns.

    Cancelling the request or indexer coordinator only stops awaiting the
    worker.  The task remains strongly referenced and continues occupying a
    shutdown slot until ``asyncio.to_thread`` has actually finished.
    """

    loop = asyncio.get_running_loop()
    with _STATE_LOCK:
        if not _ADMISSION_OPEN:
            raise KnowledgeMutationAdmissionClosed("knowledge_mutation_stopping")
        task = loop.create_task(
            asyncio.to_thread(
                _run_under_root_barrier,
                knowledge_root,
                action,
                tuple(args),
                dict(kwargs),
            ),
            name=f"knowledge-writer:{getattr(action, '__name__', 'mutation')}",
        )
        _WRITERS.add(task)
        task.add_done_callback(_writer_finished)
    return await asyncio.shield(task)


def open_knowledge_writer_admission() -> None:
    """Open writer admission for a newly started main-server runtime."""

    global _ADMISSION_OPEN
    with _STATE_LOCK:
        _ADMISSION_OPEN = True


def request_knowledge_writer_stop() -> tuple[asyncio.Task[object], ...]:
    """Close admission atomically and snapshot all real in-flight writers."""

    global _ADMISSION_OPEN
    with _STATE_LOCK:
        _ADMISSION_OPEN = False
        return tuple(_WRITERS)


async def finish_knowledge_writer_stop(*, deadline_monotonic: float) -> bool:
    """Wait until every admitted writer really returns or the deadline passes."""

    while True:
        with _STATE_LOCK:
            pending = {task for task in _WRITERS if not task.done()}
        if not pending:
            return True
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return False
        done, _pending = await asyncio.wait(pending, timeout=remaining)
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
        if not done:
            return False


def knowledge_writer_state() -> tuple[bool, int]:
    """Return bounded process-local state for diagnostics and regression tests."""

    with _STATE_LOCK:
        return _ADMISSION_OPEN, sum(not task.done() for task in _WRITERS)
