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

from main_logic.proactive_recommendation_feedback import (
    FEEDBACK_LOG_FILENAME,
    load_recommendation_feedback_jsonl,
    summarize_feedback_calibration,
)
from main_logic.proactive_recommendation_observer import (
    CALIBRATION_SAMPLE_LIMIT,
    CALIBRATION_WINDOW_SECONDS,
    OBSERVATION_LOG_FILENAME,
    load_recommendation_observations_jsonl,
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


def sanitize_recommendation_tuning(tuning: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(tuning, Mapping):
        tuning = {}
    source_adjustments = _sanitize_source_adjustments(tuning.get("source_type_adjustment"))
    mode = _mode(tuning.get("mode"))
    enabled = bool(tuning.get("enabled")) and mode != "off"
    created_at = _number(tuning.get("created_at"), 0.0)
    updated_at = _number(tuning.get("updated_at"), created_at)
    return {
        "version": TUNING_VERSION,
        "enabled": enabled,
        "mode": mode,
        "source_type_adjustment": source_adjustments,
        "created_from": _clean_text(tuning.get("created_from")) or None,
        "sample_count": max(0, int(_number(tuning.get("sample_count"), 0))),
        "created_at": created_at,
        "updated_at": updated_at,
        "auto_apply_count": max(0, int(_number(tuning.get("auto_apply_count"), 0))),
        "source_last_applied_at": _sanitize_source_timestamps(tuning.get("source_last_applied_at")),
        "last_calibration": _sanitize_calibration_snapshot(tuning.get("last_calibration")),
        "last_auto_apply": _sanitize_last_auto_apply(tuning.get("last_auto_apply")),
        "rollback": _sanitize_rollback(tuning.get("rollback")),
        "health": _sanitize_health(tuning.get("health")),
    }


def tuning_public_status(tuning: Mapping[str, Any] | None) -> dict[str, Any]:
    safe = sanitize_recommendation_tuning(tuning)
    return {
        "version": safe["version"],
        "enabled": safe["enabled"],
        "mode": safe["mode"],
        "source_type_adjustment": dict(safe["source_type_adjustment"]),
        "created_from": safe["created_from"],
        "sample_count": safe["sample_count"],
        "created_at": safe["created_at"],
        "updated_at": safe["updated_at"],
        "auto_apply_count": safe["auto_apply_count"],
        "last_calibration": dict(safe["last_calibration"]),
        "last_auto_apply": dict(safe["last_auto_apply"]),
        "rollback": dict(safe["rollback"]),
        "health": dict(safe["health"]),
    }


def apply_recommendation_tuning_score(
    score: Any,
    source_type: Any,
    *,
    tuning: Mapping[str, Any] | None = None,
    adjustments: Mapping[str, Any] | None = None,
) -> tuple[float, float]:
    base = _number(score, 0.0)
    source = _normalize_source(source_type)
    if adjustments is None and isinstance(tuning, Mapping):
        safe = sanitize_recommendation_tuning(tuning)
        if safe["enabled"] and safe["mode"] in {"manual", "auto_safe"}:
            adjustments = safe["source_type_adjustment"]
    adjustment = _number((adjustments or {}).get(source), 0.0)
    adjustment = _clamp(adjustment, -AUTO_APPLY_MAX_ABS_ADJUSTMENT, AUTO_APPLY_MAX_ABS_ADJUSTMENT)
    return round(_clamp(base + adjustment, 0.0, 1.0), 3), round(adjustment, 3)


def extract_auto_safe_feedback_suggestions(
    calibration: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return feedback suggestions eligible for auto_safe tuning writes."""
    if not isinstance(calibration, Mapping):
        return {}
    suggestions: dict[str, dict[str, Any]] = {}
    for source, suggestion in _iter_calibration_suggestions(calibration):
        normalized = _normalize_source(source)
        if normalized not in AUTO_TUNING_SOURCE_TYPES or not isinstance(suggestion, Mapping):
            continue
        raw_adjustment = _number(suggestion.get("adjustment"), 0.0)
        if raw_adjustment == 0:
            continue
        reasons = [
            _clean_text(reason)
            for reason in (suggestion.get("reasons") or [])
            if _clean_text(reason)
        ]
        if "weak_ignored_pressure" in reasons:
            continue
        if raw_adjustment > 0 and "strong_music_positive_feedback" in reasons:
            if not _has_strong_music_positive_signal(calibration, normalized):
                continue
        if raw_adjustment < 0 and not _has_auto_safe_negative_signal(calibration, normalized):
            continue
        entry = suggestions.setdefault(
            normalized,
            {"adjustment": raw_adjustment, "reasons": []},
        )
        if abs(raw_adjustment) > abs(float(entry.get("adjustment", 0.0))):
            entry["adjustment"] = raw_adjustment
        existing_reasons = entry.setdefault("reasons", [])
        for reason in reasons:
            if reason not in existing_reasons:
                existing_reasons.append(reason)
    return {
        source: {
            "adjustment": round(float(entry["adjustment"]), 3),
            "reasons": list(entry["reasons"]),
        }
        for source, entry in sorted(suggestions.items())
    }


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
    if _mode(mode) != "auto_safe":
        return {"applied": False, "reason": "mode_not_auto_safe"}
    obs_path = Path(observation_path) if observation_path is not None else _config_file(config_dir, OBSERVATION_LOG_FILENAME)
    fb_path = Path(feedback_path) if feedback_path is not None else _config_file(config_dir, FEEDBACK_LOG_FILENAME)
    if obs_path is None or fb_path is None or not obs_path.exists() or not fb_path.exists():
        return {"applied": False, "reason": "logs_missing"}

    observations = load_recommendation_observations_jsonl(obs_path, limit=CALIBRATION_SAMPLE_LIMIT)
    feedback_events = load_recommendation_feedback_jsonl(fb_path, limit=CALIBRATION_SAMPLE_LIMIT * 4)
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
        saved = save_recommendation_tuning(rolled_back, path=tuning_path, config_dir=config_dir)
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
        return {"applied": False, "reason": blocked_reason, "calibration": _calibration_snapshot(calibration)}

    current_adjustments = dict(tuning.get("source_type_adjustment") or {})
    previous_adjustments = dict(current_adjustments)
    source_last_applied = dict(tuning.get("source_last_applied_at") or {})
    applied: dict[str, float] = {}
    reasons_by_source: dict[str, list[str]] = {}

    for source, suggestion in auto_safe_suggestions.items():
        normalized = _normalize_source(source)
        if normalized not in AUTO_TUNING_SOURCE_TYPES:
            continue
        last_applied = _number(source_last_applied.get(normalized), 0.0)
        if last_applied and current - last_applied < AUTO_APPLY_SOURCE_COOLDOWN_SECONDS:
            continue
        raw_adjustment = _number(suggestion.get("adjustment") if isinstance(suggestion, Mapping) else 0.0, 0.0)
        step = _clamp(raw_adjustment, -AUTO_APPLY_MAX_STEP, AUTO_APPLY_MAX_STEP)
        if step == 0:
            continue
        previous = _number(current_adjustments.get(normalized), 0.0)
        updated = round(_clamp(previous + step, -AUTO_APPLY_MAX_ABS_ADJUSTMENT, AUTO_APPLY_MAX_ABS_ADJUSTMENT), 3)
        actual_step = round(updated - previous, 3)
        if actual_step == 0:
            continue
        current_adjustments[normalized] = updated
        source_last_applied[normalized] = current
        applied[normalized] = actual_step
        if isinstance(suggestion, Mapping):
            reasons_by_source[normalized] = [
                _clean_text(reason)
                for reason in (suggestion.get("reasons") or [])
                if _clean_text(reason)
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
        "created_at": _number(tuning.get("created_at"), current) or current,
        "updated_at": current,
        "auto_apply_count": int(tuning.get("auto_apply_count") or 0) + 1,
        "source_last_applied_at": source_last_applied,
        "last_calibration": _calibration_snapshot(calibration),
        "last_auto_apply": {
            "applied": True,
            "adjustments": applied,
            "reasons": sorted({reason for reasons in reasons_by_source.values() for reason in reasons}),
        },
        "rollback": {
            "previous_source_type_adjustment": previous_adjustments,
            "applied": False,
            "reason": None,
        },
    }
    saved = save_recommendation_tuning(updated_tuning, path=tuning_path, config_dir=config_dir)
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
    if _mode(mode) != "auto_safe":
        return {"updated": False, "reason": "mode_not_auto_safe"}
    obs_path = Path(observation_path) if observation_path is not None else _config_file(config_dir, OBSERVATION_LOG_FILENAME)
    fb_path = Path(feedback_path) if feedback_path is not None else _config_file(config_dir, FEEDBACK_LOG_FILENAME)
    if obs_path is None or fb_path is None or not obs_path.exists() or not fb_path.exists():
        return {"updated": False, "reason": "logs_missing"}
    observations = load_recommendation_observations_jsonl(obs_path, limit=CALIBRATION_SAMPLE_LIMIT)
    feedback_events = load_recommendation_feedback_jsonl(fb_path, limit=CALIBRATION_SAMPLE_LIMIT * 4)
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
    saved = save_recommendation_tuning(evaluated, path=tuning_path, config_dir=config_dir)
    return {
        "updated": saved,
        "reason": None if saved else "save_failed",
        "tuning": tuning_public_status(evaluated),
    }


def evaluate_recommendation_tuning_health(
    tuning: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    safe = sanitize_recommendation_tuning(tuning)
    health = dict(safe.get("health") or _default_health())

    if health.get("status") == "paused":
        paused_until = _optional_number(health.get("paused_until"))
        if paused_until is not None and paused_until > current:
            health["last_evaluation"] = {
                **dict(health.get("last_evaluation") or {}),
                "decision": "paused",
            }
            safe["health"] = _sanitize_health(health)
            return safe
        health["status"] = "watch"
        health["paused_until"] = None
        health["pause_reason"] = None
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="pause_expired_watch",
            previous=safe.get("last_calibration"),
        )
        safe["health"] = _sanitize_health(health)
        return safe

    if int(calibration.get("feedback_joined_count") or 0) < AUTO_APPLY_MIN_FEEDBACK_COUNT:
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="insufficient_feedback_for_health",
            previous=safe.get("last_calibration"),
        )
        safe["health"] = _sanitize_health(health)
        return safe

    previous = safe.get("last_calibration")
    if not isinstance(previous, Mapping) or not previous:
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="baseline_missing",
            previous=previous,
        )
        safe["health"] = _sanitize_health(health)
        return safe

    signature = _calibration_signature(calibration)
    last_eval = health.get("last_evaluation") if isinstance(health.get("last_evaluation"), Mapping) else {}
    if last_eval.get("calibration_signature") == signature:
        return safe

    reason = _health_bad_reason(previous, calibration)
    if reason:
        bad_count = int(health.get("bad_window_count") or 0) + 1
        health["bad_window_count"] = bad_count
        health["good_window_count"] = 0
        if bad_count >= HEALTH_BAD_WINDOWS_TO_PAUSE:
            health["status"] = "paused"
            health["paused_until"] = current + HEALTH_PAUSE_SECONDS
            health["pause_reason"] = reason
            decision = "pause"
        else:
            health["status"] = "watch"
            health["paused_until"] = None
            health["pause_reason"] = reason
            decision = "watch"
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision=decision,
            previous=previous,
            reason=reason,
        )
        safe["health"] = _sanitize_health(health)
        return safe

    if _is_good_health_window(previous, calibration):
        health["status"] = "healthy"
        health["bad_window_count"] = 0
        health["good_window_count"] = int(health.get("good_window_count") or 0) + 1
        health["paused_until"] = None
        health["pause_reason"] = None
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="keep",
            previous=previous,
        )
        safe["health"] = _sanitize_health(health)
        return safe

    health["last_evaluation"] = _evaluation_payload(
        calibration,
        decision="neutral",
        previous=previous,
    )
    safe["health"] = _sanitize_health(health)
    return safe


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
    health = dict(tuning.get("health") or _default_health())
    health.update(
        {
            "status": "paused",
            "paused_until": current + max(0, int(duration_seconds)),
            "pause_reason": _clean_text(reason) or "manual_pause",
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
    health = dict(tuning.get("health") or _default_health())
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


def _default_tuning() -> dict[str, Any]:
    return {
        "version": TUNING_VERSION,
        "enabled": False,
        "mode": "off",
        "source_type_adjustment": {},
        "created_from": None,
        "sample_count": 0,
        "created_at": 0.0,
        "updated_at": 0.0,
        "auto_apply_count": 0,
        "source_last_applied_at": {},
        "last_calibration": {},
        "last_auto_apply": {"applied": False, "adjustments": {}, "reasons": []},
        "rollback": {"previous_source_type_adjustment": {}, "applied": False, "reason": None},
        "health": _default_health(),
    }


def _resolve_tuning_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    return _config_file(config_dir, TUNING_FILENAME)


def _config_file(config_dir: str | os.PathLike[str] | None, filename: str) -> Path | None:
    if config_dir is None:
        return None
    return Path(config_dir) / filename


def _sanitize_source_adjustments(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, float] = {}
    for source, adjustment in value.items():
        normalized = _normalize_source(source)
        if normalized not in AUTO_TUNING_SOURCE_TYPES:
            continue
        amount = round(_clamp(_number(adjustment, 0.0), -AUTO_APPLY_MAX_ABS_ADJUSTMENT, AUTO_APPLY_MAX_ABS_ADJUSTMENT), 3)
        if amount:
            safe[normalized] = amount
    return safe


def _sanitize_source_timestamps(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _normalize_source(source): max(0.0, _number(ts, 0.0))
        for source, ts in value.items()
        if _normalize_source(source) in AUTO_TUNING_SOURCE_TYPES
    }


def _sanitize_calibration_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    snapshot = {
        "average_feedback_score": _optional_number(value.get("average_feedback_score")),
        "top1_positive_rate": _optional_number(value.get("top1_positive_rate")),
        "top1_negative_rate": _optional_number(value.get("top1_negative_rate")),
    }
    if all(item is None for item in snapshot.values()):
        return {}
    return snapshot


def _sanitize_last_auto_apply(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"applied": False, "adjustments": {}, "reasons": []}
    return {
        "applied": bool(value.get("applied")),
        "adjustments": _sanitize_source_adjustments(value.get("adjustments")),
        "reasons": [
            _clean_text(reason)
            for reason in (value.get("reasons") or [])
            if _clean_text(reason)
        ],
    }


def _sanitize_rollback(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"previous_source_type_adjustment": {}, "applied": False, "reason": None}
    return {
        "previous_source_type_adjustment": _sanitize_source_adjustments(
            value.get("previous_source_type_adjustment")
        ),
        "applied": bool(value.get("applied")),
        "reason": _clean_text(value.get("reason")) or None,
    }


def _default_health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "bad_window_count": 0,
        "good_window_count": 0,
        "paused_until": None,
        "pause_reason": None,
        "last_evaluation": {},
    }


def _sanitize_health(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _default_health()
    status = _clean_text(value.get("status")) or "healthy"
    if status not in HEALTH_STATUSES:
        status = "healthy"
    last_evaluation = value.get("last_evaluation")
    if not isinstance(last_evaluation, Mapping):
        last_evaluation = {}
    return {
        "status": status,
        "bad_window_count": max(0, int(_number(value.get("bad_window_count"), 0))),
        "good_window_count": max(0, int(_number(value.get("good_window_count"), 0))),
        "paused_until": _optional_number(value.get("paused_until")),
        "pause_reason": _clean_text(value.get("pause_reason")) or None,
        "last_evaluation": _sanitize_last_evaluation(last_evaluation),
    }


def _sanitize_last_evaluation(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "decision",
        "reason",
        "average_feedback_delta",
        "negative_rate_delta",
        "high_bucket_feedback",
        "calibration_signature",
    }
    safe: dict[str, Any] = {}
    for key in allowed:
        if key not in value:
            continue
        if key in {"average_feedback_delta", "negative_rate_delta", "high_bucket_feedback"}:
            safe[key] = _optional_number(value.get(key))
        else:
            safe[key] = _clean_text(value.get(key)) or None
    return {key: item for key, item in safe.items() if item is not None}


def _calibration_snapshot(calibration: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "average_feedback_score": _optional_number(calibration.get("average_feedback_score")),
        "top1_positive_rate": _optional_number(calibration.get("top1_positive_rate")),
        "top1_negative_rate": _optional_number(calibration.get("top1_negative_rate")),
    }


def _health_auto_apply_block_reason(tuning: Mapping[str, Any], *, now: float) -> str | None:
    health = _sanitize_health(tuning.get("health") if isinstance(tuning, Mapping) else None)
    status = health.get("status")
    if status == "watch":
        return "tuning_health_watch"
    if status == "paused":
        paused_until = _optional_number(health.get("paused_until"))
        if paused_until is None or paused_until > now:
            return "tuning_health_paused"
        return "tuning_health_watch"
    return None


def _health_bad_reason(
    previous: Mapping[str, Any],
    calibration: Mapping[str, Any],
) -> str | None:
    previous_average = _optional_number(previous.get("average_feedback_score"))
    current_average = _optional_number(calibration.get("average_feedback_score"))
    if previous_average is not None and current_average is not None:
        if previous_average - current_average >= ROLLBACK_AVERAGE_DROP:
            return "average_feedback_score_drop"
    previous_negative = _optional_number(previous.get("top1_negative_rate"))
    current_negative = _optional_number(calibration.get("top1_negative_rate"))
    if previous_negative is not None and current_negative is not None:
        if current_negative - previous_negative >= ROLLBACK_NEGATIVE_RATE_INCREASE:
            return "top1_negative_rate_increase"
    high_average = _high_bucket_feedback(calibration)
    if high_average is not None and high_average <= 0:
        return "high_bucket_feedback_not_positive"
    return None


def _is_good_health_window(
    previous: Mapping[str, Any],
    calibration: Mapping[str, Any],
) -> bool:
    previous_average = _optional_number(previous.get("average_feedback_score"))
    current_average = _optional_number(calibration.get("average_feedback_score"))
    previous_negative = _optional_number(previous.get("top1_negative_rate"))
    current_negative = _optional_number(calibration.get("top1_negative_rate"))
    high_average = _high_bucket_feedback(calibration)
    return bool(
        previous_average is not None
        and current_average is not None
        and current_average >= previous_average
        and previous_negative is not None
        and current_negative is not None
        and current_negative <= previous_negative
        and high_average is not None
        and high_average > 0
    )


def _evaluation_payload(
    calibration: Mapping[str, Any],
    *,
    decision: str,
    previous: Mapping[str, Any] | None,
    reason: str | None = None,
) -> dict[str, Any]:
    previous = previous if isinstance(previous, Mapping) else {}
    current_average = _optional_number(calibration.get("average_feedback_score"))
    previous_average = _optional_number(previous.get("average_feedback_score"))
    current_negative = _optional_number(calibration.get("top1_negative_rate"))
    previous_negative = _optional_number(previous.get("top1_negative_rate"))
    return {
        "decision": decision,
        "reason": reason,
        "average_feedback_delta": _delta(current_average, previous_average),
        "negative_rate_delta": _delta(current_negative, previous_negative),
        "high_bucket_feedback": _high_bucket_feedback(calibration),
        "calibration_signature": _calibration_signature(calibration),
    }


def _high_bucket_feedback(calibration: Mapping[str, Any]) -> float | None:
    buckets = calibration.get("score_bucket_feedback")
    if not isinstance(buckets, Mapping):
        return None
    high = buckets.get("high")
    if not isinstance(high, Mapping):
        return None
    return _optional_number(high.get("average_feedback_score"))


def _calibration_signature(calibration: Mapping[str, Any]) -> str:
    return "|".join(
        str(item)
        for item in (
            int(calibration.get("sample_count") or 0),
            int(calibration.get("feedback_joined_count") or 0),
            _optional_number(calibration.get("average_feedback_score")),
            _optional_number(calibration.get("top1_negative_rate")),
            _high_bucket_feedback(calibration),
        )
    )


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 3)


def _auto_apply_blocked_reason(
    calibration: Mapping[str, Any],
    *,
    auto_safe_suggestions: Mapping[str, Mapping[str, Any]] | None = None,
) -> str | None:
    if int(calibration.get("feedback_joined_count") or 0) < AUTO_APPLY_MIN_FEEDBACK_COUNT:
        return "feedback_sample_count_below_threshold"
    average = _optional_number(calibration.get("average_feedback_score"))
    if average is None or average <= 0:
        return "average_feedback_score_not_positive"
    negative_rate = _optional_number(calibration.get("top1_negative_rate"))
    if negative_rate is None or negative_rate > AUTO_APPLY_MAX_NEGATIVE_RATE:
        return "top1_negative_rate_above_threshold"
    if not auto_safe_suggestions:
        return "no_auto_safe_feedback_suggestions"
    return None


def _iter_calibration_suggestions(
    calibration: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    items: list[tuple[str, Mapping[str, Any]]] = []
    for key in ("suggested_weight_adjustments", "feedback_actionable_suggestions"):
        value = calibration.get(key)
        if not isinstance(value, Mapping):
            continue
        for source, suggestion in value.items():
            if isinstance(suggestion, Mapping):
                items.append((_clean_text(source), suggestion))
    return items


def _has_strong_music_positive_signal(
    calibration: Mapping[str, Any],
    source: str,
) -> bool:
    if source != "music":
        return False
    stats = _source_signal_stats(calibration, "music")
    played_through = int(stats.get("played_through_count") or 0)
    source_average = _source_average_feedback(calibration, "music")
    return (
        played_through >= 3
        and source_average is not None
        and source_average >= 0.5
    )


def _has_auto_safe_negative_signal(
    calibration: Mapping[str, Any],
    source: str,
) -> bool:
    stats = _source_signal_stats(calibration, source)
    high_negative = int(stats.get("high_confidence_negative_count") or 0)
    source_average = _source_average_feedback(calibration, source)
    return (
        high_negative >= AUTO_APPLY_MIN_HIGH_CONFIDENCE_NEGATIVE_COUNT
        and source_average is not None
        and source_average < 0
    )


def _source_signal_stats(
    calibration: Mapping[str, Any],
    source: str,
) -> Mapping[str, Any]:
    signal_summary = calibration.get("feedback_signal_summary")
    if not isinstance(signal_summary, Mapping):
        return {}
    stats = signal_summary.get(source)
    return stats if isinstance(stats, Mapping) else {}


def _source_average_feedback(
    calibration: Mapping[str, Any],
    source: str,
) -> float | None:
    score_by_source = calibration.get("score_by_source_type")
    if not isinstance(score_by_source, Mapping):
        return None
    return _optional_number(score_by_source.get(source))


def _rollback_reason(
    tuning: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    now: float,
) -> str | None:
    last = tuning.get("last_calibration") if isinstance(tuning, Mapping) else None
    if not isinstance(last, Mapping) or not last:
        return None
    updated_at = _number(tuning.get("updated_at"), 0.0)
    if updated_at and now - updated_at < AUTO_APPLY_SOURCE_COOLDOWN_SECONDS:
        return None
    previous_average = _optional_number(last.get("average_feedback_score"))
    current_average = _optional_number(calibration.get("average_feedback_score"))
    if previous_average is not None and current_average is not None:
        if previous_average - current_average >= ROLLBACK_AVERAGE_DROP:
            return "average_feedback_score_drop"
    previous_negative = _optional_number(last.get("top1_negative_rate"))
    current_negative = _optional_number(calibration.get("top1_negative_rate"))
    if previous_negative is not None and current_negative is not None:
        if current_negative - previous_negative >= ROLLBACK_NEGATIVE_RATE_INCREASE:
            return "top1_negative_rate_increase"
    high_bucket = calibration.get("score_bucket_feedback")
    if isinstance(high_bucket, Mapping):
        high = high_bucket.get("high")
        if isinstance(high, Mapping):
            high_average = _optional_number(high.get("average_feedback_score"))
            if high_average is not None and high_average <= 0:
                return "high_bucket_feedback_not_positive"
    return None


def _apply_rollback(
    tuning: Mapping[str, Any],
    calibration: Mapping[str, Any],
    reason: str,
    *,
    now: float,
) -> dict[str, Any]:
    rollback = tuning.get("rollback") if isinstance(tuning, Mapping) else {}
    previous = {}
    if isinstance(rollback, Mapping):
        previous = _sanitize_source_adjustments(rollback.get("previous_source_type_adjustment"))
    health = _sanitize_health(tuning.get("health") if isinstance(tuning, Mapping) else None)
    bad_count = int(health.get("bad_window_count") or 0) + 1
    health["bad_window_count"] = bad_count
    health["good_window_count"] = 0
    if bad_count >= HEALTH_BAD_WINDOWS_TO_PAUSE:
        health["status"] = "paused"
        health["paused_until"] = now + HEALTH_PAUSE_SECONDS
        health["pause_reason"] = reason
        decision = "pause"
    else:
        health["status"] = "watch"
        health["paused_until"] = None
        health["pause_reason"] = reason
        decision = "watch"
    health["last_evaluation"] = _evaluation_payload(
        calibration,
        decision=decision,
        previous=tuning.get("last_calibration") if isinstance(tuning, Mapping) else {},
        reason=reason,
    )
    return {
        **sanitize_recommendation_tuning(tuning),
        "source_type_adjustment": previous,
        "updated_at": now,
        "last_calibration": _calibration_snapshot(calibration),
        "last_auto_apply": {"applied": False, "adjustments": {}, "reasons": []},
        "rollback": {
            "previous_source_type_adjustment": previous,
            "applied": True,
            "reason": reason,
        },
        "health": health,
    }


def _mode(value: Any) -> str:
    raw = _clean_text(value)
    return raw if raw in TUNING_MODES else "off"


def _normalize_source(value: Any) -> str:
    return _clean_text(value).lower()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
