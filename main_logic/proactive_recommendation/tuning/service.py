"""Safe tuning layer for proactive recommendation scores.

This module stores feedback-derived score adjustments as local configuration.
It never edits source code, calls models, delivers messages, or bypasses gates.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from main_logic.proactive_recommendation.normalization import (
    clamp_to_range,
    coerce_float_or_default,
    normalize_source_identifier,
    to_stripped_text,
)
from main_logic.proactive_recommendation.persistence import resolve_persistence_path

from main_logic.proactive_recommendation.feedback.service import (
    FEEDBACK_LOG_FILENAME,
    load_recommendation_feedback_jsonl,
)
from main_logic.proactive_recommendation.feedback.analytics import (
    summarize_feedback_calibration,
)
from main_logic.proactive_recommendation.observation.analytics import (
    CALIBRATION_SAMPLE_LIMIT,
    CALIBRATION_WINDOW_SECONDS,
)
from main_logic.proactive_recommendation.observation.storage import (
    OBSERVATION_LOG_FILENAME,
    load_recommendation_observations_jsonl,
)
from main_logic.proactive_recommendation.tuning.configuration import (
    _new_default_tuning_health,
    _new_default_tuning_configuration,
    _normalize_tuning_mode,
    _coerce_optional_finite_float,
    _sanitize_tuning_health,
    _sanitize_source_adjustments,
    apply_recommendation_tuning_score,
    sanitize_recommendation_tuning,
    tuning_public_status,
)
from main_logic.proactive_recommendation.tuning.storage import (
    load_recommendation_tuning,
    reset_recommendation_tuning,
    save_recommendation_tuning,
)
from main_logic.proactive_recommendation.tuning.health_policy import (
    _apply_rollback,
    _auto_apply_blocked_reason,
    _calibration_signature,
    _calibration_snapshot,
    _has_auto_safe_negative_signal,
    _has_strong_music_positive_signal,
    _health_auto_apply_block_reason,
    _iter_calibration_suggestions,
    _rollback_reason,
    _source_average_feedback,
    _source_signal_stats,
    evaluate_recommendation_tuning_health,
    extract_auto_safe_feedback_suggestions,
)


logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_tuning")

TUNING_FILENAME = "proactive_recommendation_tuning.json"
TUNING_VERSION = 1
TUNING_MODES = {"off", "manual", "auto_safe"}
AUTO_TUNING_SOURCE_TYPES = {"news", "web", "video", "home", "music", "meme"}
AUTO_APPLY_MIN_FEEDBACK_COUNT = 30
AUTO_APPLY_MAX_NEGATIVE_RATE = 0.20
AUTO_APPLY_MAX_STEP = 0.02
AUTO_APPLY_MAX_ABS_ADJUSTMENT = 0.15
AUTO_APPLY_SOURCE_COOLDOWN_SECONDS = 3600
AUTO_APPLY_MIN_HIGH_CONFIDENCE_NEGATIVE_COUNT = 2
ROLLBACK_AVERAGE_DROP = 0.10
ROLLBACK_NEGATIVE_RATE_INCREASE = 0.10
HEALTH_STATUSES = {"healthy", "watch", "paused"}
HEALTH_PAUSE_SECONDS = 6 * 3600
HEALTH_BAD_WINDOWS_TO_PAUSE = 2


def maybe_auto_apply_recommendation_tuning_from_logs(
    *,
    mode: str,
    config_dir: str | os.PathLike[str] | None = None,
    tuning_path: str | os.PathLike[str] | None = None,
    observation_path: str | os.PathLike[str] | None = None,
    feedback_path: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    if _normalize_tuning_mode(mode) != "auto_safe":
        return {"applied": False, "reason": "mode_not_auto_safe"}
    obs_path = (
        Path(observation_path)
        if observation_path is not None
        else resolve_persistence_path(
            explicit_path=None,
            config_directory=config_dir,
            filename=OBSERVATION_LOG_FILENAME,
        )
    )
    fb_path = (
        Path(feedback_path)
        if feedback_path is not None
        else resolve_persistence_path(
            explicit_path=None,
            config_directory=config_dir,
            filename=FEEDBACK_LOG_FILENAME,
        )
    )
    if (
        obs_path is None
        or fb_path is None
        or not obs_path.exists()
        or not fb_path.exists()
    ):
        return {"applied": False, "reason": "logs_missing"}

    observations = load_recommendation_observations_jsonl(
        obs_path, limit=CALIBRATION_SAMPLE_LIMIT
    )
    feedback_events = load_recommendation_feedback_jsonl(
        fb_path, limit=CALIBRATION_SAMPLE_LIMIT * 4
    )
    calibration = summarize_feedback_calibration(
        observations,
        feedback_events,
        now=current,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    tuning = load_recommendation_tuning(path=tuning_path, config_dir=config_dir)
    auto_safe_suggestions = extract_auto_safe_feedback_suggestions(calibration)

    health_block = _health_auto_apply_block_reason(tuning, now=current)
    if health_block:
        return {
            "applied": False,
            "reason": health_block,
            "tuning": tuning_public_status(tuning),
        }

    rollback_reason = _rollback_reason(tuning, calibration, now=current)
    if rollback_reason:
        rolled_back = _apply_rollback(tuning, calibration, rollback_reason, now=current)
        saved = save_recommendation_tuning(
            rolled_back, path=tuning_path, config_dir=config_dir
        )
        return {
            "applied": saved,
            "rollback_applied": saved,
            "reason": rollback_reason,
            "tuning": tuning_public_status(rolled_back),
        }

    blocked_reason = _auto_apply_blocked_reason(
        calibration,
        auto_safe_suggestions=auto_safe_suggestions,
    )
    if blocked_reason:
        return {
            "applied": False,
            "reason": blocked_reason,
            "calibration": _calibration_snapshot(calibration),
        }

    current_adjustments = dict(tuning.get("source_type_adjustment") or {})
    previous_adjustments = dict(current_adjustments)
    source_last_applied = dict(tuning.get("source_last_applied_at") or {})
    applied: dict[str, float] = {}
    reasons_by_source: dict[str, list[str]] = {}

    for source, suggestion in auto_safe_suggestions.items():
        normalized = normalize_source_identifier(source)
        if normalized not in AUTO_TUNING_SOURCE_TYPES:
            continue
        last_applied = coerce_float_or_default(
            source_last_applied.get(normalized), default=0.0
        )
        if last_applied and current - last_applied < AUTO_APPLY_SOURCE_COOLDOWN_SECONDS:
            continue
        raw_adjustment = coerce_float_or_default(
            suggestion.get("adjustment") if isinstance(suggestion, Mapping) else 0.0,
            default=0.0,
        )
        step = clamp_to_range(raw_adjustment, -AUTO_APPLY_MAX_STEP, AUTO_APPLY_MAX_STEP)
        if step == 0:
            continue
        previous = coerce_float_or_default(
            current_adjustments.get(normalized), default=0.0
        )
        updated = round(
            clamp_to_range(
                previous + step,
                -AUTO_APPLY_MAX_ABS_ADJUSTMENT,
                AUTO_APPLY_MAX_ABS_ADJUSTMENT,
            ),
            3,
        )
        actual_step = round(updated - previous, 3)
        if actual_step == 0:
            continue
        current_adjustments[normalized] = updated
        source_last_applied[normalized] = current
        applied[normalized] = actual_step
        if isinstance(suggestion, Mapping):
            reasons_by_source[normalized] = [
                to_stripped_text(reason)
                for reason in (suggestion.get("reasons") or [])
                if to_stripped_text(reason)
            ]

    if not applied:
        return {
            "applied": False,
            "reason": "no_applicable_adjustments",
            "calibration": _calibration_snapshot(calibration),
        }

    updated_tuning = {
        **tuning,
        "enabled": True,
        "mode": "auto_safe",
        "source_type_adjustment": current_adjustments,
        "created_from": tuning.get("created_from") or "feedback_calibration",
        "sample_count": int(calibration.get("sample_count") or 0),
        "created_at": coerce_float_or_default(tuning.get("created_at"), default=current)
        or current,
        "updated_at": current,
        "auto_apply_count": int(tuning.get("auto_apply_count") or 0) + 1,
        "source_last_applied_at": source_last_applied,
        "last_calibration": _calibration_snapshot(calibration),
        "last_auto_apply": {
            "applied": True,
            "adjustments": applied,
            "reasons": sorted(
                {reason for reasons in reasons_by_source.values() for reason in reasons}
            ),
        },
        "rollback": {
            "previous_source_type_adjustment": previous_adjustments,
            "applied": False,
            "reason": None,
        },
    }
    saved = save_recommendation_tuning(
        updated_tuning, path=tuning_path, config_dir=config_dir
    )
    return {
        "applied": saved,
        "reason": None if saved else "save_failed",
        "adjustments": applied,
        "tuning": tuning_public_status(updated_tuning),
    }


def maybe_update_recommendation_tuning_health_from_logs(
    *,
    mode: str,
    config_dir: str | os.PathLike[str] | None = None,
    tuning_path: str | os.PathLike[str] | None = None,
    observation_path: str | os.PathLike[str] | None = None,
    feedback_path: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    if _normalize_tuning_mode(mode) != "auto_safe":
        return {"updated": False, "reason": "mode_not_auto_safe"}
    obs_path = (
        Path(observation_path)
        if observation_path is not None
        else resolve_persistence_path(
            explicit_path=None,
            config_directory=config_dir,
            filename=OBSERVATION_LOG_FILENAME,
        )
    )
    fb_path = (
        Path(feedback_path)
        if feedback_path is not None
        else resolve_persistence_path(
            explicit_path=None,
            config_directory=config_dir,
            filename=FEEDBACK_LOG_FILENAME,
        )
    )
    if (
        obs_path is None
        or fb_path is None
        or not obs_path.exists()
        or not fb_path.exists()
    ):
        return {"updated": False, "reason": "logs_missing"}
    observations = load_recommendation_observations_jsonl(
        obs_path, limit=CALIBRATION_SAMPLE_LIMIT
    )
    feedback_events = load_recommendation_feedback_jsonl(
        fb_path, limit=CALIBRATION_SAMPLE_LIMIT * 4
    )
    calibration = summarize_feedback_calibration(
        observations,
        feedback_events,
        now=current,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    tuning = load_recommendation_tuning(path=tuning_path, config_dir=config_dir)
    evaluated = evaluate_recommendation_tuning_health(tuning, calibration, now=current)
    if evaluated == sanitize_recommendation_tuning(tuning):
        return {
            "updated": False,
            "reason": "health_unchanged",
            "tuning": tuning_public_status(evaluated),
        }
    saved = save_recommendation_tuning(
        evaluated, path=tuning_path, config_dir=config_dir
    )
    return {
        "updated": saved,
        "reason": None if saved else "save_failed",
        "tuning": tuning_public_status(evaluated),
    }


def pause_recommendation_tuning(
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    now: float | None = None,
    duration_seconds: int = HEALTH_PAUSE_SECONDS,
    reason: str = "manual_pause",
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    tuning = load_recommendation_tuning(path=path, config_dir=config_dir)
    health = dict(tuning.get("health") or _new_default_tuning_health())
    health.update(
        {
            "status": "paused",
            "paused_until": current + max(0, int(duration_seconds)),
            "pause_reason": to_stripped_text(reason) or "manual_pause",
            "last_evaluation": {
                **dict(health.get("last_evaluation") or {}),
                "decision": "manual_pause",
            },
        }
    )
    updated = {**tuning, "health": health, "updated_at": current}
    save_recommendation_tuning(updated, path=path, config_dir=config_dir)
    return tuning_public_status(updated)


def resume_recommendation_tuning(
    *,
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    tuning = load_recommendation_tuning(path=path, config_dir=config_dir)
    health = dict(tuning.get("health") or _new_default_tuning_health())
    health.update(
        {
            "status": "healthy",
            "bad_window_count": 0,
            "paused_until": None,
            "pause_reason": None,
            "last_evaluation": {
                **dict(health.get("last_evaluation") or {}),
                "decision": "manual_resume",
            },
        }
    )
    updated = {**tuning, "health": health, "updated_at": current}
    save_recommendation_tuning(updated, path=path, config_dir=config_dir)
    return tuning_public_status(updated)


class TuningService:
    """Application-facing tuning orchestration without feedback-side coupling."""

    def status(self, *, config_dir: Any) -> dict[str, Any]:
        return tuning_public_status(load_recommendation_tuning(config_dir=config_dir))

    def maybe_auto_apply_from_logs(self, **kwargs: Any) -> dict[str, Any]:
        return maybe_auto_apply_recommendation_tuning_from_logs(**kwargs)

    def update_health_from_logs(self, **kwargs: Any) -> dict[str, Any]:
        return maybe_update_recommendation_tuning_health_from_logs(**kwargs)

    def pause(self, **kwargs: Any) -> dict[str, Any]:
        return pause_recommendation_tuning(**kwargs)

    def resume(self, **kwargs: Any) -> dict[str, Any]:
        return resume_recommendation_tuning(**kwargs)

    def reset(self, *, config_dir: Any) -> bool:
        return reset_recommendation_tuning(config_dir=config_dir)
