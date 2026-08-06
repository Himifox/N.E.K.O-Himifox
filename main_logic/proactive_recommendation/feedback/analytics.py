"""Feedback joins, calibration, and availability aggregates."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
import hashlib
import logging
import math
import os
from pathlib import Path
from statistics import median
import time
from typing import Any

from config import PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE

from ..normalization import (
    clamp_to_range,
    coerce_float_or_default,
    rounded_mean_or_none,
    rounded_ratio_or_none,
    to_stripped_text,
)
from ..persistence import AtomicJsonStore
from .event_processing import (
    QUALITY_FEEDBACK_SCORE_VERSION,
    build_feedback_event,
    normalize_feedback_source_identifier,
    quality_feedback_score,
    sanitize_recommendation_feedback_event,
)
from .learning import (
    REWARD_SCORE_V4_PREVIEW_VERSION,
    build_reward_score_v2_preview,
    build_reward_score_v4_preview,
)

FEEDBACK_SCORE_VERSION = "report_score_v1"

REWARD_SCORE_V2_PREVIEW_VERSION = "reward_score_v2_preview_v2"

REPLY_WINDOW_SECONDS = 10 * 60

REPLY_SPEED_BASELINE_MIN_SAMPLES = 5

REPLY_SPEED_BONUS_MAX = 0.05

REPLY_SPEED_LOG_SCALE_FLOOR = 0.25

_SOURCE_REJECTION_SCORE = -0.35

_SOURCE_FATIGUE_SCORE = -0.20

_CANDIDATE_REJECTION_SCORE = -0.10

_FEEDBACK_EVENT_SCORES: dict[str, tuple[str, float, str]] = {
    "user_reply_fast": ("generic_engagement", 0.25, "medium"),
    "user_reply": ("generic_engagement", 0.15, "medium"),
    "user_continue": ("generic_engagement", 0.35, "medium"),
    "ignored": ("generic_engagement", -0.05, "low"),
    "proactive_disabled_after": ("settings", -0.70, "high"),
    "source_disabled_after": ("settings", -0.35, "medium"),
    "source_not_interested": ("source_preference", _SOURCE_REJECTION_SCORE, "high"),
    "source_fatigue": ("source_preference", _SOURCE_FATIGUE_SCORE, "medium"),
    "candidate_not_interested": (
        "source_preference",
        _CANDIDATE_REJECTION_SCORE,
        "low",
    ),
    "source_interested": ("source_preference", 0.35, "high"),
    "music_played_through": ("music", 0.90, "high"),
    "music_high_completion": ("music", 0.65, "high"),
    "music_mid_completion": ("music", 0.25, "medium"),
    "music_normal_close": ("music", 0.05, "low"),
    "music_early_close": ("music", -0.35, "medium"),
    "music_hard_skip": ("music", -0.70, "high"),
    "music_not_started": ("music", 0.00, "low"),
    "music_error": ("music", 0.00, "low"),
    "autoplay_blocked": ("music", 0.00, "low"),
    "mini_game_accept": ("mini_game", 0.90, "high"),
    "mini_game_later": ("mini_game", 0.20, "medium"),
    "mini_game_decline": ("mini_game", -0.35, "high"),
    "mini_game_ignored": ("mini_game", -0.05, "low"),
}

_REWARD_V2_PREVIEW_COMPONENT_ORDER = (
    "reply",
    "continue",
    "consumption",
    "relative_speed",
    "interrupt",
    "settings",
    "interaction",
)

_REWARD_V2_PREVIEW_REPLY_EVENTS = {"user_reply_fast", "user_reply"}

_REWARD_V4_PREVIEW_COMPONENT_ORDER = (
    "consumption",
    "settings",
    "interaction",
)

_WEAK_NEGATIVE_EVENT_TYPES = {"ignored", "mini_game_ignored"}

_MUSIC_PLAYED_THROUGH_EVENT_TYPE = "music_played_through"

_MUSIC_ACTIONABLE_PLAYED_THROUGH_MIN = 3

_MUSIC_ACTIONABLE_AVERAGE_MIN = 0.50


def summarize_recommendation_feedback(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    samples = _calibration_observation_samples(
        observations,
        now=current,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    events_by_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in feedback_events:
        if not isinstance(event, Mapping):
            continue
        safe = sanitize_recommendation_feedback_event(event)
        key = (
            to_stripped_text(safe.get("lanlan_name")),
            to_stripped_text(safe.get("turn_id")),
        )
        if key[0] and key[1]:
            events_by_turn[key].append(safe)

    feedback_scores: list[float] = []
    source_scores: dict[str, list[float]] = defaultdict(list)
    event_counts: Counter[str] = Counter()
    high_positive = 0
    high_negative = 0
    missing = 0
    positive = 0
    negative = 0
    neutral = 0
    explicit_count = 0
    inferred_count = 0
    censored_count = 0

    for row in samples:
        key = (
            to_stripped_text(row.get("lanlan_name")),
            to_stripped_text(row.get("turn_id")),
        )
        events = list(events_by_turn.get(key, ()))
        selected = _select_quality_feedback_events_for_turn(events)
        weak_only = bool(events) and not selected and _has_weak_negative_event(events)
        censored = not selected and (
            weak_only or _reply_window_elapsed(row, now=current)
        )
        if not selected:
            missing += 1
            censored_count += int(censored)
            inferred_count += int(weak_only)
            continue
        explicit_count += 1
        score = clamp_to_range(
            sum(
                float(quality_feedback_score(event.get("event_type")) or 0.0)
                for event in selected
            ),
            -1.0,
            1.0,
        )
        feedback_scores.append(score)
        source_type = normalize_feedback_source_identifier(
            row.get("actual_primary_channel") or row.get("shadow_selected_source_type")
        )
        source_scores[source_type].append(score)
        if score > 0:
            positive += 1
        elif score < 0:
            negative += 1
        else:
            neutral += 1
        for event in selected:
            event_counts[str(event.get("event_type") or "unknown")] += 1
            confidence = str(event.get("confidence") or "")
            event_score = float(
                quality_feedback_score(event.get("event_type")) or 0.0
            )
            if confidence == "high" and event_score > 0:
                high_positive += 1
            if confidence == "high" and event_score < 0:
                high_negative += 1

    count = len(feedback_scores)
    return {
        "feedback_sample_count": count,
        "feedback_joined_count": explicit_count,
        "feedback_inferred_count": inferred_count,
        "feedback_scored_count": count,
        "quality_feedback_scored_count": count,
        "feedback_censored_count": censored_count,
        "average_turn_feedback_score": round(sum(feedback_scores) / count, 3)
        if count
        else None,
        "positive_rate": rounded_ratio_or_none(positive, count),
        "negative_rate": rounded_ratio_or_none(negative, count),
        "neutral_rate": rounded_ratio_or_none(neutral, count),
        "score_by_source_type": {
            source: round(sum(values) / len(values), 3)
            for source, values in sorted(source_scores.items())
            if values
        },
        "event_type_distribution": dict(sorted(event_counts.items())),
        "high_confidence_positive_count": high_positive,
        "high_confidence_negative_count": high_negative,
        "feedback_missing_count": missing,
        "feedback_score_population": "explicit_only",
        "sample_count": len(samples),
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "score_version": QUALITY_FEEDBACK_SCORE_VERSION,
    }


def join_observations_with_feedback(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> list[dict[str, Any]]:
    """Join recent recommendation observations with compact feedback scores."""
    current = time.time() if now is None else float(now)
    samples = _calibration_observation_samples(
        observations,
        now=current,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    events_by_turn = _feedback_events_by_turn(feedback_events)
    joined: list[dict[str, Any]] = []
    for row in samples:
        key = (
            to_stripped_text(row.get("lanlan_name")),
            to_stripped_text(row.get("turn_id")),
        )
        events = list(events_by_turn.get(key, ()))
        selected = _select_quality_feedback_events_for_turn(events)
        feedback_inferred = (
            bool(events) and not selected and _has_weak_negative_event(events)
        )
        feedback_censored = not selected and (
            feedback_inferred or _reply_window_elapsed(row, now=current)
        )
        feedback_missing = not selected
        turn_feedback_score = None
        if selected:
            turn_feedback_score = round(
                clamp_to_range(
                    sum(
                        float(
                            quality_feedback_score(event.get("event_type")) or 0.0
                        )
                        for event in selected
                    ),
                    -1.0,
                    1.0,
                ),
                3,
            )
        top1_source_type = _top1_source_type(row)
        shadow_score = _shadow_selected_score(row)
        joined.append(
            {
                "turn_id": key[1],
                "lanlan_name": key[0],
                "source_type": top1_source_type,
                "shadow_selected_score": shadow_score,
                "top1_source_type": top1_source_type,
                "actual_primary_channel": normalize_feedback_source_identifier(
                    row.get("actual_primary_channel")
                ),
                "matched_actual_source": row.get("matched_actual_source") is True,
                "matched_actual_material": row.get("matched_actual_material") is True,
                "turn_feedback_score": turn_feedback_score,
                "feedback_event_types": [
                    str(event.get("event_type") or "unknown") for event in selected
                ],
                "feedback_missing": feedback_missing,
                "feedback_inferred": feedback_inferred,
                "feedback_censored": feedback_censored,
                "score_bucket": _score_bucket(shadow_score),
            }
        )
    return joined


def join_observations_with_reward_score_v2_preview(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> list[dict[str, Any]]:
    """Join feedback into a point-in-time, attribution-checked v2 preview."""
    current = time.time() if now is None else float(now)
    samples = _calibration_observation_samples(
        observations,
        now=current,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    events_by_turn = _feedback_events_by_turn(feedback_events)
    relative_speed_by_turn = _relative_reply_speed_previews(
        samples,
        events_by_turn,
    )
    joined: list[dict[str, Any]] = []
    for row in samples:
        key = (
            to_stripped_text(row.get("lanlan_name")),
            to_stripped_text(row.get("turn_id")),
        )
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = coerce_float_or_default(row.get("ts"), default=-1.0)
            if ts >= 0 and current - ts >= REPLY_WINDOW_SECONDS:
                events = [
                    build_feedback_event(
                        lanlan_name=key[0],
                        turn_id=key[1],
                        event_type="ignored",
                        source_type=(
                            row.get("actual_primary_channel")
                            or row.get("shadow_selected_source_type")
                        ),
                        ts=current,
                    )
                ]
                feedback_inferred = True

        preview = build_reward_score_v2_preview(
            events,
            feedback_inferred=feedback_inferred,
            relative_speed_preview=relative_speed_by_turn.get(key),
        )
        attribution_issue = (
            _reward_v2_preview_attribution_issue(row, events) if events else None
        )
        attribution_valid = None if not events else attribution_issue is None
        reward_score = preview.get("reward_score_v2_preview")
        if attribution_valid is not True:
            reward_score = None
        expected_candidate_id = (
            to_stripped_text(row.get("shadow_selected_candidate_id")) or None
            if row.get("matched_actual_material") is True
            else None
        )
        joined.append(
            {
                "turn_id": key[1],
                "lanlan_name": key[0],
                "source_type": normalize_feedback_source_identifier(
                    row.get("actual_primary_channel")
                    or row.get("shadow_selected_source_type")
                ),
                "candidate_id": expected_candidate_id,
                "reward_score_v2_preview": reward_score,
                "reward_components_v2_preview": dict(preview["components"]),
                "feedback_event_types": list(preview["event_types"]),
                "feedback_missing": not events,
                "feedback_inferred": feedback_inferred,
                "attribution_valid": attribution_valid,
                "attribution_issue": attribution_issue,
                "relative_speed_status": preview["relative_speed_status"],
                "relative_speed_baseline_sample_count": preview[
                    "relative_speed_baseline_sample_count"
                ],
                "technical_zero_event_types": list(
                    preview["technical_zero_event_types"]
                ),
                "unknown_event_types": list(preview["unknown_event_types"]),
            }
        )
    return joined


def summarize_reward_score_v2_preview(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """Summarize v2 preview without mutating ranking, tuning, or profiles."""
    joined = join_observations_with_reward_score_v2_preview(
        observations,
        feedback_events,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    scored = [
        row
        for row in joined
        if row.get("attribution_valid") is True
        and isinstance(row.get("reward_score_v2_preview"), (int, float))
    ]
    explicit_scored = [
        row for row in scored if row.get("feedback_inferred") is not True
    ]
    inferred_scored = [row for row in scored if row.get("feedback_inferred") is True]
    rewards = [float(row["reward_score_v2_preview"]) for row in explicit_scored]
    inferred_rewards = [
        float(row["reward_score_v2_preview"]) for row in inferred_scored
    ]
    all_rewards = [float(row["reward_score_v2_preview"]) for row in scored]
    source_rewards: dict[str, list[float]] = defaultdict(list)
    component_values: dict[str, list[float]] = defaultdict(list)
    for row in explicit_scored:
        source_rewards[
            normalize_feedback_source_identifier(row.get("source_type"))
        ].append(float(row["reward_score_v2_preview"]))
        components = row.get("reward_components_v2_preview")
        if isinstance(components, Mapping):
            for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER:
                component_values[component].append(
                    coerce_float_or_default(components.get(component), default=0.0)
                )

    attribution_issues = Counter(
        str(row.get("attribution_issue"))
        for row in joined
        if row.get("attribution_issue")
    )
    positive_count = sum(1 for reward in rewards if reward > 0)
    negative_count = sum(1 for reward in rewards if reward < 0)
    neutral_count = sum(1 for reward in rewards if reward == 0)
    return {
        "version": REWARD_SCORE_V2_PREVIEW_VERSION,
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "personalization_state_consumed": False,
        "sample_count": len(joined),
        "reward_scored_count": len(scored),
        "explicit_reward_scored_count": len(explicit_scored),
        "inferred_reward_scored_count": len(inferred_scored),
        "feedback_joined_count": len(explicit_scored),
        "feedback_inferred_count": len(inferred_scored),
        "feedback_missing_count": sum(
            1 for row in joined if row.get("feedback_missing") is True
        ),
        "attribution_issue_count": sum(attribution_issues.values()),
        "attribution_issue_distribution": dict(sorted(attribution_issues.items())),
        "average_reward_score_v2_preview": rounded_mean_or_none(rewards),
        "average_all_reward_score_v2_preview": rounded_mean_or_none(all_rewards),
        "average_inferred_reward_score_v2_preview": rounded_mean_or_none(
            inferred_rewards
        ),
        "positive_rate": rounded_ratio_or_none(positive_count, len(rewards)),
        "negative_rate": rounded_ratio_or_none(negative_count, len(rewards)),
        "neutral_rate": rounded_ratio_or_none(neutral_count, len(rewards)),
        "score_by_source_type": {
            source: rounded_mean_or_none(values)
            for source, values in sorted(source_rewards.items())
            if values
        },
        "average_components": {
            component: rounded_mean_or_none(component_values.get(component, []))
            for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER
        },
        "relative_speed_neutral_count": sum(
            1
            for row in explicit_scored
            if row.get("relative_speed_status")
            in {
                "pending_personal_baseline",
                "insufficient_personal_baseline",
                "baseline_ready_no_bonus",
                "missing_reply_latency",
            }
        ),
        "relative_speed_bonus_count": sum(
            1
            for row in explicit_scored
            if coerce_float_or_default(
                (row.get("reward_components_v2_preview") or {}).get("relative_speed")
                if isinstance(row.get("reward_components_v2_preview"), Mapping)
                else None,
                default=0.0,
            )
            > 0
        ),
        "personal_reply_speed_baseline_ready_count": sum(
            1
            for row in explicit_scored
            if row.get("relative_speed_status")
            in {"baseline_ready_bonus", "baseline_ready_no_bonus"}
        ),
        "technical_zero_event_count": sum(
            len(row.get("technical_zero_event_types") or []) for row in explicit_scored
        ),
        "unknown_event_count": sum(
            len(row.get("unknown_event_types") or []) for row in explicit_scored
        ),
        "score_population": "valid_turn_id_joined_explicit_only",
        "inferred_ignored_reported_separately": True,
        "window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
    }


def join_observations_with_reward_score_v4_preview(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> list[dict[str, Any]]:
    """Derive source-only v4 reward from the existing attributed join."""
    joined_v2 = join_observations_with_reward_score_v2_preview(
        observations,
        feedback_events,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    joined: list[dict[str, Any]] = []
    for row in joined_v2:
        events = [
            build_feedback_event(
                lanlan_name=row.get("lanlan_name"),
                turn_id=row.get("turn_id"),
                event_type=event_type,
                source_type=row.get("source_type"),
                candidate_id=row.get("candidate_id"),
            )
            for event_type in row.get("feedback_event_types") or ()
        ]
        preview = build_reward_score_v4_preview(events)
        reward = preview.get("reward_score_v4_preview")
        if row.get("attribution_valid") is not True:
            reward = None
        excluded = list(preview.get("excluded_event_types") or ())
        joined.append(
            {
                **row,
                "reward_score_v4_preview": reward,
                "reward_components_v4_preview": dict(preview["components"]),
                "feedback_missing": reward is None and not excluded,
                "feedback_excluded": bool(excluded) and reward is None,
                "excluded_event_types_v4_preview": excluded,
            }
        )
    return joined


def summarize_reward_score_v4_preview(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """Summarize the source-only reward contract consumed by Bandit learning."""
    joined = join_observations_with_reward_score_v4_preview(
        observations,
        feedback_events,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    scored = [
        row
        for row in joined
        if row.get("attribution_valid") is True
        and isinstance(row.get("reward_score_v4_preview"), (int, float))
        and row.get("feedback_inferred") is not True
    ]
    rewards = [float(row["reward_score_v4_preview"]) for row in scored]
    source_rewards: dict[str, list[float]] = defaultdict(list)
    component_values: dict[str, list[float]] = defaultdict(list)
    for row in scored:
        source_rewards[
            normalize_feedback_source_identifier(row.get("source_type"))
        ].append(float(row["reward_score_v4_preview"]))
        components = row.get("reward_components_v4_preview")
        if isinstance(components, Mapping):
            for component in _REWARD_V4_PREVIEW_COMPONENT_ORDER:
                component_values[component].append(
                    coerce_float_or_default(components.get(component), default=0.0)
                )
    positive_count = sum(1 for reward in rewards if reward > 0)
    negative_count = sum(1 for reward in rewards if reward < 0)
    neutral_count = sum(1 for reward in rewards if reward == 0)
    return {
        "version": REWARD_SCORE_V4_PREVIEW_VERSION,
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "bandit_consumed": True,
        "sample_count": len(joined),
        "reward_scored_count": len(scored),
        "source_reward_scored_count": len(scored),
        "feedback_excluded_count": sum(
            1 for row in joined if row.get("feedback_excluded") is True
        ),
        "feedback_missing_count": sum(
            1 for row in joined if row.get("feedback_missing") is True
        ),
        "feedback_score_population": "source_attributed_only",
        "average_reward_score_v4_preview": rounded_mean_or_none(rewards),
        "positive_rate": rounded_ratio_or_none(positive_count, len(rewards)),
        "negative_rate": rounded_ratio_or_none(negative_count, len(rewards)),
        "neutral_rate": rounded_ratio_or_none(neutral_count, len(rewards)),
        "score_by_source_type": {
            source: rounded_mean_or_none(values)
            for source, values in sorted(source_rewards.items())
            if values
        },
        "average_components": {
            component: rounded_mean_or_none(component_values.get(component, []))
            for component in _REWARD_V4_PREVIEW_COMPONENT_ORDER
        },
        "excluded_event_count": sum(
            len(row.get("excluded_event_types_v4_preview") or ()) for row in joined
        ),
        "window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
    }


def summarize_feedback_calibration(
    observations: Iterable[Mapping[str, Any]],
    feedback_events: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = 3600,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """Report whether recommendation scores align with later user feedback."""
    joined = join_observations_with_feedback(
        observations,
        feedback_events,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    scored = [
        row
        for row in joined
        if row.get("feedback_missing") is not True
        and isinstance(row.get("turn_feedback_score"), (int, float))
    ]
    feedback_joined_count = len(scored)
    feedback_inferred_count = sum(
        1 for row in joined if row.get("feedback_inferred") is True
    )
    feedback_scored_count = len(scored)
    feedback_censored_count = sum(
        1 for row in joined if row.get("feedback_censored") is True
    )
    feedback_scores = [float(row["turn_feedback_score"]) for row in scored]
    positive_count = sum(1 for score in feedback_scores if score > 0)
    negative_count = sum(1 for score in feedback_scores if score < 0)

    source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    high_score_source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mid_low_source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    top1_counts: Counter[str] = Counter()
    for row in joined:
        source = normalize_feedback_source_identifier(row.get("source_type"))
        if source:
            top1_counts[source] += 1
        if row.get("feedback_missing") is True or not isinstance(
            row.get("turn_feedback_score"), (int, float)
        ):
            continue
        source_rows[source].append(row)
        bucket = row.get("score_bucket")
        if bucket == "high":
            high_score_source_rows[source].append(row)
        elif bucket in {"mid", "low"}:
            mid_low_source_rows[source].append(row)

    score_by_source_type = {
        source: _average_joined_feedback(rows)
        for source, rows in sorted(source_rows.items())
        if rows
    }
    bucket_feedback = _score_bucket_feedback(scored)

    over_scored_sources = sorted(
        source
        for source, rows in high_score_source_rows.items()
        if rows and _average_joined_feedback(rows) < 0
    )
    under_scored_sources = sorted(
        source
        for source, rows in mid_low_source_rows.items()
        if rows and _average_joined_feedback(rows) >= 0.25
    )

    dominant_low_feedback_sources = _dominant_low_feedback_sources(
        joined,
        score_by_source_type,
        top1_counts,
    )
    feedback_signal_summary = _feedback_signal_summary(scored)
    source_feedback_pressure = _source_feedback_pressure(feedback_signal_summary)
    suggested_weight_adjustments = _suggest_feedback_weight_adjustments(
        over_scored_sources=over_scored_sources,
        under_scored_sources=under_scored_sources,
        dominant_low_feedback_sources=dominant_low_feedback_sources,
        source_feedback_pressure=source_feedback_pressure,
    )
    feedback_actionable_suggestions = _feedback_actionable_suggestions(
        score_by_source_type=score_by_source_type,
        signal_summary=feedback_signal_summary,
        source_feedback_pressure=source_feedback_pressure,
    )
    active_ready_reasons = _feedback_active_ready_reasons(
        feedback_joined_count=feedback_joined_count,
        average_feedback_score=rounded_mean_or_none(feedback_scores),
        top1_positive_rate=rounded_ratio_or_none(positive_count, feedback_scored_count),
        top1_negative_rate=rounded_ratio_or_none(negative_count, feedback_scored_count),
        bucket_feedback=bucket_feedback,
        dominant_low_feedback_sources=dominant_low_feedback_sources,
    )

    return {
        "sample_count": len(joined),
        "feedback_joined_count": feedback_joined_count,
        "feedback_inferred_count": feedback_inferred_count,
        "feedback_scored_count": feedback_scored_count,
        "quality_feedback_scored_count": feedback_scored_count,
        "feedback_censored_count": feedback_censored_count,
        "feedback_missing_count": len(joined) - feedback_scored_count,
        "average_feedback_score": rounded_mean_or_none(feedback_scores),
        "top1_positive_rate": rounded_ratio_or_none(
            positive_count, feedback_scored_count
        ),
        "top1_negative_rate": rounded_ratio_or_none(
            negative_count, feedback_scored_count
        ),
        "feedback_score_population": "explicit_only",
        "feedback_rate_denominator": "quality_feedback_scored_count",
        "score_by_source_type": score_by_source_type,
        "score_bucket_feedback": bucket_feedback,
        "over_scored_sources": over_scored_sources,
        "under_scored_sources": under_scored_sources,
        "suggested_weight_adjustments": suggested_weight_adjustments,
        "feedback_signal_summary": feedback_signal_summary,
        "source_feedback_pressure": source_feedback_pressure,
        "feedback_actionable_suggestions": feedback_actionable_suggestions,
        "manual_tuning_preview": _manual_tuning_preview(
            feedback_actionable_suggestions
        ),
        "active_ready_by_feedback": not active_ready_reasons,
        "active_ready_reasons": active_ready_reasons,
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "score_version": QUALITY_FEEDBACK_SCORE_VERSION,
    }


def _feedback_events_by_turn(
    feedback_events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    events_by_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in feedback_events:
        if not isinstance(event, Mapping):
            continue
        safe = sanitize_recommendation_feedback_event(event)
        key = (
            to_stripped_text(safe.get("lanlan_name")),
            to_stripped_text(safe.get("turn_id")),
        )
        if key[0] and key[1]:
            events_by_turn[key].append(safe)
    return events_by_turn


def _relative_reply_speed_previews(
    observations: Sequence[Mapping[str, Any]],
    events_by_turn: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build per-turn speed previews from earlier, valid replies only."""
    records: list[tuple[tuple[str, str], float, float | None]] = []
    for observation in observations:
        key = (
            to_stripped_text(observation.get("lanlan_name")),
            to_stripped_text(observation.get("turn_id")),
        )
        events = list(events_by_turn.get(key, ()))
        if not key[0] or not key[1] or not events:
            continue
        if _reward_v2_preview_attribution_issue(observation, events) is not None:
            continue
        replies = [
            event
            for event in events
            if to_stripped_text(event.get("event_type"))
            in _REWARD_V2_PREVIEW_REPLY_EVENTS
        ]
        if not replies:
            continue
        reply = min(
            replies,
            key=lambda event: coerce_float_or_default(
                event.get("ts"), default=float("inf")
            ),
        )
        event_ts = coerce_float_or_default(
            reply.get("ts"),
            default=coerce_float_or_default(
                observation.get("ts"), default=float("inf")
            ),
        )
        latency = _reply_latency_seconds(reply)
        records.append((key, event_ts, latency))

    valid_history = [record for record in records if record[2] is not None]
    previews: dict[tuple[str, str], dict[str, Any]] = {}
    for key, event_ts, latency in records:
        prior_latencies = [
            float(previous_latency)
            for _, previous_ts, previous_latency in valid_history
            if previous_ts < event_ts and previous_latency is not None
        ]
        previews[key] = _relative_reply_speed_preview(
            latency,
            prior_latencies,
        )
    return previews


