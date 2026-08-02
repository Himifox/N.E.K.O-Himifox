"""Observation-only timing and fatigue state for proactive recommendations."""

from __future__ import annotations

from collections import deque
import math
import time


DELIVERY_TIMING_HISTORY_MAX = 512
DELIVERY_TIMING_MAX_AGE_SECONDS = 2 * 60 * 60

_delivery_timing_history: dict[str, deque[float]] = {}


def record_proactive_delivery_for_timing(
    lanlan_name: str,
    *,
    delivered_at: float | None = None,
) -> None:
    """Record a real proactive delivery without affecting recommendation logic."""
    name = str(lanlan_name or "").strip()
    if not name:
        return
    timestamp = time.time() if delivered_at is None else float(delivered_at)
    history = _delivery_timing_history.get(name)
    if history is None:
        history = deque(maxlen=DELIVERY_TIMING_HISTORY_MAX)
        _delivery_timing_history[name] = history
    history.append(timestamp)
    _prune_delivery_timing_history(history, now=timestamp)


def proactive_delivery_timing_snapshot(
    lanlan_name: str,
    *,
    configured_interval_seconds: object = None,
    now: float | None = None,
) -> dict[str, int | float | None]:
    """Freeze timing features before the current proactive turn can deliver."""
    current = time.time() if now is None else float(now)
    history = _delivery_timing_history.get(str(lanlan_name or "").strip())
    if history is not None:
        _prune_delivery_timing_history(history, now=current)
    timestamps = list(history or ())
    last_delivery = timestamps[-1] if timestamps else None
    elapsed = (
        max(0.0, current - last_delivery)
        if last_delivery is not None and last_delivery <= current
        else None
    )
    return {
        "configured_interval_seconds": _optional_nonnegative_seconds(
            configured_interval_seconds
        ),
        "elapsed_since_last_delivery_seconds": (
            round(elapsed, 3) if elapsed is not None else None
        ),
        "recent_delivery_count_30m": sum(
            0 <= current - timestamp <= 30 * 60 for timestamp in timestamps
        ),
        "recent_delivery_count_2h": sum(
            0 <= current - timestamp <= DELIVERY_TIMING_MAX_AGE_SECONDS
            for timestamp in timestamps
        ),
    }


def clear_proactive_delivery_timing_history() -> None:
    """Clear process-local timing state; intended for tests and clean shutdown."""
    _delivery_timing_history.clear()


def _prune_delivery_timing_history(
    history: deque[float],
    *,
    now: float,
) -> None:
    cutoff = now - DELIVERY_TIMING_MAX_AGE_SECONDS
    while history and history[0] < cutoff:
        history.popleft()


def _optional_nonnegative_seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)
