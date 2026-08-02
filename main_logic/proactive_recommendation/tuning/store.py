"""Persistence for recommendation tuning state."""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path
from typing import Any

from .model import _default_tuning, sanitize_recommendation_tuning

logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_tuning")
TUNING_FILENAME = "proactive_recommendation_tuning.json"


def load_recommendation_tuning(
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    target = _resolve_tuning_path(path=path, config_dir=config_dir)
    if target is None or not target.exists():
        return _default_tuning()
    try:
        with target.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        logger.debug("proactive recommendation tuning load failed: %s", exc)
        return _default_tuning()
    return sanitize_recommendation_tuning(payload)


def save_recommendation_tuning(
    tuning: Mapping[str, Any],
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> bool:
    target = _resolve_tuning_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe = sanitize_recommendation_tuning(tuning)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(safe, fh, ensure_ascii=False, sort_keys=True, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
        return True
    except Exception as exc:
        logger.debug("proactive recommendation tuning save failed: %s", exc)
        return False


def reset_recommendation_tuning(
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> bool:
    target = _resolve_tuning_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        if target.exists():
            target.unlink()
        return True
    except Exception as exc:
        logger.debug("proactive recommendation tuning reset failed: %s", exc)
        return False


def _resolve_tuning_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    return _config_file(config_dir, TUNING_FILENAME)


def _config_file(
    config_dir: str | os.PathLike[str] | None, filename: str
) -> Path | None:
    if config_dir is None:
        return None
    return Path(config_dir) / filename
