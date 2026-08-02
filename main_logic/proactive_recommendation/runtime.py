"""Process-local runtime safety switch for proactive recommendation.

Activation remains a developer startup decision through
``PROACTIVE_RECOMMENDATION_MODE``.  Runtime mutation is intentionally one-way:
the API may demote ``active_source`` to ``shadow`` immediately, but it can
never promote a process into active mode.
"""

from __future__ import annotations

from threading import RLock
import time
from typing import Any

from config import (
    PROACTIVE_RECOMMENDATION_BANDIT_MODE,
    PROACTIVE_RECOMMENDATION_MODE,
    PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE,
)


VALID_RECOMMENDATION_MODES = frozenset({"off", "shadow", "active_source"})


def _normalize_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_RECOMMENDATION_MODES else "shadow"


class RecommendationRuntimeState:
    """Small synchronized state machine with no runtime activation path."""

    def __init__(self, startup_mode: str) -> None:
        self._lock = RLock()
        self._startup_mode = _normalize_mode(startup_mode)
        self._effective_mode = self._startup_mode
        self._rollback_count = 0
        self._last_rollback_at: float | None = None
        self._last_rollback_reason: str | None = None

    def mode(self) -> str:
        with self._lock:
            return self._effective_mode

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured_mode": self._startup_mode,
                "effective_mode": self._effective_mode,
                "active_source_enabled": self._effective_mode == "active_source",
                "bandit_configured_mode": PROACTIVE_RECOMMENDATION_BANDIT_MODE,
                "bandit_canary_effective": (
                    self._effective_mode == "active_source"
                    and PROACTIVE_RECOMMENDATION_PERSONALIZATION_MODE == "active"
                    and PROACTIVE_RECOMMENDATION_BANDIT_MODE == "canary"
                ),
                "activation_source": "startup_environment_only",
                "runtime_activation_allowed": False,
                "rollback_available": self._effective_mode == "active_source",
                "rollback_target": "shadow",
                "rollback_count": self._rollback_count,
                "last_rollback_at": self._last_rollback_at,
                "last_rollback_reason": self._last_rollback_reason,
                "restart_restores_configured_mode": (
                    self._effective_mode != self._startup_mode
                ),
            }

    def rollback(
        self, *, reason: Any = None, now: float | None = None
    ) -> dict[str, Any]:
        clean_reason = str(reason or "developer_runtime_rollback").strip()[:120]
        with self._lock:
            previous = self._effective_mode
            applied = previous == "active_source"
            if applied:
                self._effective_mode = "shadow"
                self._rollback_count += 1
                self._last_rollback_at = time.time() if now is None else float(now)
                self._last_rollback_reason = (
                    clean_reason or "developer_runtime_rollback"
                )
            return {
                "applied": applied,
                "previous_mode": previous,
                "status": self.status(),
            }


_RUNTIME = RecommendationRuntimeState(PROACTIVE_RECOMMENDATION_MODE)


def get_recommendation_runtime_mode() -> str:
    return _RUNTIME.mode()


def get_recommendation_runtime_status() -> dict[str, Any]:
    return _RUNTIME.status()


def rollback_recommendation_runtime(*, reason: Any = None) -> dict[str, Any]:
    return _RUNTIME.rollback(reason=reason)


__all__ = [
    "RecommendationRuntimeState",
    "get_recommendation_runtime_mode",
    "get_recommendation_runtime_status",
    "rollback_recommendation_runtime",
]
