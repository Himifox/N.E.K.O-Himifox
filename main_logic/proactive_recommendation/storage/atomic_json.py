"""Cross-thread and cross-process primitives for local JSON state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from typing import Any, TypeVar

import portalocker


T = TypeVar("T")
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


@contextmanager
def locked_path(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
):
    """Lock a stable sidecar so replacing the target never drops the lock."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with portalocker.Lock(str(lock_path), mode="a", timeout=timeout):
        yield target


class AtomicJsonStore:
    """Atomic JSON persistence with a lock covering the full update transaction."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        default_factory: Callable[[], T],
        sanitizer: Callable[[Any], T],
    ) -> None:
        self.path = Path(path)
        self._default_factory = default_factory
        self._sanitizer = sanitizer

    def read(self) -> T:
        with locked_path(self.path):
            return self._read_unlocked()

    def write(self, value: Any) -> T:
        with locked_path(self.path):
            safe = self._sanitizer(value)
            self._write_unlocked(safe)
            return safe

    def update(self, mutator: Callable[[T], T | None]) -> T:
        with locked_path(self.path):
            current = self._read_unlocked()
            updated = mutator(current)
            safe = self._sanitizer(current if updated is None else updated)
            self._write_unlocked(safe)
            return safe

    def delete(self) -> bool:
        with locked_path(self.path):
            if self.path.exists():
                self.path.unlink()
            return True

    def _read_unlocked(self) -> T:
        if not self.path.exists():
            return self._default_factory()
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                return self._sanitizer(json.load(stream))
        except (OSError, ValueError, TypeError):
            return self._default_factory()

    def _write_unlocked(self, value: Mapping[str, Any] | Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        suffix = f".tmp.{os.getpid()}.{threading.get_ident()}"
        temporary = self.path.with_name(self.path.name + suffix)
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
