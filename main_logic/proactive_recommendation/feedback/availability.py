"""Shadow-only aggregate baseline for whether now is a good time to interrupt."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import logging
import math
import os
from pathlib import Path
import time
from typing import Any

from config import PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE

from ..normalization import coerce_float_or_default, rounded_ratio_or_none
from ..persistence import AtomicJsonStore


AVAILABILITY_FILENAME = "proactive_recommendation_availability_shadow.json"
AVAILABILITY_VERSION = "availability_shadow_v1"
AVAILABILITY_HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
AVAILABILITY_MIN_EXPOSURES = 30
AVAILABILITY_MIN_REPLIES = 10
AVAILABILITY_REPLY_WINDOW_SECONDS = 10 * 60

_ACTIVITY_STATES = {
    "away",
    "busy",
    "chatting",
    "focused_work",
    "gaming",
    "idle",
    "unknown",
}
_INPUT_MODES = {"audio", "text", "unknown"}
logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_availability")


def availability_time_bucket(timestamp: float) -> str:
    """Return one of four local six-hour buckets."""
    start = (datetime.fromtimestamp(float(timestamp)).hour // 6) * 6
    return f"{start:02d}-{(start + 6):02d}"


def record_availability_outcome(
    *,
    config_dir: str | os.PathLike[str] | None,
    activity_state: Any,
    input_mode: Any,
    delivered_at: float,
    replied_at: float | None = None,
    censored: bool = False,
    mode: str | None = None,
) -> dict[str, Any]:
    """Add one finalized exposure; no turn ID, source, or text is persisted."""
    effective_mode = PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE if mode is None else mode
    outcome_at = float(replied_at) if replied_at is not None else float(delivered_at) + AVAILABILITY_REPLY_WINDOW_SECONDS
    if effective_mode != "shadow" or config_dir is None:
        return get_availability_shadow(
            config_dir=config_dir,
            activity_state=activity_state,
            input_mode=input_mode,
            now=outcome_at,
            mode=effective_mode,
        )
    replied = replied_at is not None
    if not replied and not censored:
        return get_availability_shadow(
            config_dir=config_dir,
            activity_state=activity_state,
            input_mode=input_mode,
            now=outcome_at,
            mode=effective_mode,
        )

    activity = _normalize_activity_state(activity_state)
    normalized_input = _normalize_input_mode(input_mode)
    time_bucket = availability_time_bucket(delivered_at)
    bucket_key = _bucket_key(activity, normalized_input, time_bucket)

    def update(state: dict[str, Any]) -> dict[str, Any]:
        bucket = _decayed_bucket(
            state["buckets"].get(bucket_key),
            activity_state=activity,
            input_mode=normalized_input,
            time_bucket=time_bucket,
            now=outcome_at,
        )
        bucket["exposure_weight"] += 1.0
        if replied:
            latency = min(
                AVAILABILITY_REPLY_WINDOW_SECONDS,
                max(0.0, float(replied_at) - float(delivered_at)),
            )
            bucket["reply_weight"] += 1.0
            bucket["reply_latency_weighted_seconds"] += latency
        else:
            bucket["censored_weight"] += 1.0
        bucket["updated_at"] = outcome_at
        state["buckets"][bucket_key] = bucket
        state["updated_at"] = outcome_at
        return state

    try:
        _availability_store(Path(config_dir)).update(update)
    except Exception:
        logger.debug("availability shadow update failed", exc_info=True)
        return _availability_snapshot(
            mode=effective_mode,
            activity=activity,
            input_mode=normalized_input,
            time_bucket=time_bucket,
            selected_level=None,
            selected=None,
            fallback_trace=[],
            storage_error=True,
        )
    return get_availability_shadow(
        config_dir=config_dir,
        activity_state=activity,
        input_mode=normalized_input,
        now=outcome_at,
        mode=effective_mode,
    )


def get_availability_shadow(
    *,
    config_dir: str | os.PathLike[str] | None,
    activity_state: Any = "unknown",
    input_mode: Any = "unknown",
    now: float | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Return a counterfactual suggestion that cannot affect scheduling."""
    effective_mode = PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE if mode is None else mode
    current = time.time() if now is None else float(now)
    activity = _normalize_activity_state(activity_state)
    normalized_input = _normalize_input_mode(input_mode)
    time_bucket = availability_time_bucket(current)
    if effective_mode != "shadow" or config_dir is None:
        return _availability_snapshot(
            mode=effective_mode,
            activity=activity,
            input_mode=normalized_input,
            time_bucket=time_bucket,
            selected_level=None,
            selected=None,
            fallback_trace=[],
        )

    try:
        state = _availability_store(Path(config_dir)).read()
    except Exception:
        logger.debug("availability shadow read failed", exc_info=True)
        return _availability_snapshot(
            mode=effective_mode,
            activity=activity,
            input_mode=normalized_input,
            time_bucket=time_bucket,
            selected_level=None,
            selected=None,
            fallback_trace=[],
            storage_error=True,
        )
    buckets = [
        _decayed_bucket(
            bucket,
            activity_state=bucket.get("activity_state"),
            input_mode=bucket.get("input_mode"),
            time_bucket=bucket.get("time_bucket"),
            now=current,
        )
        for bucket in state["buckets"].values()
    ]
    candidates = (
        (
            "exact",
            _combine_buckets(
                bucket
                for bucket in buckets
                if bucket["activity_state"] == activity
                and bucket["input_mode"] == normalized_input
                and bucket["time_bucket"] == time_bucket
            ),
        ),
        (
            "activity_state",
            _combine_buckets(
                bucket for bucket in buckets if bucket["activity_state"] == activity
            ),
        ),
        (
            "input_mode",
            _combine_buckets(
                bucket for bucket in buckets if bucket["input_mode"] == normalized_input
            ),
        ),
        ("global", _combine_buckets(buckets)),
    )
    selected_level = None
    selected = None
    fallback_trace = []
    for level, bucket in candidates:
        ready = _bucket_ready(bucket)
        fallback_trace.append({"level": level, **_public_bucket(bucket), "ready": ready})
        if ready:
            selected_level = level
            selected = bucket
            break
    return _availability_snapshot(
        mode=effective_mode,
        activity=activity,
        input_mode=normalized_input,
        time_bucket=time_bucket,
        selected_level=selected_level,
        selected=selected,
        fallback_trace=fallback_trace,
    )


