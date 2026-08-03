"""Persistence for sanitized recommendation observations."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from pathlib import Path
from typing import Any

from main_logic.proactive_recommendation.persistence import JsonlStore
from .validation import sanitize_recommendation_observation

logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_observer")
OBSERVATION_LOG_FILENAME = "proactive_recommendation_observations.jsonl"
DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024


class ObservationStore:
    """Object interface for the sanitized observation JSONL stream."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(
        self,
        observation: Mapping[str, Any],
        *,
        rotate_bytes: int = DEFAULT_ROTATE_BYTES,
    ) -> bool:
        return append_recommendation_observation_jsonl(
            observation,
            log_mode="jsonl",
            path=self.path,
            rotate_bytes=rotate_bytes,
        )

    def load(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return load_recommendation_observations_jsonl(self.path, limit=limit)


def append_recommendation_observation_jsonl(
    observation: Mapping[str, Any],
    *,
    log_mode: str = "off",
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    rotate_bytes: int = DEFAULT_ROTATE_BYTES,
) -> bool:
    """Append one sanitized observation to a local JSONL file when enabled."""
    if log_mode != "jsonl":
        return False
    target = _resolve_observation_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        safe = sanitize_recommendation_observation(observation)
        if not str(safe.get("turn_id") or "").strip():
            logger.debug(
                "proactive recommendation observation rejected: missing turn_id"
            )
            return False
        if not str(safe.get("algorithm_version") or "").strip():
            logger.debug(
                "proactive recommendation observation rejected: missing algorithm_version"
            )
            return False
        return JsonlStore(
            target,
            sanitizer=sanitize_recommendation_observation,
        ).append(safe, rotate_bytes=rotate_bytes)
    except Exception as exc:
        logger.debug("proactive recommendation observation append failed: %s", exc)
        return False


def load_recommendation_observations_jsonl(
    path: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read observations from JSONL, returning the newest ``limit`` rows."""
    try:
        return JsonlStore(
            path,
            sanitizer=sanitize_recommendation_observation,
        ).load(limit=limit)
    except Exception as exc:
        logger.debug("proactive recommendation observation read failed: %s", exc)
        return []


def _resolve_observation_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    if config_dir is None:
        return None
    return Path(config_dir) / OBSERVATION_LOG_FILENAME
