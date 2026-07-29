"""Persistent preference evidence for proactive recommendation policies."""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any


PREFERENCE_STATE_VERSION = "recommendation_preference_state_v1"
PREFERENCE_STATE_FILENAME = "proactive_recommendation_preference_state_v1.json"
PREFERENCE_HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
PREFERENCE_BETA_PRIOR_ALPHA = 2.0
PREFERENCE_BETA_PRIOR_BETA = 2.0
PREFERENCE_MIN_EVIDENCE = 3.0
PREFERENCE_SATURATION_EVIDENCE = 12.0
PREFERENCE_MAX_ABS_DELTA = 0.03
PREFERENCE_RECENT_OUTCOME_LIMIT = 2000

_lock = threading.RLock()


def get_recommendation_preference_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a decayed, point-in-time state snapshot."""
    root = _config_root(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return _public_state(_empty_state(), current)
    with _lock:
        return _public_state(_load_state(root), current)


def ensure_recommendation_preference_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    legacy_preview: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Seed the official state once from the existing v2 aggregate contract."""
    root = _config_root(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return get_recommendation_preference_state(config_dir=None, now=current)
    path = root / PREFERENCE_STATE_FILENAME
    with _lock:
        if path.exists():
            return _public_state(_load_state(root), current)
        state = _empty_state()
        affinity = (
            legacy_preview.get("source_affinity")
            if isinstance(legacy_preview, Mapping)
            and legacy_preview.get("version") == "feedback_state_preview_v2"
            else None
        )
        persistent = affinity.get("persistent") if isinstance(affinity, Mapping) else None
        sources = persistent.get("sources") if isinstance(persistent, Mapping) else None
        if isinstance(sources, Mapping):
            for raw_source, raw_bucket in sources.items():
                source = _source_name(raw_source)
                if source and isinstance(raw_bucket, Mapping):
                    state["sources"][source] = {
                        "effective_success": _bounded_count(
                            raw_bucket.get("positive_evidence_count")
                        ),
                        "effective_failure": _bounded_count(
                            raw_bucket.get("negative_evidence_count")
                        ),
                        "updated_at": max(
                            0.0, _finite(raw_bucket.get("updated_at"))
                        )
                        or current,
                    }
        _save_state(root, state)
        return _public_state(state, current)


def update_recommendation_source_preference(
    *,
    config_dir: str | os.PathLike[str] | None,
    turn_id: Any,
    source_type: Any,
    success: float,
    failure: float,
    explicit: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply at most one source outcome per turn, with explicit feedback winning."""
    root = _config_root(config_dir)
    turn = _text(turn_id)
    source = _source_name(source_type)
    current = time.time() if now is None else float(now)
    win = _bounded_reward(success)
    loss = _bounded_reward(failure)
    if root is None or not turn or not source or (win == 0 and loss == 0):
        return get_recommendation_preference_state(config_dir=config_dir, now=current)

    with _lock:
        state = _load_state(root)
        outcomes = state["recent_source_outcomes"]
        outcome_key = f"{turn}|{source}"
        previous = outcomes.get(outcome_key)
        priority = 2 if explicit else 1
        if isinstance(previous, Mapping) and int(previous.get("priority", 0)) >= priority:
            return _public_state(state, current)

        bucket = state["sources"].setdefault(source, _empty_bucket(current))
        _decay_bucket(bucket, current)
        if isinstance(previous, Mapping):
            bucket["effective_success"] = max(
                0.0,
                float(bucket["effective_success"])
                - _bounded_reward(previous.get("success")),
            )
            bucket["effective_failure"] = max(
                0.0,
                float(bucket["effective_failure"])
                - _bounded_reward(previous.get("failure")),
            )
        bucket["effective_success"] += win
        bucket["effective_failure"] += loss
        bucket["updated_at"] = current
        outcomes[outcome_key] = {
            "turn_id": turn,
            "source_type": source,
            "success": win,
            "failure": loss,
            "priority": priority,
            "updated_at": current,
        }
        _trim_outcomes(outcomes)
        _save_state(root, state)
        return _public_state(state, current)


def reset_recommendation_preference_state(
    *, config_dir: str | os.PathLike[str] | None
) -> bool:
    root = _config_root(config_dir)
    if root is None:
        return False
    with _lock:
        try:
            (root / PREFERENCE_STATE_FILENAME).unlink(missing_ok=True)
            return True
        except OSError:
            return False


def preference_adjustments(state: Mapping[str, Any] | None) -> dict[str, float]:
    """Return the registered gradual_12 mapping from an official snapshot."""
    if not isinstance(state, Mapping) or state.get("version") != PREFERENCE_STATE_VERSION:
        return {}
    sources = state.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    result: dict[str, float] = {}
    for raw_source, raw_bucket in sources.items():
        source = _source_name(raw_source)
        if not source or not isinstance(raw_bucket, Mapping):
            continue
        result[source] = round(
            _clamp(
                _finite(raw_bucket.get("personalization_delta")),
                -PREFERENCE_MAX_ABS_DELTA,
                PREFERENCE_MAX_ABS_DELTA,
            ),
            6,
        )
    return result


def _public_state(state: Mapping[str, Any], now: float) -> dict[str, Any]:
    public_sources: dict[str, dict[str, Any]] = {}
    raw_sources = state.get("sources")
    if isinstance(raw_sources, Mapping):
        for raw_source, raw_bucket in sorted(raw_sources.items()):
            source = _source_name(raw_source)
            if not source or not isinstance(raw_bucket, Mapping):
                continue
            bucket = dict(raw_bucket)
            _decay_bucket(bucket, now)
            success = float(bucket["effective_success"])
            failure = float(bucket["effective_failure"])
            evidence = success + failure
            alpha = PREFERENCE_BETA_PRIOR_ALPHA + success
            beta = PREFERENCE_BETA_PRIOR_BETA + failure
            posterior_mean = alpha / (alpha + beta)
            direction = (success - failure) / evidence if evidence else 0.0
            confidence = (
                min(1.0, evidence / PREFERENCE_SATURATION_EVIDENCE)
                if evidence + 1e-6 >= PREFERENCE_MIN_EVIDENCE
                else 0.0
            )
            delta = direction * confidence * PREFERENCE_MAX_ABS_DELTA
            public_sources[source] = {
                "effective_success": round(success, 6),
                "effective_failure": round(failure, 6),
                "effective_evidence": round(evidence, 6),
                "posterior_alpha": round(alpha, 6),
                "posterior_beta": round(beta, 6),
                "posterior_mean": round(posterior_mean, 6),
                "direction": round(direction, 6),
                "confidence": round(confidence, 6),
                "personalization_delta": round(
                    _clamp(delta, -PREFERENCE_MAX_ABS_DELTA, PREFERENCE_MAX_ABS_DELTA),
                    6,
                ),
                "updated_at": round(float(bucket["updated_at"]), 6),
            }
    return {
        "version": PREFERENCE_STATE_VERSION,
        "half_life_seconds": PREFERENCE_HALF_LIFE_SECONDS,
        "beta_prior": {
            "alpha": PREFERENCE_BETA_PRIOR_ALPHA,
            "beta": PREFERENCE_BETA_PRIOR_BETA,
        },
        "min_evidence": PREFERENCE_MIN_EVIDENCE,
        "saturation_evidence": PREFERENCE_SATURATION_EVIDENCE,
        "max_abs_delta": PREFERENCE_MAX_ABS_DELTA,
        "sources": public_sources,
    }


def _load_state(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / PREFERENCE_STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(raw, Mapping) or raw.get("version") != PREFERENCE_STATE_VERSION:
        return _empty_state()
    sources: dict[str, dict[str, float]] = {}
    if isinstance(raw.get("sources"), Mapping):
        for raw_source, raw_bucket in raw["sources"].items():
            source = _source_name(raw_source)
            if source and isinstance(raw_bucket, Mapping):
                sources[source] = {
                    "effective_success": _bounded_count(raw_bucket.get("effective_success")),
                    "effective_failure": _bounded_count(raw_bucket.get("effective_failure")),
                    "updated_at": max(0.0, _finite(raw_bucket.get("updated_at"))),
                }
    outcomes: dict[str, dict[str, Any]] = {}
    if isinstance(raw.get("recent_source_outcomes"), Mapping):
        for key, value in raw["recent_source_outcomes"].items():
            if isinstance(value, Mapping):
                outcomes[str(key)[:256]] = dict(value)
    return {
        "version": PREFERENCE_STATE_VERSION,
        "sources": sources,
        "recent_source_outcomes": outcomes,
    }


def _save_state(root: Path, state: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / PREFERENCE_STATE_FILENAME
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def _decay_bucket(bucket: dict[str, Any], now: float) -> None:
    updated_at = max(0.0, _finite(bucket.get("updated_at")))
    elapsed = max(0.0, now - updated_at) if updated_at else 0.0
    factor = 0.5 ** (elapsed / PREFERENCE_HALF_LIFE_SECONDS)
    bucket["effective_success"] = _bounded_count(bucket.get("effective_success")) * factor
    bucket["effective_failure"] = _bounded_count(bucket.get("effective_failure")) * factor
    bucket["updated_at"] = now if updated_at else now


def _trim_outcomes(outcomes: dict[str, dict[str, Any]]) -> None:
    if len(outcomes) <= PREFERENCE_RECENT_OUTCOME_LIMIT:
        return
    oldest = sorted(
        outcomes,
        key=lambda key: _finite(outcomes[key].get("updated_at")),
    )[: len(outcomes) - PREFERENCE_RECENT_OUTCOME_LIMIT]
    for key in oldest:
        outcomes.pop(key, None)


def _empty_state() -> dict[str, Any]:
    return {
        "version": PREFERENCE_STATE_VERSION,
        "sources": {},
        "recent_source_outcomes": {},
    }


def _empty_bucket(now: float) -> dict[str, float]:
    return {"effective_success": 0.0, "effective_failure": 0.0, "updated_at": now}


def _config_root(value: str | os.PathLike[str] | None) -> Path | None:
    return Path(value).resolve() if value is not None else None


def _source_name(value: Any) -> str:
    source = _text(value).lower()
    return source if source and source.replace("_", "").isalnum() else ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_reward(value: Any) -> float:
    return _clamp(_finite(value), 0.0, 1.0)


def _bounded_count(value: Any) -> float:
    return _clamp(_finite(value), 0.0, 1_000_000.0)


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


__all__ = [
    "PREFERENCE_BETA_PRIOR_ALPHA",
    "PREFERENCE_BETA_PRIOR_BETA",
    "PREFERENCE_HALF_LIFE_SECONDS",
    "PREFERENCE_MAX_ABS_DELTA",
    "PREFERENCE_MIN_EVIDENCE",
    "PREFERENCE_SATURATION_EVIDENCE",
    "PREFERENCE_STATE_FILENAME",
    "PREFERENCE_STATE_VERSION",
    "get_recommendation_preference_state",
    "ensure_recommendation_preference_state",
    "preference_adjustments",
    "reset_recommendation_preference_state",
    "update_recommendation_source_preference",
]
