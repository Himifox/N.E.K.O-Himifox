"""Pure feedback joins, calibration, and tuning suggestions."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
import math
from statistics import median
import time
from typing import Any

from .events import (
    _clean_text,
    _normalize_source_type,
    _number,
    build_feedback_event,
    sanitize_recommendation_feedback_event,
)
from .rewards import build_reward_score_v2_preview

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
        key = (_clean_text(safe.get("lanlan_name")), _clean_text(safe.get("turn_id")))
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

    for row in samples:
        key = (_clean_text(row.get("lanlan_name")), _clean_text(row.get("turn_id")))
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = _number(row.get("ts"), -1.0)
            if ts >= 0 and current - ts >= REPLY_WINDOW_SECONDS:
                events = [
                    build_feedback_event(
                        lanlan_name=key[0],
                        turn_id=key[1],
                        event_type="ignored",
                        source_type=row.get("actual_primary_channel")
                        or row.get("shadow_selected_source_type"),
                        ts=current,
                    )
                ]
                feedback_inferred = True
        if not events:
            missing += 1
            continue
        if feedback_inferred:
            inferred_count += 1
        else:
            explicit_count += 1
        selected = _select_feedback_events_for_turn(events)
        score = _clamp(
            sum(_number(event.get("report_score_v1"), 0.0) for event in selected),
            -1.0,
            1.0,
        )
        feedback_scores.append(score)
        source_type = _normalize_source_type(
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
            event_score = _number(event.get("report_score_v1"), 0.0)
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
        "average_turn_feedback_score": round(sum(feedback_scores) / count, 3)
        if count
        else None,
        "positive_rate": _rate(positive, count),
        "negative_rate": _rate(negative, count),
        "neutral_rate": _rate(neutral, count),
        "score_by_source_type": {
            source: round(sum(values) / len(values), 3)
            for source, values in sorted(source_scores.items())
            if values
        },
        "event_type_distribution": dict(sorted(event_counts.items())),
        "high_confidence_positive_count": high_positive,
        "high_confidence_negative_count": high_negative,
        "feedback_missing_count": missing,
        "sample_count": len(samples),
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "score_version": FEEDBACK_SCORE_VERSION,
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
        key = (_clean_text(row.get("lanlan_name")), _clean_text(row.get("turn_id")))
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = _number(row.get("ts"), -1.0)
            if ts >= 0 and current - ts >= REPLY_WINDOW_SECONDS:
                events = [
                    build_feedback_event(
                        lanlan_name=key[0],
                        turn_id=key[1],
                        event_type="ignored",
                        source_type=row.get("actual_primary_channel")
                        or row.get("shadow_selected_source_type"),
                        ts=current,
                    )
                ]
                feedback_inferred = True
        selected = _select_feedback_events_for_turn(events) if events else []
        feedback_missing = not selected
        turn_feedback_score = None
        if selected:
            turn_feedback_score = round(
                _clamp(
                    sum(
                        _number(event.get("report_score_v1"), 0.0) for event in selected
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
                "actual_primary_channel": _normalize_source_type(
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
        key = (_clean_text(row.get("lanlan_name")), _clean_text(row.get("turn_id")))
        events = list(events_by_turn.get(key, ()))
        feedback_inferred = False
        if key[0] and key[1] and not events and row.get("delivered") is True:
            ts = _number(row.get("ts"), -1.0)
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
            _clean_text(row.get("shadow_selected_candidate_id")) or None
            if row.get("matched_actual_material") is True
            else None
        )
        joined.append(
            {
                "turn_id": key[1],
                "lanlan_name": key[0],
                "source_type": _normalize_source_type(
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
        source_rewards[_normalize_source_type(row.get("source_type"))].append(
            float(row["reward_score_v2_preview"])
        )
        components = row.get("reward_components_v2_preview")
        if isinstance(components, Mapping):
            for component in _REWARD_V2_PREVIEW_COMPONENT_ORDER:
                component_values[component].append(
                    _number(components.get(component), 0.0)
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
        "average_reward_score_v2_preview": _average(rewards),
        "average_all_reward_score_v2_preview": _average(all_rewards),
        "average_inferred_reward_score_v2_preview": _average(inferred_rewards),
        "positive_rate": _rate(positive_count, len(rewards)),
        "negative_rate": _rate(negative_count, len(rewards)),
        "neutral_rate": _rate(neutral_count, len(rewards)),
        "score_by_source_type": {
            source: _average(values)
            for source, values in sorted(source_rewards.items())
            if values
        },
        "average_components": {
            component: _average(component_values.get(component, []))
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
            if _number(
                (row.get("reward_components_v2_preview") or {}).get("relative_speed")
                if isinstance(row.get("reward_components_v2_preview"), Mapping)
                else None,
                0.0,
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
    feedback_joined_count = sum(
        1 for row in scored if row.get("feedback_inferred") is not True
    )
    feedback_inferred_count = sum(
        1 for row in scored if row.get("feedback_inferred") is True
    )
    feedback_scored_count = len(scored)
    feedback_scores = [float(row["turn_feedback_score"]) for row in scored]
    positive_count = sum(1 for score in feedback_scores if score > 0)
    negative_count = sum(1 for score in feedback_scores if score < 0)

    source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    high_score_source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mid_low_source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    top1_counts: Counter[str] = Counter()
    for row in joined:
        source = _normalize_source_type(row.get("source_type"))
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
        average_feedback_score=_average(feedback_scores),
        top1_positive_rate=_rate(positive_count, feedback_scored_count),
        top1_negative_rate=_rate(negative_count, feedback_scored_count),
        bucket_feedback=bucket_feedback,
        dominant_low_feedback_sources=dominant_low_feedback_sources,
    )

    return {
        "sample_count": len(joined),
        "feedback_joined_count": feedback_joined_count,
        "feedback_inferred_count": feedback_inferred_count,
        "feedback_scored_count": feedback_scored_count,
        "feedback_missing_count": len(joined) - feedback_scored_count,
        "average_feedback_score": _average(feedback_scores),
        "top1_positive_rate": _rate(positive_count, feedback_scored_count),
        "top1_negative_rate": _rate(negative_count, feedback_scored_count),
        "feedback_score_population": "explicit_and_inferred",
        "feedback_rate_denominator": "feedback_scored_count",
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
        "score_version": FEEDBACK_SCORE_VERSION,
    }


def _feedback_events_by_turn(
    feedback_events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    events_by_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in feedback_events:
        if not isinstance(event, Mapping):
            continue
        safe = sanitize_recommendation_feedback_event(event)
        key = (_clean_text(safe.get("lanlan_name")), _clean_text(safe.get("turn_id")))
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
            _clean_text(observation.get("lanlan_name")),
            _clean_text(observation.get("turn_id")),
        )
        events = list(events_by_turn.get(key, ()))
        if not key[0] or not key[1] or not events:
            continue
        if _reward_v2_preview_attribution_issue(observation, events) is not None:
            continue
        replies = [
            event
            for event in events
            if _clean_text(event.get("event_type")) in _REWARD_V2_PREVIEW_REPLY_EVENTS
        ]
        if not replies:
            continue
        reply = min(
            replies,
            key=lambda event: _number(event.get("ts"), float("inf")),
        )
        event_ts = _number(
            reply.get("ts"),
            _number(observation.get("ts"), float("inf")),
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
    latency = _number(metadata.get("reply_latency_seconds"), float("nan"))
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
                source = _normalize_source_type(candidate.get("source_type"))
                if source:
                    return source
    return _normalize_source_type(row.get("shadow_selected_source_type"))


def _shadow_selected_score(row: Mapping[str, Any]) -> float | None:
    score = _number(row.get("shadow_selected_score"), float("nan"))
    if score == score:
        return round(score, 3)
    candidates = row.get("top_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                score = _number(candidate.get("score"), float("nan"))
                if score == score:
                    return round(score, 3)
    return None


def _score_bucket(score: Any) -> str | None:
    value = _number(score, float("nan"))
    if value != value:
        return None
    if value >= 0.75:
        return "high"
    if value >= 0.50:
        return "mid"
    return "low"


def _average(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _average_joined_feedback(rows: Sequence[Mapping[str, Any]]) -> float:
    scores = [
        float(row["turn_feedback_score"])
        for row in rows
        if isinstance(row.get("turn_feedback_score"), (int, float))
    ]
    average = _average(scores)
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
            "average_feedback_score": _average(scores),
            "positive_rate": _rate(positive, len(scores)),
            "negative_rate": _rate(negative, len(scores)),
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
        if _rate(count, total) is not None
        and float(_rate(count, total) or 0.0) >= 0.60
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
        source = _normalize_source_type(row.get("source_type"))
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
            "confidence_positive_rate": _rate(
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
        adjustment = round(_number(suggestion.get("adjustment"), 0.0), 3)
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


def _select_feedback_events_for_turn(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_group: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = sanitize_recommendation_feedback_event(raw)
        group = str(event.get("event_group") or "unknown")
        previous = by_group.get(group)
        if previous is None or abs(_number(event.get("report_score_v1"), 0.0)) > abs(
            _number(previous.get("report_score_v1"), 0.0)
        ):
            by_group[group] = event
    return list(by_group.values())


def _reward_v2_preview_attribution_issue(
    observation: Mapping[str, Any],
    feedback_events: Sequence[Mapping[str, Any]],
) -> str | None:
    """Validate that feedback belongs to the material actually delivered."""
    if observation.get("delivered") is not True:
        return "observation_not_delivered"
    expected_source = _normalize_source_type(
        observation.get("actual_primary_channel")
        or observation.get("shadow_selected_source_type")
    )
    expected_candidate_id = (
        _clean_text(observation.get("shadow_selected_candidate_id")) or None
        if observation.get("matched_actual_material") is True
        else None
    )
    for raw_event in feedback_events:
        event = sanitize_recommendation_feedback_event(raw_event)
        event_source = _normalize_source_type(event.get("source_type"))
        if (
            event_source != "unknown"
            and expected_source != "unknown"
            and event_source != expected_source
        ):
            return "source_mismatch"
        event_candidate_id = _clean_text(event.get("candidate_id")) or None
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
        if 0 <= now - _number(row.get("ts"), -1.0) <= max(0, int(window_seconds))
    ]
    limit = max(0, int(sample_limit))
    return recent[-limit:] if limit else []


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
