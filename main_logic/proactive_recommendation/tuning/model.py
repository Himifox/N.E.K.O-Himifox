"""Tuning data model, sanitization, and score application."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TUNING_VERSION = 1

TUNING_MODES = {"off", "manual", "auto_safe"}

AUTO_TUNING_SOURCE_TYPES = {"news", "web", "video", "home", "music", "meme"}

AUTO_APPLY_MAX_ABS_ADJUSTMENT = 0.15

HEALTH_STATUSES = {"healthy", "watch", "paused"}


def sanitize_recommendation_tuning(tuning: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(tuning, Mapping):
        tuning = {}
    source_adjustments = _sanitize_source_adjustments(
        tuning.get("source_type_adjustment")
    )
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
        "source_last_applied_at": _sanitize_source_timestamps(
            tuning.get("source_last_applied_at")
        ),
        "last_calibration": _sanitize_calibration_snapshot(
            tuning.get("last_calibration")
        ),
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
    adjustment = _clamp(
        adjustment, -AUTO_APPLY_MAX_ABS_ADJUSTMENT, AUTO_APPLY_MAX_ABS_ADJUSTMENT
    )
    return round(_clamp(base + adjustment, 0.0, 1.0), 3), round(adjustment, 3)


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
        "rollback": {
            "previous_source_type_adjustment": {},
            "applied": False,
            "reason": None,
        },
        "health": _default_health(),
    }


def _sanitize_source_adjustments(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, float] = {}
    for source, adjustment in value.items():
        normalized = _normalize_source(source)
        if normalized not in AUTO_TUNING_SOURCE_TYPES:
            continue
        amount = round(
            _clamp(
                _number(adjustment, 0.0),
                -AUTO_APPLY_MAX_ABS_ADJUSTMENT,
                AUTO_APPLY_MAX_ABS_ADJUSTMENT,
            ),
            3,
        )
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
        if key in {
            "average_feedback_delta",
            "negative_rate_delta",
            "high_bucket_feedback",
        }:
            safe[key] = _optional_number(value.get(key))
        else:
            safe[key] = _clean_text(value.get(key)) or None
    return {key: item for key, item in safe.items() if item is not None}


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
