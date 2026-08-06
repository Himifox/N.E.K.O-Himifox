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
    calculate_half_life_decay_factor,
    trim_oldest_outcomes,
)


PREFERENCE_STATE_VERSION = "recommendation_preference_state_v1"
PREFERENCE_STATE_FILENAME = "proactive_recommendation_preference_state_v1.json"
PREFERENCE_SCORE_CONTRACT = "source_preference_score_v2"
PREFERENCE_HALF_LIFE_SECONDS = 7 * 24 * 60 * 60
PREFERENCE_BETA_PRIOR_ALPHA = 2.0
PREFERENCE_BETA_PRIOR_BETA = 2.0
PREFERENCE_MIN_EVIDENCE = 0.25
PREFERENCE_SATURATION_EVIDENCE = 6.0
PREFERENCE_RESOURCE_SATURATION_EVIDENCE = 2.0
PREFERENCE_DELTA_PER_EVIDENCE = 0.005
PREFERENCE_MAX_ABS_DELTA = 0.03
PREFERENCE_RESOURCE_MAX_ABS_DELTA = 0.01
PREFERENCE_RECENT_OUTCOME_LIMIT = 2000

EXPLICIT_SOURCE_SIGNAL = "explicit_source"
RESOURCE_BEHAVIOR_SIGNAL = "resource_behavior"
_SIGNAL_BASES = {EXPLICIT_SOURCE_SIGNAL, RESOURCE_BEHAVIOR_SIGNAL}