def _reply_latency_seconds(event: Mapping[str, Any]) -> float | None:
    metadata = event.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    latency = coerce_float_or_default(
        metadata.get("reply_latency_seconds"), default=float("nan")
    )
    if not math.isfinite(latency) or latency < 0 or latency > REPLY_WINDOW_SECONDS:
        return None
    return latency


def _relative_reply_speed_preview(
    latency: float | None,
    prior_latencies: Sequence[float],
) -> dict[str, Any]:
    sample_count = len(prior_latencies)
    if latency is None:
        return {
            "status": "missing_reply_latency",
            "baseline_sample_count": sample_count,
            "bonus": 0.0,
        }
    if sample_count < REPLY_SPEED_BASELINE_MIN_SAMPLES:
        return {
            "status": "insufficient_personal_baseline",
            "baseline_sample_count": sample_count,
            "bonus": 0.0,
        }

    logged = [math.log1p(value) for value in prior_latencies]
    center = float(median(logged))
    mad = float(median(abs(value - center) for value in logged))
    scale = max(1.4826 * mad, REPLY_SPEED_LOG_SCALE_FLOOR)
    faster_z = max(0.0, (center - math.log1p(latency)) / scale)
    bonus = min(REPLY_SPEED_BONUS_MAX, faster_z * 0.02)
    return {
        "status": ("baseline_ready_bonus" if bonus > 0 else "baseline_ready_no_bonus"),
        "baseline_sample_count": sample_count,
        "bonus": round(bonus, 3),
    }


