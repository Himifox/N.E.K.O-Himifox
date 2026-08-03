"""Persistent encounter rewards for the constrained source bandit."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
from pathlib import Path
import time
from typing import Any

from main_logic.proactive_recommendation.state.source_preferences import (
    PREFERENCE_BETA_PRIOR_ALPHA,
    PREFERENCE_BETA_PRIOR_BETA,
    PREFERENCE_HALF_LIFE_SECONDS,
)
from main_logic.proactive_recommendation.normalization import (
    clamp_to_range,
    coerce_bounded_evidence_weight,
    coerce_finite_float,
    normalize_source_identifier,
    to_stripped_text,
)
from main_logic.proactive_recommendation.persistence import AtomicJsonStore
from main_logic.proactive_recommendation.state.decay import (
    apply_decay_to_evidence_bucket,
    build_empty_evidence_bucket,
    calculate_half_life_decay_factor,
    trim_oldest_outcomes,
)


BANDIT_STATE_VERSION = "recommendation_bandit_state_v1"
BANDIT_STATE_FILENAME = "proactive_recommendation_bandit_state_v1.json"
BANDIT_RECENT_OUTCOME_LIMIT = 2000

def get_recommendation_bandit_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a decayed point-in-time encounter-reward snapshot."""
    root = _resolve_config_directory(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return _build_bandit_posterior_snapshot(_new_bandit_posterior_state(), current)
    stored_state = _bandit_posterior_store(root).read()
    return _build_bandit_posterior_snapshot(stored_state, current)


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
    root = _resolve_config_directory(config_dir)
    turn = to_stripped_text(turn_id)
    source = normalize_source_identifier(arm)
    current = time.time() if now is None else float(now)
    score = clamp_to_range(coerce_finite_float(reward), -1.0, 1.0)
    signature = tuple(sorted({to_stripped_text(value) for value in event_types if to_stripped_text(value)}))
    if root is None or not turn or not source or not signature:
        return get_recommendation_bandit_state(config_dir=config_dir, now=current)

    def apply_reward(state: dict[str, Any]) -> dict[str, Any]:
        outcomes = state["recent_arm_outcomes"]
        outcome_key = f"{turn}|{source}"
        previous = outcomes.get(outcome_key)
        if (
            isinstance(previous, Mapping)
            and tuple(previous.get("event_types", ())) == signature
        ):
            return state

        bucket = state["arms"].setdefault(source, build_empty_evidence_bucket(current))
        state_now = max(current, max(0.0, coerce_finite_float(bucket.get("updated_at"))))
        apply_decay_to_evidence_bucket(
            bucket,
            state_now,
            half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
        )
        if isinstance(previous, Mapping):
            recorded_at = max(0.0, coerce_finite_float(previous.get("recorded_at")))
            factor = (
                calculate_half_life_decay_factor(
                    recorded_at,
                    state_now,
                    half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
                )
                if recorded_at
                else 1.0
            )
            bucket["effective_success"] = max(
                0.0,
                float(bucket["effective_success"])
                - coerce_bounded_evidence_weight(previous.get("success")) * factor,
            )
            bucket["effective_failure"] = max(
                0.0,
                float(bucket["effective_failure"])
                - coerce_bounded_evidence_weight(previous.get("failure")) * factor,
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
        trim_oldest_outcomes(
            outcomes,
            maximum_count=BANDIT_RECENT_OUTCOME_LIMIT,
            timestamp_field="recorded_at",
        )
        return state

    stored_state = _bandit_posterior_store(root).update(apply_reward)
    state_now = max(
        current,
        max(
            (
                coerce_finite_float(bucket.get("updated_at"))
                for bucket in stored_state.get("arms", {}).values()
                if isinstance(bucket, Mapping)
            ),
            default=current,
        ),
    )
    return _build_bandit_posterior_snapshot(stored_state, state_now)


def reset_recommendation_bandit_state(
    *, config_dir: str | os.PathLike[str] | None
) -> bool:
    root = _resolve_config_directory(config_dir)
    if root is None:
        return False
    try:
        return _bandit_posterior_store(root).delete()
    except OSError:
        return False


def _build_bandit_posterior_snapshot(state: Mapping[str, Any], now: float) -> dict[str, Any]:
    arms: dict[str, dict[str, float]] = {}
    raw_arms = state.get("arms")
    if isinstance(raw_arms, Mapping):
        for raw_arm, raw_bucket in raw_arms.items():
            arm = normalize_source_identifier(raw_arm)
            if not arm or not isinstance(raw_bucket, Mapping):
                continue
            bucket = dict(raw_bucket)
            apply_decay_to_evidence_bucket(
                bucket,
                now,
                half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
            )
            success = coerce_bounded_evidence_weight(bucket.get("effective_success"))
            failure = coerce_bounded_evidence_weight(bucket.get("effective_failure"))
            alpha = PREFERENCE_BETA_PRIOR_ALPHA + success
            beta = PREFERENCE_BETA_PRIOR_BETA + failure
            arms[arm] = {
                "effective_success": round(success, 6),
                "effective_failure": round(failure, 6),
                "effective_evidence": round(success + failure, 6),
                "posterior_alpha": round(alpha, 6),
                "posterior_beta": round(beta, 6),
                "posterior_mean": round(alpha / (alpha + beta), 6),
                "updated_at": round(max(0.0, coerce_finite_float(bucket.get("updated_at"))), 6),
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
        "finalized_outcome_count": len(outcomes)
        if isinstance(outcomes, Mapping)
        else 0,
    }


def _sanitize_bandit_posterior_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != BANDIT_STATE_VERSION:
        return _new_bandit_posterior_state()
    arms: dict[str, dict[str, float]] = {}
    if isinstance(raw.get("arms"), Mapping):
        for raw_arm, raw_bucket in raw["arms"].items():
            arm = normalize_source_identifier(raw_arm)
            if arm and isinstance(raw_bucket, Mapping):
                arms[arm] = {
                    "effective_success": coerce_bounded_evidence_weight(
                        raw_bucket.get("effective_success")
                    ),
                    "effective_failure": coerce_bounded_evidence_weight(
                        raw_bucket.get("effective_failure")
                    ),
                    "updated_at": max(
                        0.0,
                        coerce_finite_float(raw_bucket.get("updated_at")),
                    ),
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


def _bandit_posterior_store(root: Path) -> AtomicJsonStore[dict[str, Any]]:
    return AtomicJsonStore(
        root / BANDIT_STATE_FILENAME,
        default_factory=_new_bandit_posterior_state,
        sanitizer=_sanitize_bandit_posterior_state,
    )


def _new_bandit_posterior_state() -> dict[str, Any]:
    return {
        "version": BANDIT_STATE_VERSION,
        "arms": {},
        "recent_arm_outcomes": {},
    }


def _resolve_config_directory(value: str | os.PathLike[str] | None) -> Path | None:
    return Path(value).resolve() if value is not None else None




__all__ = [
    "BANDIT_STATE_FILENAME",
    "BANDIT_STATE_VERSION",
    "get_recommendation_bandit_state",
    "reset_recommendation_bandit_state",
    "update_recommendation_bandit_reward",
]