def _availability_snapshot(
    *,
    mode: str,
    activity: str,
    input_mode: str,
    time_bucket: str,
    selected_level: str | None,
    selected: Mapping[str, Any] | None,
    fallback_trace: list[dict[str, Any]],
    storage_error: bool = False,
) -> dict[str, Any]:
    status = "insufficient"
    multiplier = "2x" if mode == "shadow" else None
    if selected is not None:
        response_rate = float(selected["reply_weight"]) / float(
            selected["exposure_weight"]
        )
        latency = float(selected["reply_latency_weighted_seconds"]) / float(
            selected["reply_weight"]
        )
        if response_rate >= 0.50 and latency <= 180:
            status, multiplier = "available", "1x"
        elif response_rate < 0.20 or latency >= 480:
            status, multiplier = "unavailable", "4x"
        else:
            status, multiplier = "uncertain", "2x"
    return {
        "version": AVAILABILITY_VERSION,
        "mode": mode,
        "enabled": mode == "shadow",
        "shadow_only": True,
        "scheduling_consumed": False,
        "interval_consumed": False,
        "gate_consumed": False,
        "status": status,
        "counterfactual_interval_multiplier": multiplier,
        "selected_level": selected_level,
        "selected_bucket": _public_bucket(selected),
        "context": {
            "activity_state": activity,
            "input_mode": input_mode,
            "local_time_bucket": time_bucket,
        },
        "minimum_exposures": AVAILABILITY_MIN_EXPOSURES,
        "minimum_replies": AVAILABILITY_MIN_REPLIES,
        "reply_window_seconds": AVAILABILITY_REPLY_WINDOW_SECONDS,
        "half_life_seconds": AVAILABILITY_HALF_LIFE_SECONDS,
        "fallback_trace": fallback_trace,
        "stored_fields": "aggregate_only_no_conversation_text",
        "storage_error": storage_error,
    }