def get_recommendation_preference_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a decayed, point-in-time state snapshot."""
    root = _resolve_config_directory(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return _build_source_preference_snapshot(
            _new_source_preference_state(), current
        )
    stored_state = _source_preference_store(root).read()
    return _build_source_preference_snapshot(stored_state, current)


def ensure_recommendation_preference_state(
    *,
    config_dir: str | os.PathLike[str] | None,
    legacy_preview: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Initialize the official state without guessing legacy signal provenance."""
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
            state["legacy_unclassified_seed_evidence"] = round(
                sum(
                    coerce_bounded_evidence_weight(
                        bucket.get("positive_evidence_count")
                    )
                    + coerce_bounded_evidence_weight(
                        bucket.get("negative_evidence_count")
                    )
                    for bucket in sources.values()
                    if isinstance(bucket, Mapping)
                ),
                6,
            )
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
    event_type: Any = None,
    outcome_strength: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply at most one classified source outcome per turn."""
    root = _resolve_config_directory(config_dir)
    turn = to_stripped_text(turn_id)
    source = normalize_source_identifier(source_type)
    current = time.time() if now is None else float(now)
    win = _coerce_reward_component(success)
    loss = _coerce_reward_component(failure)
    strength = _coerce_reward_component(
        max(win, loss) if outcome_strength is None else outcome_strength
    )
    signal_basis = (
        EXPLICIT_SOURCE_SIGNAL
        if explicit
        else RESOURCE_BEHAVIOR_SIGNAL if source == "music" else ""
    )
    if (
        root is None
        or not turn
        or not source
        or not signal_basis
        or (win == 0 and loss == 0)
    ):
        return get_recommendation_preference_state(
            config_dir=config_dir, now=current
        )

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

        outcomes[outcome_key] = {
            "turn_id": turn,
            "source_type": source,
            "event_type": to_stripped_text(event_type),
            "signal_basis": signal_basis,
            "success": win,
            "failure": loss,
            "priority": priority,
            "outcome_strength": strength,
            "recorded_at": current,
            "updated_at": current,
        }
        state["preference_score_contract"] = PREFERENCE_SCORE_CONTRACT
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


def preference_adjustments(
    state: Mapping[str, Any] | None,
) -> dict[str, float]:
    """Return the bounded adjustment from the registered v2 score contract."""
    if (
        not isinstance(state, Mapping)
        or state.get("version") != PREFERENCE_STATE_VERSION
        or state.get("preference_score_contract") != PREFERENCE_SCORE_CONTRACT
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


def _build_source_preference_snapshot(
    state: Mapping[str, Any], now: float
) -> dict[str, Any]:
    aggregates, migration = _aggregate_outcomes(state, now)
    public_sources: dict[str, dict[str, Any]] = {}
    for source, aggregate in sorted(aggregates.items()):
        explicit = aggregate[EXPLICIT_SOURCE_SIGNAL]
        behavior = aggregate[RESOURCE_BEHAVIOR_SIGNAL]
        explicit_evidence = explicit["success"] + explicit["failure"]
        behavior_evidence = behavior["success"] + behavior["failure"]

        if explicit_evidence > 0 and (
            source != "music"
            or explicit_evidence + 1e-9 >= PREFERENCE_MIN_EVIDENCE
        ):
            selected_basis = EXPLICIT_SOURCE_SIGNAL
            selected = explicit
            maximum_delta = PREFERENCE_MAX_ABS_DELTA
            saturation = PREFERENCE_SATURATION_EVIDENCE
        elif source == "music" and behavior_evidence > 0:
            selected_basis = RESOURCE_BEHAVIOR_SIGNAL
            selected = behavior
            maximum_delta = PREFERENCE_RESOURCE_MAX_ABS_DELTA
            saturation = PREFERENCE_RESOURCE_SATURATION_EVIDENCE
        else:
            selected_basis = "none"
            selected = {"success": 0.0, "failure": 0.0}
            maximum_delta = PREFERENCE_MAX_ABS_DELTA
            saturation = PREFERENCE_SATURATION_EVIDENCE

        success = float(selected["success"])
        failure = float(selected["failure"])
        evidence = success + failure
        alpha = PREFERENCE_BETA_PRIOR_ALPHA + success
        beta = PREFERENCE_BETA_PRIOR_BETA + failure
        direction = (success - failure) / evidence if evidence else 0.0
        confidence = min(1.0, evidence / saturation) if evidence else 0.0
        delta = clamp_to_range(
            (success - failure) * PREFERENCE_DELTA_PER_EVIDENCE,
            -maximum_delta,
            maximum_delta,
        )
        public_sources[source] = {
            "explicit_evidence": _public_evidence(explicit),
            "resource_behavior_evidence": _public_evidence(behavior),
            "selected_signal_basis": selected_basis,
            "effective_success": round(success, 6),
            "effective_failure": round(failure, 6),
            "effective_evidence": round(evidence, 6),
            "posterior_alpha": round(alpha, 6),
            "posterior_beta": round(beta, 6),
            "posterior_mean": round(alpha / (alpha + beta), 6),
            "direction": round(direction, 6),
            "confidence": round(confidence, 6),
            "personalization_delta": round(delta, 6),
            "updated_at": round(float(aggregate["updated_at"]), 6),
        }

    return {
        "version": PREFERENCE_STATE_VERSION,
        "preference_score_contract": PREFERENCE_SCORE_CONTRACT,
        "half_life_seconds": PREFERENCE_HALF_LIFE_SECONDS,
        "beta_prior": {
            "alpha": PREFERENCE_BETA_PRIOR_ALPHA,
            "beta": PREFERENCE_BETA_PRIOR_BETA,
        },
        "min_evidence": PREFERENCE_MIN_EVIDENCE,
        "saturation_evidence": PREFERENCE_SATURATION_EVIDENCE,
        "resource_behavior_saturation_evidence": (
            PREFERENCE_RESOURCE_SATURATION_EVIDENCE
        ),
        "delta_per_evidence": PREFERENCE_DELTA_PER_EVIDENCE,
        "max_abs_delta": PREFERENCE_MAX_ABS_DELTA,
        "resource_behavior_max_abs_delta": PREFERENCE_RESOURCE_MAX_ABS_DELTA,
        "legacy_replacement_approximation_count": max(
            0, int(state.get("legacy_replacement_approximation_count", 0))
        ),
        "migration": migration,
        "sources": public_sources,
    }


def _aggregate_outcomes(
    state: Mapping[str, Any], now: float
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = {}
    migrated_count = 0
    unclassified_count = 0
    outcomes = state.get("recent_source_outcomes")
    if isinstance(outcomes, Mapping):
        for outcome in outcomes.values():
            if not isinstance(outcome, Mapping):
                continue
            source = normalize_source_identifier(outcome.get("source_type"))
            if not source:
                unclassified_count += 1
                continue
            signal_basis = to_stripped_text(outcome.get("signal_basis"))
            legacy_outcome = signal_basis not in _SIGNAL_BASES
            if legacy_outcome:
                priority = int(coerce_finite_float(outcome.get("priority")))
                if priority >= 2:
                    signal_basis = EXPLICIT_SOURCE_SIGNAL
                elif source == "music":
                    signal_basis = RESOURCE_BEHAVIOR_SIGNAL
                else:
                    unclassified_count += 1
                    continue
                migrated_count += 1

            success = _coerce_reward_component(outcome.get("success"))
            failure = _coerce_reward_component(outcome.get("failure"))
            if legacy_outcome and signal_basis == RESOURCE_BEHAVIOR_SIGNAL:
                success *= 0.2
            recorded_at = max(
                0.0,
                coerce_finite_float(
                    outcome.get("recorded_at") or outcome.get("updated_at")
                ),
            )
            factor = (
                calculate_half_life_decay_factor(
                    recorded_at,
                    now,
                    half_life_seconds=PREFERENCE_HALF_LIFE_SECONDS,
                )
                if recorded_at
                else 1.0
            )
            aggregate = aggregates.setdefault(
                source,
                {
                    EXPLICIT_SOURCE_SIGNAL: {"success": 0.0, "failure": 0.0},
                    RESOURCE_BEHAVIOR_SIGNAL: {"success": 0.0, "failure": 0.0},
                    "updated_at": 0.0,
                },
            )
            bucket = aggregate[signal_basis]
            bucket["success"] += success * factor
            bucket["failure"] += failure * factor
            aggregate["updated_at"] = max(
                float(aggregate["updated_at"]), recorded_at
            )

    legacy_sources = state.get("sources")
    return aggregates, {
        "legacy_outcome_migration_count": migrated_count,
        "unclassified_outcome_count": unclassified_count,
        "legacy_aggregate_ignored": bool(legacy_sources),
        "legacy_unclassified_seed_evidence": round(
            coerce_bounded_evidence_weight(
                state.get("legacy_unclassified_seed_evidence")
            ),
            6,
        ),
    }


def _public_evidence(bucket: Mapping[str, Any]) -> dict[str, float]:
    success = coerce_bounded_evidence_weight(bucket.get("success"))
    failure = coerce_bounded_evidence_weight(bucket.get("failure"))
    return {
        "effective_success": round(success, 6),
        "effective_failure": round(failure, 6),
        "effective_evidence": round(success + failure, 6),
    }


def _sanitize_source_preference_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != PREFERENCE_STATE_VERSION:
        return _new_source_preference_state()
    legacy_sources: dict[str, dict[str, float]] = {}
    if isinstance(raw.get("sources"), Mapping):
        for raw_source, raw_bucket in raw["sources"].items():
            source = normalize_source_identifier(raw_source)
            if source and isinstance(raw_bucket, Mapping):
                legacy_sources[source] = {
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
        "preference_score_contract": PREFERENCE_SCORE_CONTRACT,
        "sources": legacy_sources,
        "recent_source_outcomes": outcomes,
        "legacy_replacement_approximation_count": int(
            coerce_bounded_evidence_weight(
                raw.get("legacy_replacement_approximation_count")
            )
        ),
        "legacy_unclassified_seed_evidence": (
            coerce_bounded_evidence_weight(
                raw.get("legacy_unclassified_seed_evidence")
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
        "preference_score_contract": PREFERENCE_SCORE_CONTRACT,
        "sources": {},
        "recent_source_outcomes": {},
        "legacy_replacement_approximation_count": 0,
        "legacy_unclassified_seed_evidence": 0.0,
    }


def _resolve_config_directory(
    value: str | os.PathLike[str] | None,
) -> Path | None:
    return Path(value).resolve() if value is not None else None


def _coerce_reward_component(value: Any) -> float:
    return clamp_to_unit_interval(coerce_finite_float(value))


__all__ = [
    "EXPLICIT_SOURCE_SIGNAL",
    "PREFERENCE_BETA_PRIOR_ALPHA",
    "PREFERENCE_BETA_PRIOR_BETA",
    "PREFERENCE_DELTA_PER_EVIDENCE",
    "PREFERENCE_HALF_LIFE_SECONDS",
    "PREFERENCE_MAX_ABS_DELTA",
    "PREFERENCE_MIN_EVIDENCE",
    "PREFERENCE_RESOURCE_MAX_ABS_DELTA",
    "PREFERENCE_RESOURCE_SATURATION_EVIDENCE",
    "PREFERENCE_SATURATION_EVIDENCE",
    "PREFERENCE_SCORE_CONTRACT",
    "PREFERENCE_STATE_FILENAME",
    "PREFERENCE_STATE_VERSION",
    "RESOURCE_BEHAVIOR_SIGNAL",
    "ensure_recommendation_preference_state",
    "get_recommendation_preference_state",
    "preference_adjustments",
    "reset_recommendation_preference_state",
    "update_recommendation_source_preference",
]
