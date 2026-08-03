"""Pure safety, health, and rollback policies for tuning."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from main_logic.proactive_recommendation.normalization import (
    coerce_float_or_default,
    normalize_source_identifier,
    to_stripped_text,
)

from .configuration import (
    _new_default_tuning_health,
    _coerce_optional_finite_float,
    _sanitize_tuning_health,
    _sanitize_source_adjustments,
    sanitize_recommendation_tuning,
)

AUTO_TUNING_SOURCE_TYPES = {"news", "web", "video", "home", "music", "meme"}

AUTO_APPLY_MIN_FEEDBACK_COUNT = 30

AUTO_APPLY_MAX_NEGATIVE_RATE = 0.20

AUTO_APPLY_SOURCE_COOLDOWN_SECONDS = 3600

AUTO_APPLY_MIN_HIGH_CONFIDENCE_NEGATIVE_COUNT = 2

ROLLBACK_AVERAGE_DROP = 0.10

ROLLBACK_NEGATIVE_RATE_INCREASE = 0.10

HEALTH_PAUSE_SECONDS = 6 * 3600

HEALTH_BAD_WINDOWS_TO_PAUSE = 2


def extract_auto_safe_feedback_suggestions(
    calibration: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return feedback suggestions eligible for auto_safe tuning writes."""
    if not isinstance(calibration, Mapping):
        return {}
    suggestions: dict[str, dict[str, Any]] = {}
    for source, suggestion in _iter_calibration_suggestions(calibration):
        normalized = normalize_source_identifier(source)
        if normalized not in AUTO_TUNING_SOURCE_TYPES or not isinstance(
            suggestion, Mapping
        ):
            continue
        raw_adjustment = coerce_float_or_default(
            suggestion.get("adjustment"), default=0.0
        )
        if raw_adjustment == 0:
            continue
        reasons = [
            to_stripped_text(reason)
            for reason in (suggestion.get("reasons") or [])
            if to_stripped_text(reason)
        ]
        if "weak_ignored_pressure" in reasons:
            continue
        if raw_adjustment > 0 and "strong_music_positive_feedback" in reasons:
            if not _has_strong_music_positive_signal(calibration, normalized):
                continue
        if raw_adjustment < 0 and not _has_auto_safe_negative_signal(
            calibration, normalized
        ):
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


def evaluate_recommendation_tuning_health(
    tuning: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    safe = sanitize_recommendation_tuning(tuning)
    health = dict(safe.get("health") or _new_default_tuning_health())

    if health.get("status") == "paused":
        paused_until = _coerce_optional_finite_float(health.get("paused_until"))
        if paused_until is not None and paused_until > current:
            health["last_evaluation"] = {
                **dict(health.get("last_evaluation") or {}),
                "decision": "paused",
            }
            safe["health"] = _sanitize_tuning_health(health)
            return safe
        health["status"] = "watch"
        health["paused_until"] = None
        health["pause_reason"] = None
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="pause_expired_watch",
            previous=safe.get("last_calibration"),
        )
        safe["health"] = _sanitize_tuning_health(health)
        return safe

    if (
        int(calibration.get("feedback_joined_count") or 0)
        < AUTO_APPLY_MIN_FEEDBACK_COUNT
    ):
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="insufficient_feedback_for_health",
            previous=safe.get("last_calibration"),
        )
        safe["health"] = _sanitize_tuning_health(health)
        return safe

    previous = safe.get("last_calibration")
    if not isinstance(previous, Mapping) or not previous:
        health["last_evaluation"] = _evaluation_payload(
            calibration,
            decision="baseline_missing",
            previous=previous,
        )
        safe["health"] = _sanitize_tuning_health(health)
        return safe

    signature = _calibration_signature(calibration)
    last_eval = (
        health.get("last_evaluation")
        if isinstance(health.get("last_evaluation"), Mapping)
        else {}
    )
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
        safe["health"] = _sanitize_tuning_health(health)
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
        safe["health"] = _sanitize_tuning_health(health)
        return safe

    health["last_evaluation"] = _evaluation_payload(
        calibration,
        decision="neutral",
        previous=previous,
    )
    safe["health"] = _sanitize_tuning_health(health)
    return safe


def _calibration_snapshot(calibration: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "average_feedback_score": _coerce_optional_finite_float(
            calibration.get("average_feedback_score")
        ),
        "top1_positive_rate": _coerce_optional_finite_float(
            calibration.get("top1_positive_rate")
        ),
        "top1_negative_rate": _coerce_optional_finite_float(
            calibration.get("top1_negative_rate")
        ),
    }


def _health_auto_apply_block_reason(
    tuning: Mapping[str, Any], *, now: float
) -> str | None:
    health = _sanitize_tuning_health(
        tuning.get("health") if isinstance(tuning, Mapping) else None
    )
    status = health.get("status")
    if status == "watch":
        return "tuning_health_watch"
    if status == "paused":
        paused_until = _coerce_optional_finite_float(health.get("paused_until"))
        if paused_until is None or paused_until > now:
            return "tuning_health_paused"
        return "tuning_health_watch"
    return None


