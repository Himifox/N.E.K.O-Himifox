"""Persistent preference evidence for proactive recommendation policies."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import time
from typing import Any

from main_logic.proactive_recommendation.normalization import (
    clamp_to_range,
    clamp_to_unit_interval,
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


PREFERENCE_STATE_VERSION = "recommendation_preference_state_v1"
PREFERENCE_STATE_FILENAME = "proactive_recommendation_preference_state_v1.json"
PREFERENCE_HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
PREFERENCE_BETA_PRIOR_ALPHA = 2.0
PREFERENCE_BETA_PRIOR_BETA = 2.0
PREFERENCE_MIN_EVIDENCE = 3.0
PREFERENCE_SATURATION_EVIDENCE = 12.0
PREFERENCE_MAX_ABS_DELTA = 0.03
PREFERENCE_RECENT_OUTCOME_LIMIT = 2000

def get_recommendation_preference_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a decayed, point-in-time state snapshot."""
    root = _resolve_config_directory(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return _build_source_preference_snapshot(_new_source_preference_state(), current)
    stored_state = _source_preference_store(root).read()
    return _build_source_preference_snapshot(stored_state, current)


def ensure_recommendation_preference_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    legacy_preview: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Seed the official state once from the existing v2 aggregate contract."""
    root = _resolve_config_directory(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return get_recommendation_preference_state(config_dir=None, now=current)
    def build_seed_state() -> dict[str, Any]:
        state = _new_source_preference_state()
        affinity = (
            legacy_preview.get("source_affinity")
            if isinstance(legacy_preview, Mapping)
            and legacy_preview.get("version") == "feedback_state_preview_v2"
            else None
        )
        persistent = (
            affinity.get("persistent") if isinstance(affinity, Mapping) else None
        )
        sources = persistent.get("sources") if isinstance(persistent, Mapping) else None
        if isinstance(sources, Mapping):
            for raw_source, raw_bucket in sources.items():
                source = normalize_source_identifier(raw_source)
                if source and isinstance(raw_bucket, Mapping):
                    state["sources"][source] = {
                        "effective_success": coerce_bounded_evidence_weight(
                            raw_bucket.get("positive_evidence_count")
                        ),
                        "effective_failure": coerce_bounded_evidence_weight(
                            raw_bucket.get("negative_evidence_count")
                        ),
                        # v2 counters had no decay contract. Start the new
                        # 30-day clock at migration so existing evidence is
                        # preserved instead of being retroactively expired.
                        "updated_at": current,
                    }
        return state

    stored_state = _source_preference_store(root).initialize_if_missing(
        build_seed_state
    )
    return _build_source_preference_snapshot(stored_state, current)


def update_recommendation_source_preference(
    *,
    config_dir: str | os.PathLike[str] | None,
    turn_id: Any,
    source_type: Any,
    success: float,
    failure: float,
    explicit: bool,
    outcome_strength: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply at most one source outcome per turn, with explicit feedback winning."""
    root = _resolve_config_directory(config_dir)
    turn = to_stripped_text(turn_id)
    source = normalize_source_identifier(source_type)
    current = time.time() if now is None else float(now)
    win = _coerce_reward_component(success)
    loss = _coerce_reward_component(failure)
    strength = _coerce_reward_component(
        max(win, loss) if outcome_strength is None else outcome_strength
    )
    if root is None or not turn or not source or (win == 0 and loss == 0):
        return get_recommendation_preference_state(config_dir=config_dir, now=current)

    def apply_outcome(state: dict[str, Any]) -> dict[str, Any]:
        outcomes = state["recent_source_outcomes"]
        outcome_key = f"{turn}|{source}"
        previous = outcomes.get(outcome_key)
        priority = 2 if explicit else 1
        if isinstance(previous, Mapping):
            previous_priority = int(previous.get("priority", 0))
            previous_strength = _coerce_reward_component(
                previous.get(
                    "outcome_strength",
                    max(
                        _coerce_reward_component(previous.get("success")),
                        _coerce_reward_component(previous.get("failure")),
                    ),
                )
            )
            if previous_priority > priority or (
                previous_priority == priority and previous_strength >= strength
            ):
                return state

        bucket = state["sources"].setdefault(source, build_empty_evidence_bucket(current))
        apply_decay_to_evidence_bucket(
            bucket,
            current,
            half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
        )
        if isinstance(previous, Mapping):
            recorded_at = max(0.0, coerce_finite_float(previous.get("recorded_at")))
            if recorded_at <= 0.0:
                recorded_at = max(0.0, coerce_finite_float(previous.get("updated_at")))
            if recorded_at <= 0.0:
                previous_factor = 1.0
                state["legacy_replacement_approximation_count"] = (
                    int(state.get("legacy_replacement_approximation_count", 0)) + 1
                )
            else:
                previous_factor = calculate_half_life_decay_factor(
                    recorded_at,
                    current,
                    half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
                )
            bucket["effective_success"] = max(
                0.0,
                float(bucket["effective_success"])
                - _coerce_reward_component(previous.get("success")) * previous_factor,
            )
            bucket["effective_failure"] = max(
                0.0,
                float(bucket["effective_failure"])
                - _coerce_reward_component(previous.get("failure")) * previous_factor,
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
            "outcome_strength": strength,
            "recorded_at": current,
            "updated_at": current,
        }
        trim_oldest_outcomes(
            outcomes,
            maximum_count=PREFERENCE_RECENT_OUTCOME_LIMIT,
            timestamp_field="updated_at",
        )
        return state

    stored_state = _source_preference_store(root).update(apply_outcome)
    return _build_source_preference_snapshot(stored_state, current)


def reset_recommendation_preference_state(
    *, config_dir: str | os.PathLike[str] | None
) -> bool:
    root = _resolve_config_directory(config_dir)
    if root is None:
        return False
    try:
        return _source_preference_store(root).delete()
    except OSError:
        return False


def preference_adjustments(state: Mapping[str, Any] | None) -> dict[str, float]:
    """Return the registered gradual_12 mapping from an official snapshot."""
    if (
        not isinstance(state, Mapping)
        or state.get("version") != PREFERENCE_STATE_VERSION
    ):
        return {}
    sources = state.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    result: dict[str, float] = {}
    for raw_source, raw_bucket in sources.items():
        source = normalize_source_identifier(raw_source)
        if not source or not isinstance(raw_bucket, Mapping):
            continue
        result[source] = round(
            clamp_to_range(
                coerce_finite_float(raw_bucket.get("personalization_delta")),
                -PREFERENCE_MAX_ABS_DELTA,
                PREFERENCE_MAX_ABS_DELTA,
            ),
            6,
        )
    return result


def _build_source_preference_snapshot(state: Mapping[str, Any], now: float) -> dict[str, Any]:
    public_sources: dict[str, dict[str, Any]] = {}
    raw_sources = state.get("sources")
    if isinstance(raw_sources, Mapping):
        for raw_source, raw_bucket in sorted(raw_sources.items()):
            source = normalize_source_identifier(raw_source)
            if not source or not isinstance(raw_bucket, Mapping):
                continue
            bucket = dict(raw_bucket)
            apply_decay_to_evidence_bucket(
                bucket,
                now,
                half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
            )
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
                    clamp_to_range(delta, -PREFERENCE_MAX_ABS_DELTA, PREFERENCE_MAX_ABS_DELTA),
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
        "legacy_replacement_approximation_count": max(
            0, int(state.get("legacy_replacement_approximation_count", 0))
        ),
        "sources": public_sources,
    }


