"""Concurrency-safe JSONL storage with lock-protected rotation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
from typing import Any

from .atomic_json import locked_path


class JsonlStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        sanitizer: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> None:
        self.path = Path(path)
        self._sanitizer = sanitizer

    def append(self, record: Mapping[str, Any], *, rotate_bytes: int) -> bool:
        safe = self._sanitizer(record)
        with locked_path(self.path):
            if (
                rotate_bytes > 0
                and self.path.exists()
                and self.path.stat().st_size > rotate_bytes
            ):
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n"
                )
                stream.flush()
            return True

    def load(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: deque[dict[str, Any]] | list[dict[str, Any]]
        rows = deque(maxlen=limit) if limit and limit > 0 else []
        with locked_path(self.path):
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, Mapping):
                        rows.append(self._sanitizer(item))
        return list(rows)