def _health_bad_reason(
    previous: Mapping[str, Any],
    calibration: Mapping[str, Any],
) -> str | None:
    previous_average = _coerce_optional_finite_float(
        previous.get("average_feedback_score")
    )
    current_average = _coerce_optional_finite_float(
        calibration.get("average_feedback_score")
    )
    if previous_average is not None and current_average is not None:
        if previous_average - current_average >= ROLLBACK_AVERAGE_DROP:
            return "average_feedback_score_drop"
    previous_negative = _coerce_optional_finite_float(
        previous.get("top1_negative_rate")
    )
    current_negative = _coerce_optional_finite_float(
        calibration.get("top1_negative_rate")
    )
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
    previous_average = _coerce_optional_finite_float(
        previous.get("average_feedback_score")
    )
    current_average = _coerce_optional_finite_float(
        calibration.get("average_feedback_score")
    )
    previous_negative = _coerce_optional_finite_float(
        previous.get("top1_negative_rate")
    )
    current_negative = _coerce_optional_finite_float(
        calibration.get("top1_negative_rate")
    )
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
    current_average = _coerce_optional_finite_float(
        calibration.get("average_feedback_score")
    )
    previous_average = _coerce_optional_finite_float(
        previous.get("average_feedback_score")
    )
    current_negative = _coerce_optional_finite_float(
        calibration.get("top1_negative_rate")
    )
    previous_negative = _coerce_optional_finite_float(
        previous.get("top1_negative_rate")
    )
    return {
        "decision": decision,
        "reason": reason,
        "average_feedback_delta": _rounded_metric_delta(
            current_average, previous_average
        ),
        "negative_rate_delta": _rounded_metric_delta(
            current_negative, previous_negative
        ),
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
    return _coerce_optional_finite_float(high.get("average_feedback_score"))


def _calibration_signature(calibration: Mapping[str, Any]) -> str:
    return "|".join(
        str(item)
        for item in (
            int(calibration.get("sample_count") or 0),
            int(calibration.get("feedback_joined_count") or 0),
            _coerce_optional_finite_float(calibration.get("average_feedback_score")),
            _coerce_optional_finite_float(calibration.get("top1_negative_rate")),
            _high_bucket_feedback(calibration),
        )
    )


def _rounded_metric_delta(
    current: float | None, previous: float | None
) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 3)


def _auto_apply_blocked_reason(
    calibration: Mapping[str, Any],
    *,
    auto_safe_suggestions: Mapping[str, Mapping[str, Any]] | None = None,
) -> str | None:
    if (
        int(calibration.get("feedback_joined_count") or 0)
        < AUTO_APPLY_MIN_FEEDBACK_COUNT
    ):
        return "feedback_sample_count_below_threshold"
    average = _coerce_optional_finite_float(calibration.get("average_feedback_score"))
    if average is None or average <= 0:
        return "average_feedback_score_not_positive"
    negative_rate = _coerce_optional_finite_float(calibration.get("top1_negative_rate"))
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
                items.append((to_stripped_text(source), suggestion))
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
    return played_through >= 3 and source_average is not None and source_average >= 0.5


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
    return _coerce_optional_finite_float(score_by_source.get(source))


def _rollback_reason(
    tuning: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    now: float,
) -> str | None:
    last = tuning.get("last_calibration") if isinstance(tuning, Mapping) else None
    if not isinstance(last, Mapping) or not last:
        return None
    updated_at = coerce_float_or_default(tuning.get("updated_at"), default=0.0)
    if updated_at and now - updated_at < AUTO_APPLY_SOURCE_COOLDOWN_SECONDS:
        return None
    previous_average = _coerce_optional_finite_float(last.get("average_feedback_score"))
    current_average = _coerce_optional_finite_float(
        calibration.get("average_feedback_score")
    )
    if previous_average is not None and current_average is not None:
        if previous_average - current_average >= ROLLBACK_AVERAGE_DROP:
            return "average_feedback_score_drop"
    previous_negative = _coerce_optional_finite_float(last.get("top1_negative_rate"))
    current_negative = _coerce_optional_finite_float(
        calibration.get("top1_negative_rate")
    )
    if previous_negative is not None and current_negative is not None:
        if current_negative - previous_negative >= ROLLBACK_NEGATIVE_RATE_INCREASE:
            return "top1_negative_rate_increase"
    high_bucket = calibration.get("score_bucket_feedback")
    if isinstance(high_bucket, Mapping):
        high = high_bucket.get("high")
        if isinstance(high, Mapping):
            high_average = _coerce_optional_finite_float(
                high.get("average_feedback_score")
            )
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
        previous = _sanitize_source_adjustments(
            rollback.get("previous_source_type_adjustment")
        )
    health = _sanitize_tuning_health(
        tuning.get("health") if isinstance(tuning, Mapping) else None
    )
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