def _sanitize_source_preference_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != PREFERENCE_STATE_VERSION:
        return _new_source_preference_state()
    sources: dict[str, dict[str, float]] = {}
    if isinstance(raw.get("sources"), Mapping):
        for raw_source, raw_bucket in raw["sources"].items():
            source = normalize_source_identifier(raw_source)
            if source and isinstance(raw_bucket, Mapping):
                sources[source] = {
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
    if isinstance(raw.get("recent_source_outcomes"), Mapping):
        for key, value in raw["recent_source_outcomes"].items():
            if isinstance(value, Mapping):
                outcomes[str(key)[:256]] = dict(value)
    return {
        "version": PREFERENCE_STATE_VERSION,
        "sources": sources,
        "recent_source_outcomes": outcomes,
        "legacy_replacement_approximation_count": int(
            coerce_bounded_evidence_weight(
                raw.get("legacy_replacement_approximation_count")
            )
        ),
    }


def _source_preference_store(root: Path) -> AtomicJsonStore[dict[str, Any]]:
    return AtomicJsonStore(
        root / PREFERENCE_STATE_FILENAME,
        default_factory=_new_source_preference_state,
        sanitizer=_sanitize_source_preference_state,
    )


def _new_source_preference_state() -> dict[str, Any]:
    return {
        "version": PREFERENCE_STATE_VERSION,
        "sources": {},
        "recent_source_outcomes": {},
        "legacy_replacement_approximation_count": 0,
    }


def _resolve_config_directory(value: str | os.PathLike[str] | None) -> Path | None:
    return Path(value).resolve() if value is not None else None


def _coerce_reward_component(value: Any) -> float:
    return clamp_to_unit_interval(coerce_finite_float(value))


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
