"""Concurrency-safe persistence primitives for recommendation data."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from typing import Any, TypeVar

import portalocker


StoredValue = TypeVar("StoredValue")
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


def resolve_persistence_path(
    *,
    explicit_path: str | os.PathLike[str] | None,
    config_directory: str | os.PathLike[str] | None,
    filename: str,
) -> Path | None:
    if explicit_path is not None:
        return Path(explicit_path)
    if config_directory is None:
        return None
    return Path(config_directory) / filename


@contextmanager
def locked_path(
    target_path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
):
    """Lock a sidecar path so replacing the target never drops the lock."""
    resolved_target_path = Path(target_path)
    resolved_target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = resolved_target_path.with_name(resolved_target_path.name + ".lock")
    with portalocker.Lock(str(lock_path), mode="a", timeout=timeout):
        yield resolved_target_path


class AtomicJsonStore:
    """Atomic JSON persistence with a lock covering a full update transaction."""

    def __init__(
        self,
        target_path: str | os.PathLike[str],
        *,
        default_factory: Callable[[], StoredValue],
        sanitizer: Callable[[Any], StoredValue],
    ) -> None:
        self.target_path = Path(target_path)
        self._default_factory = default_factory
        self._sanitizer = sanitizer

    @property
    def path(self) -> Path:
        return self.target_path

    def read(self) -> StoredValue:
        with locked_path(self.target_path):
            return self._read_unlocked()

    def write(self, value: Any) -> StoredValue:
        with locked_path(self.target_path):
            sanitized_value = self._sanitizer(value)
            self._write_unlocked(sanitized_value)
            return sanitized_value

    def update(
        self,
        mutator: Callable[[StoredValue], StoredValue | None],
    ) -> StoredValue:
        with locked_path(self.target_path):
            current_value = self._read_unlocked()
            updated_value = mutator(current_value)
            sanitized_value = self._sanitizer(
                current_value if updated_value is None else updated_value
            )
            self._write_unlocked(sanitized_value)
            return sanitized_value

    def delete(self) -> bool:
        with locked_path(self.target_path):
            if self.target_path.exists():
                self.target_path.unlink()
            return True

    def _read_unlocked(self) -> StoredValue:
        if not self.target_path.exists():
            return self._default_factory()
        try:
            with self.target_path.open("r", encoding="utf-8") as input_stream:
                return self._sanitizer(json.load(input_stream))
        except (OSError, ValueError, TypeError):
            return self._default_factory()

    def _write_unlocked(self, value: Mapping[str, Any] | Any) -> None:
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_suffix = f".tmp.{os.getpid()}.{threading.get_ident()}"
        temporary_path = self.target_path.with_name(
            self.target_path.name + temporary_suffix
        )
        try:
            with temporary_path.open("w", encoding="utf-8") as output_stream:
                json.dump(
                    value,
                    output_stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                output_stream.write("\n")
                output_stream.flush()
                os.fsync(output_stream.fileno())
            os.replace(temporary_path, self.target_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class JsonlStore:
    """Lock-protected JSONL append, rotation, and tolerant loading."""

    def __init__(
        self,
        target_path: str | os.PathLike[str],
        *,
        sanitizer: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> None:
        self.target_path = Path(target_path)
        self._sanitizer = sanitizer

    @property
    def path(self) -> Path:
        return self.target_path

    def append(self, record: Mapping[str, Any], *, rotate_bytes: int) -> bool:
        sanitized_record = self._sanitizer(record)
        with locked_path(self.target_path):
            if (
                rotate_bytes > 0
                and self.target_path.exists()
                and self.target_path.stat().st_size > rotate_bytes
            ):
                rotated_path = self.target_path.with_name(self.target_path.name + ".1")
                os.replace(self.target_path, rotated_path)
            with self.target_path.open("a", encoding="utf-8") as output_stream:
                output_stream.write(
                    json.dumps(
                        sanitized_record,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                output_stream.flush()
            return True

    def load(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.target_path.exists():
            return []
        loaded_records: deque[dict[str, Any]] | list[dict[str, Any]]
        loaded_records = deque(maxlen=limit) if limit and limit > 0 else []
        with locked_path(self.target_path):
            if not self.target_path.exists():
                return []
            with self.target_path.open("r", encoding="utf-8") as input_stream:
                for line in input_stream:
                    try:
                        raw_record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw_record, Mapping):
                        loaded_records.append(self._sanitizer(raw_record))
        return list(loaded_records)


__all__ = [
    "AtomicJsonStore",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "JsonlStore",
    "locked_path",
    "resolve_persistence_path",
]