def _top1_source_type(row: Mapping[str, Any]) -> str:
    candidates = row.get("top_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                source = normalize_feedback_source_identifier(
                    candidate.get("source_type")
                )
                if source:
                    return source
    return normalize_feedback_source_identifier(row.get("shadow_selected_source_type"))


def _shadow_selected_score(row: Mapping[str, Any]) -> float | None:
    score = coerce_float_or_default(
        row.get("shadow_selected_score"), default=float("nan")
    )
    if score == score:
        return round(score, 3)
    candidates = row.get("top_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                score = coerce_float_or_default(
                    candidate.get("score"), default=float("nan")
                )
                if score == score:
                    return round(score, 3)
    return None


def _score_bucket(score: Any) -> str | None:
    value = coerce_float_or_default(score, default=float("nan"))
    if value != value:
        return None
    if value >= 0.75:
        return "high"
    if value >= 0.50:
        return "mid"
    return "low"


def _average_joined_feedback(rows: Sequence[Mapping[str, Any]]) -> float:
    scores = [
        float(row["turn_feedback_score"])
        for row in rows
        if isinstance(row.get("turn_feedback_score"), (int, float))
    ]
    average = rounded_mean_or_none(scores)
    return 0.0 if average is None else average


def _score_bucket_feedback(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for bucket in ("high", "mid", "low"):
        bucket_rows = [row for row in rows if row.get("score_bucket") == bucket]
        scores = [
            float(row["turn_feedback_score"])
            for row in bucket_rows
            if isinstance(row.get("turn_feedback_score"), (int, float))
        ]
        positive = sum(1 for score in scores if score > 0)
        negative = sum(1 for score in scores if score < 0)
        result[bucket] = {
            "count": len(scores),
            "average_feedback_score": rounded_mean_or_none(scores),
            "positive_rate": rounded_ratio_or_none(positive, len(scores)),
            "negative_rate": rounded_ratio_or_none(negative, len(scores)),
        }
    return result


def _dominant_low_feedback_sources(
    joined: Sequence[Mapping[str, Any]],
    score_by_source_type: Mapping[str, float],
    top1_counts: Counter[str],
) -> list[str]:
    total = len(joined)
    if total <= 0:
        return []
    return sorted(
        source
        for source, count in top1_counts.items()
        if rounded_ratio_or_none(count, total) is not None
        and float(rounded_ratio_or_none(count, total) or 0.0) >= 0.60
        and float(score_by_source_type.get(source, 0.0)) < 0.10
    )


def _suggest_feedback_weight_adjustments(
    *,
    over_scored_sources: Sequence[str],
    under_scored_sources: Sequence[str],
    dominant_low_feedback_sources: Sequence[str],
    source_feedback_pressure: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    suggestions: dict[str, dict[str, Any]] = {}
    pressure = source_feedback_pressure or {}

    def add(source: str, adjustment: float, reason: str) -> None:
        entry = suggestions.setdefault(
            source,
            {"adjustment": adjustment, "reasons": []},
        )
        if abs(adjustment) > abs(float(entry.get("adjustment", 0.0))):
            entry["adjustment"] = adjustment
        reasons = entry.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)

    for source in over_scored_sources:
        if _is_weak_ignored_only_pressure(source, pressure):
            continue
        add(source, -0.05, "over_scored_high_score_low_feedback")
    for source in under_scored_sources:
        add(source, 0.03, "under_scored_positive_feedback")
    for source in dominant_low_feedback_sources:
        if _is_weak_ignored_only_pressure(source, pressure):
            continue
        add(source, -0.05, "dominant_low_feedback_source")
    return {
        source: {
            "adjustment": round(float(entry["adjustment"]), 3),
            "reasons": list(entry["reasons"]),
        }
        for source, entry in sorted(suggestions.items())
    }


def _is_weak_ignored_only_pressure(
    source: str,
    source_feedback_pressure: Mapping[str, Mapping[str, Any]],
) -> bool:
    pressure = source_feedback_pressure.get(source)
    if not isinstance(pressure, Mapping):
        return False
    return (
        pressure.get("level") == "weak_ignored_pressure"
        and int(pressure.get("weak_negative_count") or 0) > 0
        and int(pressure.get("high_confidence_negative_count") or 0) == 0
        and int(pressure.get("strong_positive_count") or 0) == 0
    )


def _feedback_signal_summary(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "feedback_count": 0,
            "strong_positive_count": 0,
            "weak_negative_count": 0,
            "ignored_count": 0,
            "played_through_count": 0,
            "high_confidence_negative_count": 0,
        }
    )
    for row in rows:
        source = normalize_feedback_source_identifier(row.get("source_type"))
        if not source:
            continue
        bucket = stats[source]
        bucket["feedback_count"] += 1
        event_types = row.get("feedback_event_types")
        if not isinstance(event_types, Sequence) or isinstance(
            event_types, (str, bytes)
        ):
            continue
        for raw_event_type in event_types:
            event_type = str(raw_event_type or "unknown")
            _, score, confidence = _FEEDBACK_EVENT_SCORES.get(
                event_type,
                ("unknown", 0.0, "low"),
            )
            if confidence == "high" and score > 0:
                bucket["strong_positive_count"] += 1
            if confidence == "high" and score < 0:
                bucket["high_confidence_negative_count"] += 1
            if event_type in _WEAK_NEGATIVE_EVENT_TYPES:
                bucket["weak_negative_count"] += 1
            if event_type == "ignored":
                bucket["ignored_count"] += 1
            if event_type == _MUSIC_PLAYED_THROUGH_EVENT_TYPE:
                bucket["played_through_count"] += 1

    result: dict[str, dict[str, Any]] = {}
    for source, bucket in sorted(stats.items()):
        denominator = (
            int(bucket["strong_positive_count"])
            + int(bucket["high_confidence_negative_count"])
            + int(bucket["weak_negative_count"])
        )
        result[source] = {
            "feedback_count": int(bucket["feedback_count"]),
            "strong_positive_count": int(bucket["strong_positive_count"]),
            "weak_negative_count": int(bucket["weak_negative_count"]),
            "ignored_count": int(bucket["ignored_count"]),
            "played_through_count": int(bucket["played_through_count"]),
            "high_confidence_negative_count": int(
                bucket["high_confidence_negative_count"]
            ),
            "confidence_positive_rate": rounded_ratio_or_none(
                int(bucket["strong_positive_count"]),
                denominator,
            ),
        }
    return result


