"""Persistent encounter rewards for the constrained source bandit."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any

from main_logic.proactive_recommendation_preference import (
    PREFERENCE_BETA_PRIOR_ALPHA,
    PREFERENCE_BETA_PRIOR_BETA,
    PREFERENCE_HALF_LIFE_SECONDS,
)


BANDIT_STATE_VERSION = "recommendation_bandit_state_v1"
BANDIT_STATE_FILENAME = "proactive_recommendation_bandit_state_v1.json"
BANDIT_RECENT_OUTCOME_LIMIT = 2000

_lock = threading.RLock()


def get_recommendation_bandit_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a decayed point-in-time encounter-reward snapshot."""
    root = _config_root(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return _public_state(_empty_state(), current)
    with _lock:
        return _public_state(_load_state(root), current)


def update_recommendation_bandit_reward(
    *,
    config_dir: str | os.PathLike[str] | None,
    turn_id: Any,
    arm: Any,
    reward: Any,
    event_types: Iterable[Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Replace one turn/arm aggregate when its unique event set changes."""
    root = _config_root(config_dir)
    turn = _text(turn_id)
    source = _source_name(arm)
    current = time.time() if now is None else float(now)
    score = _clamp(_finite(reward), -1.0, 1.0)
    signature = tuple(sorted({_text(value) for value in event_types if _text(value)}))
    if root is None or not turn or not source or not signature:
        return get_recommendation_bandit_state(config_dir=config_dir, now=current)

    with _lock:
        state = _load_state(root)
        outcomes = state["recent_arm_outcomes"]
        outcome_key = f"{turn}|{source}"
        previous = outcomes.get(outcome_key)
        if isinstance(previous, Mapping) and tuple(previous.get("event_types", ())) == signature:
            return _public_state(state, current)

        bucket = state["arms"].setdefault(source, _empty_bucket(current))
        state_now = max(current, max(0.0, _finite(bucket.get("updated_at"))))
        _decay_bucket(bucket, state_now)
        if isinstance(previous, Mapping):
            recorded_at = max(0.0, _finite(previous.get("recorded_at")))
            factor = _decay_factor(recorded_at, state_now) if recorded_at else 1.0
            bucket["effective_success"] = max(
                0.0,
                float(bucket["effective_success"])
                - _bounded_count(previous.get("success")) * factor,
            )
            bucket["effective_failure"] = max(
                0.0,
                float(bucket["effective_failure"])
                - _bounded_count(previous.get("failure")) * factor,
            )

        success = max(0.0, score)
        failure = max(0.0, -score)
        bucket["effective_success"] += success
        bucket["effective_failure"] += failure
        bucket["updated_at"] = state_now
        outcomes[outcome_key] = {
            "turn_id": turn,
            "arm": source,
            "reward": score,
            "success": success,
            "failure": failure,
            "event_types": list(signature),
            "recorded_at": state_now,
        }
        _trim_outcomes(outcomes)
        _save_state(root, state)
        return _public_state(state, state_now)


def reset_recommendation_bandit_state(
    *, config_dir: str | os.PathLike[str] | None
) -> bool:
    root = _config_root(config_dir)
    if root is None:
        return False
    with _lock:
        try:
            (root / BANDIT_STATE_FILENAME).unlink(missing_ok=True)
            return True
        except OSError:
            return False


def _public_state(state: Mapping[str, Any], now: float) -> dict[str, Any]:
    arms: dict[str, dict[str, float]] = {}
    raw_arms = state.get("arms")
    if isinstance(raw_arms, Mapping):
        for raw_arm, raw_bucket in raw_arms.items():
            arm = _source_name(raw_arm)
            if not arm or not isinstance(raw_bucket, Mapping):
                continue
            bucket = dict(raw_bucket)
            _decay_bucket(bucket, now)
            success = _bounded_count(bucket.get("effective_success"))
            failure = _bounded_count(bucket.get("effective_failure"))
            alpha = PREFERENCE_BETA_PRIOR_ALPHA + success
            beta = PREFERENCE_BETA_PRIOR_BETA + failure
            arms[arm] = {
                "effective_success": round(success, 6),
                "effective_failure": round(failure, 6),
                "effective_evidence": round(success + failure, 6),
                "posterior_alpha": round(alpha, 6),
                "posterior_beta": round(beta, 6),
                "posterior_mean": round(alpha / (alpha + beta), 6),
                "updated_at": round(max(0.0, _finite(bucket.get("updated_at"))), 6),
            }
    outcomes = state.get("recent_arm_outcomes")
    return {
        "version": BANDIT_STATE_VERSION,
        "half_life_seconds": PREFERENCE_HALF_LIFE_SECONDS,
        "beta_prior": {
            "alpha": PREFERENCE_BETA_PRIOR_ALPHA,
            "beta": PREFERENCE_BETA_PRIOR_BETA,
        },
        "arms": arms,
        "finalized_outcome_count": len(outcomes) if isinstance(outcomes, Mapping) else 0,
    }


def _load_state(root: Path) -> dict[str, Any]:
    path = root / BANDIT_STATE_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _empty_state()
    if not isinstance(raw, Mapping) or raw.get("version") != BANDIT_STATE_VERSION:
        return _empty_state()
    arms: dict[str, dict[str, float]] = {}
    if isinstance(raw.get("arms"), Mapping):
        for raw_arm, raw_bucket in raw["arms"].items():
            arm = _source_name(raw_arm)
            if arm and isinstance(raw_bucket, Mapping):
                arms[arm] = {
                    "effective_success": _bounded_count(raw_bucket.get("effective_success")),
                    "effective_failure": _bounded_count(raw_bucket.get("effective_failure")),
                    "updated_at": max(0.0, _finite(raw_bucket.get("updated_at"))),
                }
    outcomes: dict[str, dict[str, Any]] = {}
    if isinstance(raw.get("recent_arm_outcomes"), Mapping):
        for key, value in raw["recent_arm_outcomes"].items():
            if isinstance(value, Mapping):
                outcomes[str(key)[:256]] = dict(value)
    return {
        "version": BANDIT_STATE_VERSION,
        "arms": arms,
        "recent_arm_outcomes": outcomes,
    }


def _save_state(root: Path, state: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / BANDIT_STATE_FILENAME
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def _decay_bucket(bucket: dict[str, Any], now: float) -> None:
    updated_at = max(0.0, _finite(bucket.get("updated_at")))
    factor = _decay_factor(updated_at, now) if updated_at else 1.0
    bucket["effective_success"] = _bounded_count(bucket.get("effective_success")) * factor
    bucket["effective_failure"] = _bounded_count(bucket.get("effective_failure")) * factor
    bucket["updated_at"] = now


def _trim_outcomes(outcomes: dict[str, dict[str, Any]]) -> None:
    overflow = len(outcomes) - BANDIT_RECENT_OUTCOME_LIMIT
    if overflow <= 0:
        return
    oldest = sorted(
        outcomes,
        key=lambda key: _finite(outcomes[key].get("recorded_at")),
    )[:overflow]
    for key in oldest:
        outcomes.pop(key, None)


def _empty_state() -> dict[str, Any]:
    return {
        "version": BANDIT_STATE_VERSION,
        "arms": {},
        "recent_arm_outcomes": {},
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


def _bounded_count(value: Any) -> float:
    return _clamp(_finite(value), 0.0, 1_000_000.0)


def _decay_factor(recorded_at: float, now: float) -> float:
    elapsed = max(0.0, now - max(0.0, recorded_at))
    return 0.5 ** (elapsed / PREFERENCE_HALF_LIFE_SECONDS)


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


__all__ = [
    "BANDIT_STATE_FILENAME",
    "BANDIT_STATE_VERSION",
    "get_recommendation_bandit_state",
    "reset_recommendation_bandit_state",
    "update_recommendation_bandit_reward",
]
