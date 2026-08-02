"""Thread-safe process-local history of shadow recommendation selections."""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Any


_SHADOW_HISTORY_MAX = 20
_shadow_history: dict[str, deque[dict[str, Any]]] = {}
_shadow_history_lock = threading.RLock()


def record_shadow_selection(
    lanlan_name: str,
    decision: Any,
    *,
    now: float | None = None,
) -> None:
    selected = getattr(decision, "selected_candidate", None)
    source_type = getattr(selected, "source_type", None)
    candidate_id = getattr(selected, "id", None)
    if not source_type and not candidate_id:
        return
    with _shadow_history_lock:
        history = _shadow_history.setdefault(
            lanlan_name,
            deque(maxlen=_SHADOW_HISTORY_MAX),
        )
        history.append(
            {
                "ts": time.time() if now is None else now,
                "source_type": str(source_type or ""),
                "candidate_id": str(candidate_id or ""),
            }
        )


def recent_shadow_values(lanlan_name: str, field_name: str) -> list[str]:
    with _shadow_history_lock:
        return [
            str(item.get(field_name) or "")
            for item in _shadow_history.get(lanlan_name, ())
            if item.get(field_name)
        ]


def recent_shadow_sources(lanlan_name: str) -> list[str]:
    return recent_shadow_values(lanlan_name, "source_type")


def recent_shadow_candidate_ids(lanlan_name: str) -> list[str]:
    return recent_shadow_values(lanlan_name, "candidate_id")
