"""Read-only safe views for recommendation freezes, reviews, and logs."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from main_logic.proactive_recommendation_feedback import (
    has_forbidden_feedback_fields,
    sanitize_recommendation_feedback_event,
)
from main_logic.proactive_recommendation_observer import (
    sanitize_recommendation_observation,
)
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_timing_audit import (
    prepare_observation_for_timing_import,
)


class RecommendationSafeExportError(ValueError):
    """Raised when a source artifact cannot produce a safe derived view."""


def prepare_recommendation_safe_view(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Return a sanitized deep copy without changing the source artifact.

    Artifact-level review and annotation structures are preserved. Only raw
    observation and feedback rows pass through the production sanitizers.
    """
    if not isinstance(artifact, Mapping):
        raise RecommendationSafeExportError("artifact must be a JSON object")
    view = deepcopy(dict(artifact))
    if "observations" in artifact:
        view["observations"] = _prepare_observations(artifact.get("observations"))
    if "feedback" in artifact:
        view["feedback"] = _prepare_feedback(artifact.get("feedback"))
    return view


def write_new_recommendation_safe_export(
    path: str | Path,
    prepared_view: Mapping[str, Any],
) -> Path:
    """Atomically write a new derived export and never replace an existing file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.touch(exist_ok=False)
    except FileExistsError as exc:
        raise RecommendationSafeExportError(
            f"safe export target already exists: {target}"
        ) from exc
    try:
        atomic_write_json(target, dict(prepared_view))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def read_recommendation_safe_export(path: str | Path) -> dict[str, Any]:
    """Read one derived JSON export without mutating or re-sanitizing it."""
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecommendationSafeExportError(
            f"cannot read recommendation safe export: {source}"
        ) from exc
    if not isinstance(value, Mapping):
        raise RecommendationSafeExportError("safe export must be a JSON object")
    return dict(value)


def _prepare_observations(value: Any) -> list[dict[str, Any]]:
    rows = _mapping_rows(value, field="observations")
    safe: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        prepared = prepare_observation_for_timing_import(
            row,
            sanitize_recommendation_observation,
        )
        if not prepared["accepted"]:
            raise RecommendationSafeExportError(
                f"observations[{index}] rejected: {prepared['reason']}"
            )
        safe.append(prepared["observation"])
    return safe


def _prepare_feedback(value: Any) -> list[dict[str, Any]]:
    rows = _mapping_rows(value, field="feedback")
    safe: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if has_forbidden_feedback_fields(row):
            raise RecommendationSafeExportError(
                f"feedback[{index}] contains forbidden sensitive fields"
            )
        safe.append(sanitize_recommendation_feedback_event(row))
    return safe


def _mapping_rows(value: Any, *, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecommendationSafeExportError(f"{field} must be a list")
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise RecommendationSafeExportError(f"{field}[{index}] must be an object")
        rows.append(row)
    return rows


__all__ = [
    "RecommendationSafeExportError",
    "prepare_recommendation_safe_view",
    "read_recommendation_safe_export",
    "write_new_recommendation_safe_export",
]