def _source_feedback_pressure(
    signal_summary: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    pressure: dict[str, dict[str, Any]] = {}
    for source, stats in sorted(signal_summary.items()):
        ignored_count = int(stats.get("ignored_count") or 0)
        weak_negative_count = int(stats.get("weak_negative_count") or 0)
        high_negative_count = int(stats.get("high_confidence_negative_count") or 0)
        strong_positive_count = int(stats.get("strong_positive_count") or 0)
        if high_negative_count > 0:
            level = "high_confidence_negative_pressure"
        elif weak_negative_count > 0:
            level = "weak_ignored_pressure"
        else:
            level = "none"
        pressure[source] = {
            "level": level,
            "ignored_count": ignored_count,
            "weak_negative_count": weak_negative_count,
            "high_confidence_negative_count": high_negative_count,
            "strong_positive_count": strong_positive_count,
        }
    return pressure


def _feedback_actionable_suggestions(
    *,
    score_by_source_type: Mapping[str, float],
    signal_summary: Mapping[str, Mapping[str, Any]],
    source_feedback_pressure: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    suggestions: dict[str, dict[str, Any]] = {}

    def add(source: str, adjustment: float, reason: str, confidence: str) -> None:
        entry = suggestions.setdefault(
            source,
            {"adjustment": adjustment, "reasons": [], "confidence": confidence},
        )
        if abs(adjustment) > abs(float(entry.get("adjustment", 0.0))):
            entry["adjustment"] = adjustment
            entry["confidence"] = confidence
        reasons = entry.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)

    music_stats = signal_summary.get("music") or {}
    music_average = score_by_source_type.get("music")
    if (
        int(music_stats.get("played_through_count") or 0)
        >= _MUSIC_ACTIONABLE_PLAYED_THROUGH_MIN
        and isinstance(music_average, (int, float))
        and float(music_average) >= _MUSIC_ACTIONABLE_AVERAGE_MIN
    ):
        add("music", 0.03, "strong_music_positive_feedback", "high")

    for source, pressure in sorted(source_feedback_pressure.items()):
        level = str(pressure.get("level") or "")
        average = score_by_source_type.get(source)
        if level == "weak_ignored_pressure":
            add(source, 0.0, "weak_ignored_pressure", "low")
        elif (
            level == "high_confidence_negative_pressure"
            and isinstance(average, (int, float))
            and float(average) < 0
        ):
            add(source, -0.05, "high_confidence_negative_feedback", "high")

    return {
        source: {
            "adjustment": round(float(entry["adjustment"]), 3),
            "reasons": list(entry["reasons"]),
            "confidence": str(entry.get("confidence") or "low"),
        }
        for source, entry in sorted(suggestions.items())
    }


def _manual_tuning_preview(
    feedback_actionable_suggestions: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    preview: dict[str, dict[str, Any]] = {}
    for source, suggestion in sorted(feedback_actionable_suggestions.items()):
        adjustment = round(
            coerce_float_or_default(suggestion.get("adjustment"), default=0.0), 3
        )
        preview[source] = {
            "current_adjustment": 0.0,
            "suggested_delta": adjustment,
            "preview_adjustment": adjustment,
            "reasons": list(suggestion.get("reasons") or []),
            "confidence": str(suggestion.get("confidence") or "low"),
            "write_mode": "manual_review_only",
        }
    return preview


def _feedback_active_ready_reasons(
    *,
    feedback_joined_count: int,
    average_feedback_score: float | None,
    top1_positive_rate: float | None,
    top1_negative_rate: float | None,
    bucket_feedback: Mapping[str, Mapping[str, Any]],
    dominant_low_feedback_sources: Sequence[str],
) -> list[str]:
    reasons: list[str] = []
    if feedback_joined_count < 30:
        reasons.append("feedback_sample_count_below_threshold")
    if average_feedback_score is None or average_feedback_score <= 0:
        reasons.append("average_feedback_score_not_positive")
    high = bucket_feedback.get("high", {}).get("average_feedback_score")
    mid = bucket_feedback.get("mid", {}).get("average_feedback_score")
    low = bucket_feedback.get("low", {}).get("average_feedback_score")
    if not all(isinstance(value, (int, float)) for value in (high, mid, low)):
        reasons.append("score_bucket_feedback_insufficient")
    elif not (float(high) > float(mid) >= float(low)):
        reasons.append("score_bucket_feedback_not_monotonic")
    if top1_positive_rate is None or top1_positive_rate < 0.35:
        reasons.append("top1_positive_rate_below_threshold")
    if top1_negative_rate is None or top1_negative_rate > 0.20:
        reasons.append("top1_negative_rate_above_threshold")
    if dominant_low_feedback_sources:
        reasons.append("dominant_low_feedback_source")
    return reasons


def _select_quality_feedback_events_for_turn(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Select derived v2 quality events while excluding missing/censored signals."""
    by_group: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = sanitize_recommendation_feedback_event(raw)
        score = quality_feedback_score(event.get("event_type"))
        if score is None:
            continue
        group = str(event.get("event_group") or "unknown")
        previous = by_group.get(group)
        previous_score = (
            quality_feedback_score(previous.get("event_type"))
            if previous is not None
            else None
        )
        if previous_score is None or abs(score) > abs(previous_score):
            by_group[group] = event
    return list(by_group.values())


def _has_weak_negative_event(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        to_stripped_text(event.get("event_type")) in _WEAK_NEGATIVE_EVENT_TYPES
        for event in events
        if isinstance(event, Mapping)
    )


def _reply_window_elapsed(row: Mapping[str, Any], *, now: float) -> bool:
    if row.get("delivered") is not True:
        return False
    ts = coerce_float_or_default(row.get("ts"), default=-1.0)
    return ts >= 0 and now - ts >= REPLY_WINDOW_SECONDS


def _select_feedback_events_for_turn(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_group: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = sanitize_recommendation_feedback_event(raw)
        group = str(event.get("event_group") or "unknown")
        previous = by_group.get(group)
        if previous is None or abs(
            coerce_float_or_default(event.get("report_score_v1"), default=0.0)
        ) > abs(coerce_float_or_default(previous.get("report_score_v1"), default=0.0)):
            by_group[group] = event
    return list(by_group.values())


def _reward_v2_preview_attribution_issue(
    observation: Mapping[str, Any],
    feedback_events: Sequence[Mapping[str, Any]],
) -> str | None:
    """Validate that feedback belongs to the material actually delivered."""
    if observation.get("delivered") is not True:
        return "observation_not_delivered"
    expected_source = normalize_feedback_source_identifier(
        observation.get("actual_primary_channel")
        or observation.get("shadow_selected_source_type")
    )
    expected_candidate_id = (
        to_stripped_text(observation.get("shadow_selected_candidate_id")) or None
        if observation.get("matched_actual_material") is True
        else None
    )
    for raw_event in feedback_events:
        event = sanitize_recommendation_feedback_event(raw_event)
        event_source = normalize_feedback_source_identifier(event.get("source_type"))
        if (
            event_source != "unknown"
            and expected_source != "unknown"
            and event_source != expected_source
        ):
            return "source_mismatch"
        event_candidate_id = to_stripped_text(event.get("candidate_id")) or None
        if event_candidate_id is None:
            continue
        if expected_candidate_id is None:
            return "candidate_unverifiable"
        if event_candidate_id != expected_candidate_id:
            return "candidate_mismatch"
    return None


def _calibration_observation_samples(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float,
    window_seconds: int,
    sample_limit: int,
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in observations if isinstance(row, Mapping)]
    recent = [
        row
        for row in rows
        if 0
        <= now - coerce_float_or_default(row.get("ts"), default=-1.0)
        <= max(0, int(window_seconds))
    ]
    limit = max(0, int(sample_limit))
    return recent[-limit:] if limit else []


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


def availability_exposure_id(lanlan_name: Any, turn_id: Any) -> str:
    """Return a non-reversible key for one short-lived pending exposure."""
    raw = f"{str(lanlan_name or '').strip()}\0{str(turn_id or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def register_availability_exposure(
    *,
    config_dir: str | os.PathLike[str] | None,
    exposure_id: str,
    activity_state: Any,
    input_mode: Any,
    delivered_at: float,
    mode: str | None = None,
) -> None:
    """Persist only the bounded metadata needed to survive a service restart."""
    effective_mode = (
        PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE if mode is None else mode
    )
    if effective_mode != "shadow" or config_dir is None or not exposure_id:
        return
    delivered = float(delivered_at)

    def update(state: dict[str, Any]) -> dict[str, Any]:
        _flush_due_pending_in_state(state, now=delivered)
        state["pending_exposures"][exposure_id] = {
            "activity_state": _normalize_activity_state(activity_state),
            "input_mode": _normalize_input_mode(input_mode),
            "delivered_at": delivered,
        }
        return state

    try:
        _availability_store(Path(config_dir)).update(update)
    except Exception:
        logger.debug("availability pending exposure update failed", exc_info=True)


def flush_persisted_censored_availability(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
    mode: str | None = None,
) -> int:
    """Finalize expired pending exposures, including after a process restart."""
    effective_mode = (
        PROACTIVE_RECOMMENDATION_AVAILABILITY_MODE if mode is None else mode
    )
    if effective_mode != "shadow" or config_dir is None:
        return 0
    current = time.time() if now is None else float(now)
    finalized = 0

    def update(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal finalized
        finalized = _flush_due_pending_in_state(state, now=current)
        return state

    try:
        _availability_store(Path(config_dir)).update(update)
    except Exception:
        logger.debug("availability pending exposure flush failed", exc_info=True)
        return 0
    return finalized


def record_availability_outcome(
    *,
    config_dir: str | os.PathLike[str] | None,
    activity_state: Any,
    input_mode: Any,
    delivered_at: float,
    replied_at: float | None = None,
    censored: bool = False,
    exposure_id: str | None = None,
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
    def update(state: dict[str, Any]) -> dict[str, Any]:
        pending = None
        if exposure_id:
            pending = state["pending_exposures"].pop(exposure_id, None)
            if pending is None:
                return state
        effective_activity = (
            pending.get("activity_state") if pending is not None else activity
        )
        effective_input = (
            pending.get("input_mode") if pending is not None else normalized_input
        )
        if effective_input == "unknown" and normalized_input != "unknown":
            effective_input = normalized_input
        effective_delivered = (
            float(pending.get("delivered_at"))
            if pending is not None
            else float(delivered_at)
        )
        return _apply_outcome_to_state(
            state,
            activity_state=effective_activity,
            input_mode=effective_input,
            delivered_at=effective_delivered,
            outcome_at=outcome_at,
            replied_at=float(replied_at) if replied_at is not None else None,
        )

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


def _apply_outcome_to_state(
    state: dict[str, Any],
    *,
    activity_state: Any,
    input_mode: Any,
    delivered_at: float,
    outcome_at: float,
    replied_at: float | None,
) -> dict[str, Any]:
    activity = _normalize_activity_state(activity_state)
    normalized_input = _normalize_input_mode(input_mode)
    time_bucket = availability_time_bucket(delivered_at)
    bucket_key = _bucket_key(activity, normalized_input, time_bucket)
    bucket = _decayed_bucket(
        state["buckets"].get(bucket_key),
        activity_state=activity,
        input_mode=normalized_input,
        time_bucket=time_bucket,
        now=outcome_at,
    )
    bucket["exposure_count"] += 1
    bucket["exposure_weight"] += 1.0
    if replied_at is not None:
        latency = min(
            AVAILABILITY_REPLY_WINDOW_SECONDS,
            max(0.0, replied_at - delivered_at),
        )
        bucket["reply_count"] += 1
        bucket["reply_weight"] += 1.0
        bucket["reply_latency_weighted_seconds"] += latency
    else:
        bucket["censored_count"] += 1
        bucket["censored_weight"] += 1.0
    bucket["updated_at"] = outcome_at
    state["buckets"][bucket_key] = bucket
    state["updated_at"] = max(float(state.get("updated_at") or 0.0), outcome_at)
    return state


def _flush_due_pending_in_state(state: dict[str, Any], *, now: float) -> int:
    finalized = 0
    pending_exposures = state["pending_exposures"]
    for exposure_id, pending in list(pending_exposures.items()):
        delivered_at = float(pending.get("delivered_at") or 0.0)
        if now - delivered_at <= AVAILABILITY_REPLY_WINDOW_SECONDS:
            continue
        pending_exposures.pop(exposure_id, None)
        _apply_outcome_to_state(
            state,
            activity_state=pending.get("activity_state"),
            input_mode=pending.get("input_mode"),
            delivered_at=delivered_at,
            outcome_at=delivered_at + AVAILABILITY_REPLY_WINDOW_SECONDS,
            replied_at=None,
        )
        finalized += 1
    return finalized


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
        fallback_trace.append(
            {
                "level": level,
                **(
                    _public_bucket(bucket)
                    or {
                        "exposure_count": 0,
                        "reply_count": 0,
                        "censored_count": 0,
                        "response_rate": None,
                        "average_reply_latency_seconds": None,
                    }
                ),
                "ready": ready,
            }
        )
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
        "stored_fields": "aggregate_plus_ephemeral_pending_no_conversation_text",
        "storage_error": storage_error,
    }


def _public_bucket(bucket: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not bucket:
        return None
    exposures = float(bucket.get("exposure_weight") or 0.0)
    replies = float(bucket.get("reply_weight") or 0.0)
    return {
        "exposure_count": int(bucket.get("exposure_count") or 0),
        "reply_count": int(bucket.get("reply_count") or 0),
        "censored_count": int(bucket.get("censored_count") or 0),
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
        and int(bucket.get("exposure_count") or 0) >= AVAILABILITY_MIN_EXPOSURES
        and int(bucket.get("reply_count") or 0) >= AVAILABILITY_MIN_REPLIES
    )


def _combine_buckets(buckets: Any) -> dict[str, float] | None:
    combined = {
        "exposure_count": 0,
        "reply_count": 0,
        "censored_count": 0,
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
        "exposure_count": _raw_count(
            bucket, "exposure_count", fallback_weight="exposure_weight"
        ),
        "reply_count": _raw_count(
            bucket, "reply_count", fallback_weight="reply_weight"
        ),
        "censored_count": _raw_count(
            bucket, "censored_count", fallback_weight="censored_weight"
        ),
        "exposure_weight": max(
            0.0,
            coerce_float_or_default(bucket.get("exposure_weight"), default=0.0),
        )
        * factor,
        "reply_weight": max(
            0.0,
            coerce_float_or_default(bucket.get("reply_weight"), default=0.0),
        )
        * factor,
        "censored_weight": max(
            0.0,
            coerce_float_or_default(bucket.get("censored_weight"), default=0.0),
        )
        * factor,
        "reply_latency_weighted_seconds": max(
            0.0,
            coerce_float_or_default(
                bucket.get("reply_latency_weighted_seconds"), default=0.0
            ),
        )
        * factor,
        "updated_at": now,
    }


def _raw_count(
    bucket: Mapping[str, Any], key: str, *, fallback_weight: str
) -> int:
    if key in bucket:
        return max(0, int(coerce_float_or_default(bucket.get(key), default=0.0)))
    return max(
        0,
        int(math.ceil(coerce_float_or_default(bucket.get(fallback_weight), default=0.0))),
    )


def _sanitize_state(raw: Any) -> dict[str, Any]:
    source = (
        raw
        if isinstance(raw, Mapping) and raw.get("schema_version") in {1, 2}
        else {}
    )
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
    pending_exposures: dict[str, dict[str, Any]] = {}
    raw_pending = source.get("pending_exposures")
    if isinstance(raw_pending, Mapping):
        for exposure_id, pending in list(raw_pending.items())[:256]:
            if (
                isinstance(exposure_id, str)
                and len(exposure_id) == 32
                and all(character in "0123456789abcdef" for character in exposure_id)
                and isinstance(pending, Mapping)
            ):
                pending_exposures[exposure_id] = {
                    "activity_state": _normalize_activity_state(
                        pending.get("activity_state")
                    ),
                    "input_mode": _normalize_input_mode(pending.get("input_mode")),
                    "delivered_at": max(
                        0.0,
                        coerce_float_or_default(
                            pending.get("delivered_at"), default=0.0
                        ),
                    ),
                }
    return {
        "schema_version": 2,
        "updated_at": max(0.0, coerce_float_or_default(source.get("updated_at"), default=0.0)),
        "buckets": buckets,
        "pending_exposures": pending_exposures,
    }


def _availability_store(config_dir: Path) -> AtomicJsonStore:
    return AtomicJsonStore(
        config_dir / AVAILABILITY_FILENAME,
        default_factory=lambda: {
            "schema_version": 2,
            "updated_at": 0.0,
            "buckets": {},
            "pending_exposures": {},
        },
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
