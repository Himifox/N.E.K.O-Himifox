"""Persistence for recommendation tuning state."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from main_logic.proactive_recommendation.persistence import (
    AtomicJsonStore,
    resolve_persistence_path,
)

from .configuration import (
    _new_default_tuning_configuration,
    sanitize_recommendation_tuning,
)


TUNING_FILENAME = "proactive_recommendation_tuning.json"


def load_recommendation_tuning(
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    store = _tuning_store(path=path, config_dir=config_dir)
    return store.read() if store is not None else _new_default_tuning_configuration()


def save_recommendation_tuning(
    tuning: Mapping[str, Any],
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> bool:
    store = _tuning_store(path=path, config_dir=config_dir)
    if store is None:
        return False
    store.write(tuning)
    return True


def reset_recommendation_tuning(
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> bool:
    store = _tuning_store(path=path, config_dir=config_dir)
    return store.delete() if store is not None else False


def _tuning_store(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> AtomicJsonStore[dict[str, Any]] | None:
    target_path = resolve_persistence_path(
        explicit_path=path,
        config_directory=config_dir,
        filename=TUNING_FILENAME,
    )
    if target_path is None:
        return None
    return AtomicJsonStore(
        target_path,
        default_factory=_new_default_tuning_configuration,
        sanitizer=sanitize_recommendation_tuning,
    )