def _public_bucket(bucket: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not bucket:
        return None
    exposures = float(bucket.get("exposure_weight") or 0.0)
    replies = float(bucket.get("reply_weight") or 0.0)
    return {
        "exposure_count": round(exposures, 3),
        "reply_count": round(replies, 3),
        "censored_count": round(float(bucket.get("censored_weight") or 0.0), 3),
        "response_rate": rounded_ratio_or_none(replies, exposures),
        "average_reply_latency_seconds": (
            round(float(bucket.get("reply_latency_weighted_seconds") or 0.0) / replies, 3)
            if replies > 0
            else None
        ),
    }


def _bucket_ready(bucket: Mapping[str, Any] | None) -> bool:
    return bool(
        bucket
        and float(bucket.get("exposure_weight") or 0.0) >= AVAILABILITY_MIN_EXPOSURES
        and float(bucket.get("reply_weight") or 0.0) >= AVAILABILITY_MIN_REPLIES
    )


def _combine_buckets(buckets: Any) -> dict[str, float] | None:
    combined = {
        "exposure_weight": 0.0,
        "reply_weight": 0.0,
        "censored_weight": 0.0,
        "reply_latency_weighted_seconds": 0.0,
    }
    found = False
    for bucket in buckets:
        found = True
        for key in combined:
            combined[key] += float(bucket.get(key) or 0.0)
    return combined if found else None


def _decayed_bucket(
    raw: Any,
    *,
    activity_state: Any,
    input_mode: Any,
    time_bucket: Any,
    now: float,
) -> dict[str, Any]:
    bucket = raw if isinstance(raw, Mapping) else {}
    updated_at = coerce_float_or_default(bucket.get("updated_at"), default=now)
    elapsed = max(0.0, now - updated_at)
    factor = math.pow(0.5, elapsed / AVAILABILITY_HALF_LIFE_SECONDS)
    return {
        "activity_state": _normalize_activity_state(activity_state),
        "input_mode": _normalize_input_mode(input_mode),
        "time_bucket": _normalize_time_bucket(time_bucket),
        "exposure_weight": max(0.0, coerce_float_or_default(bucket.get("exposure_weight"), default=0.0)) * factor,
        "reply_weight": max(0.0, coerce_float_or_default(bucket.get("reply_weight"), default=0.0)) * factor,
        "censored_weight": max(0.0, coerce_float_or_default(bucket.get("censored_weight"), default=0.0)) * factor,
        "reply_latency_weighted_seconds": max(
            0.0,
            coerce_float_or_default(
                bucket.get("reply_latency_weighted_seconds"), default=0.0
            ),
        )
        * factor,
        "updated_at": now,
    }


def _sanitize_state(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) and raw.get("schema_version") == 1 else {}
    buckets: dict[str, dict[str, Any]] = {}
    raw_buckets = source.get("buckets")
    if isinstance(raw_buckets, Mapping):
        for raw_bucket in raw_buckets.values():
            if not isinstance(raw_bucket, Mapping):
                continue
            activity = _normalize_activity_state(raw_bucket.get("activity_state"))
            input_mode = _normalize_input_mode(raw_bucket.get("input_mode"))
            time_bucket = _normalize_time_bucket(raw_bucket.get("time_bucket"))
            buckets[_bucket_key(activity, input_mode, time_bucket)] = _decayed_bucket(
                raw_bucket,
                activity_state=activity,
                input_mode=input_mode,
                time_bucket=time_bucket,
                now=coerce_float_or_default(raw_bucket.get("updated_at"), default=0.0),
            )
    return {
        "schema_version": 1,
        "updated_at": max(0.0, coerce_float_or_default(source.get("updated_at"), default=0.0)),
        "buckets": buckets,
    }


def _availability_store(config_dir: Path) -> AtomicJsonStore:
    return AtomicJsonStore(
        config_dir / AVAILABILITY_FILENAME,
        default_factory=lambda: {"schema_version": 1, "updated_at": 0.0, "buckets": {}},
        sanitizer=_sanitize_state,
    )


def _normalize_activity_state(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _ACTIVITY_STATES else "unknown"


def _normalize_input_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "voice":
        normalized = "audio"
    return normalized if normalized in _INPUT_MODES else "unknown"


def _normalize_time_bucket(value: Any) -> str:
    normalized = str(value or "")
    return normalized if normalized in {"00-06", "06-12", "12-18", "18-24"} else "00-06"


def _bucket_key(activity_state: str, input_mode: str, time_bucket: str) -> str:
    return f"{activity_state}|{input_mode}|{time_bucket}"
