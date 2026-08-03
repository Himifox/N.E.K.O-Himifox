"""Persistence for sanitized recommendation feedback events."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from pathlib import Path
from typing import Any

from main_logic.proactive_recommendation.persistence import JsonlStore
from .events import sanitize_recommendation_feedback_event

logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_feedback")
FEEDBACK_LOG_FILENAME = "proactive_recommendation_feedback.jsonl"
DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024


def append_recommendation_feedback_jsonl(
    event: Mapping[str, Any],
    *,
    log_mode: str = "off",
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    rotate_bytes: int = DEFAULT_ROTATE_BYTES,
) -> bool:
    if log_mode != "jsonl":
        return False
    target = _resolve_feedback_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        return JsonlStore(
            target,
            sanitizer=sanitize_recommendation_feedback_event,
        ).append(event, rotate_bytes=rotate_bytes)
    except Exception as exc:
        logger.debug("proactive recommendation feedback append failed: %s", exc)
        return False


def load_recommendation_feedback_jsonl(
    path: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    try:
        return JsonlStore(
            path,
            sanitizer=sanitize_recommendation_feedback_event,
        ).load(limit=limit)
    except Exception as exc:
        logger.debug("proactive recommendation feedback read failed: %s", exc)
        return []


def _resolve_feedback_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    if config_dir is None:
        return None
    return Path(config_dir) / FEEDBACK_LOG_FILENAME
