"""Local observation sink for proactive recommendation shadow decisions.

This module deliberately handles only sanitized diagnostics. It does not fetch
sources, deliver messages, call LLMs, or alter proactive chat behavior.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
import time
from typing import Any

from main_logic.proactive_recommendation.normalization import rounded_ratio_or_none

from main_logic.proactive_recommendation.observation.validation import (
    sanitize_recommendation_observation,
    sanitize_recommendation_policy_decision,
)
from main_logic.proactive_recommendation.normalization import (
    coerce_float_or_default,
)


DEFAULT_HIGH_SCORE_THRESHOLD = 0.75
DEFAULT_EXAMPLE_LIMIT = 10
MAX_EXAMPLE_LIMIT = 20
CALIBRATION_WINDOW_SECONDS = 3600
CALIBRATION_SAMPLE_LIMIT = 50
ACTIVE_READY_MIN_SAMPLE_COUNT = 30
ACTIVE_READY_SOURCE_MATCH_RATE = 0.75
ACTIVE_READY_MATERIAL_MATCH_RATE = 0.65
ACTIVE_READY_AVERAGE_RANK = 1.8
ACTIVE_READY_PASS_HIGH_SCORE_RATE = 0.15
VALIDATION_SOURCE_OVERUSE_RATE = 0.6
VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT = 5
VALIDATION_CANDIDATE_OVERUSE_RATE = 0.35
VALIDATION_CANDIDATE_OVERUSE_MIN_SAMPLE_COUNT = 5
VALIDATION_EXAMPLE_LIMIT_PER_ISSUE = 3
_EXAMPLE_KEYS = {
    "turn_id",
    "ts",
    "decision_stage",
    "shadow_selected_source_type",
    "actual_primary_channel",
    "actual_rank",
    "top_candidates",
    "review_context",
}


def summarize_recommendation_policy(
    observations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize policy exposure and propensity integrity for runtime monitoring."""
    modes: Counter[str] = Counter()
    proposals: Counter[str] = Counter()
    actuals: Counter[str] = Counter()
    policy_count = 0
    applied_count = 0
    explored_count = 0
    exploration_eligible_count = 0
    probability_violation_count = 0
    for observation in observations:
        raw_policy = observation.get("policy_decision")
        if not isinstance(raw_policy, Mapping):
            continue
        policy_count += 1
        policy = sanitize_recommendation_policy_decision(raw_policy)
        if not policy:
            probability_violation_count += 1
            continue
        modes[str(policy["mode"])] += 1
        proposed = str(policy.get("proposed_arm") or policy.get("chosen_arm") or "")
        actual = str(policy.get("actual_arm") or "")
        if proposed:
            proposals[proposed] += 1
        if actual:
            actuals[actual] += 1
        elif policy.get("context_version") == "source-context-v1" and proposed:
            actuals[proposed] += 1
        applied_count += int(policy.get("policy_applied") is True)
        exploration_eligible_count += int(policy.get("exploration_eligible") is True)
        explored_count += int(policy.get("explored") is True)
    total_choices = sum(actuals.values())
    distribution = {
        source: round(count / total_choices, 6) if total_choices else 0.0
        for source, count in sorted(actuals.items())
    }
    return {
        "policy_observation_count": policy_count,
        "valid_policy_observation_count": policy_count - probability_violation_count,
        "mode_distribution": dict(sorted(modes.items())),
        "proposed_arm_count": dict(sorted(proposals.items())),
        "actual_arm_count": dict(sorted(actuals.items())),
        "policy_applied_count": applied_count,
        "chosen_arm_count": dict(sorted(proposals.items())),
        "chosen_arm_distribution": distribution,
        "exploration_eligible_count": exploration_eligible_count,
        "explored_count": explored_count,
        "observed_exploration_rate": round(
            explored_count / exploration_eligible_count, 6
        )
        if exploration_eligible_count
        else 0.0,
        "max_source_exposure_rate": max(distribution.values(), default=0.0),
        "hhi": round(sum(value * value for value in distribution.values()), 6),
        "probability_violation_count": probability_violation_count,
        "hard_gate_pass": probability_violation_count == 0,
    }


def summarize_recommendation_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    limit: int | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
) -> dict[str, Any]:
    """Aggregate shadow-observation quality metrics for local diagnostics."""
    rows = [
        sanitize_recommendation_observation(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    if limit and limit > 0:
        rows = rows[-limit:]

    total = len(rows)
    delivered = [row for row in rows if row.get("delivered") is True]
    passes = [row for row in rows if row.get("delivered") is not True]
    ranks = [
        int(row["actual_rank"])
        for row in delivered
        if isinstance(row.get("actual_rank"), int)
    ]
    top1_sources = Counter(
        _top1_source_type(row) for row in rows if _top1_source_type(row)
    )
    stage_counts = Counter(str(row.get("decision_stage") or "unknown") for row in rows)
    pass_high_score_count = sum(
        1
        for row in passes
        if coerce_float_or_default(row.get("shadow_selected_score"), default=0.0)
        >= high_score_threshold
    )

    return {
        "total": total,
        "delivered_count": len(delivered),
        "pass_count": len(passes),
        "source_match_rate": rounded_ratio_or_none(
            sum(1 for row in delivered if row.get("matched_actual_source") is True),
            len(delivered),
        ),
        "material_match_rate": rounded_ratio_or_none(
            sum(1 for row in delivered if row.get("matched_actual_material") is True),
            len(delivered),
        ),
        "average_actual_rank": round(sum(ranks) / len(ranks), 3) if ranks else None,
        "shadow_top1_by_source_type": dict(sorted(top1_sources.items())),
        "decision_stage_counts": dict(sorted(stage_counts.items())),
        "pass_high_score_count": pass_high_score_count,
        "high_score_threshold": float(high_score_threshold),
    }


def get_recommendation_calibration_samples(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = CALIBRATION_WINDOW_SECONDS,
    sample_limit: int = CALIBRATION_SAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Return sanitized observations in the current calibration window."""
    current = time.time() if now is None else float(now)
    window = max(0, int(window_seconds))
    limit = max(0, int(sample_limit))
    rows = [
        sanitize_recommendation_observation(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    recent = [
        row
        for row in rows
        if _is_recent_observation(row, now=current, window_seconds=window)
    ]
    if limit <= 0:
        return []
    return recent[-limit:]


def summarize_recommendation_calibration(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    window_seconds: int = CALIBRATION_WINDOW_SECONDS,
    sample_limit: int = CALIBRATION_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Summarize whether shadow ranking is stable enough to discuss active mode."""
    samples = get_recommendation_calibration_samples(
        observations,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    summary = summarize_recommendation_observations(
        samples,
        high_score_threshold=high_score_threshold,
    )
    sample_count = summary["total"]
    pass_high_score_rate = rounded_ratio_or_none(
        summary["pass_high_score_count"], sample_count
    )

    issues: list[str] = []
    reasons: list[str] = []
    if sample_count < ACTIVE_READY_MIN_SAMPLE_COUNT:
        issues.append("low_sample_count")
        reasons.append("sample_count_below_threshold")

    source_match_rate = summary["source_match_rate"]
    if source_match_rate is None or source_match_rate < ACTIVE_READY_SOURCE_MATCH_RATE:
        issues.append("source_selection_drift")
        reasons.append("source_match_rate_below_threshold")

    material_match_rate = summary["material_match_rate"]
    if (
        material_match_rate is None
        or material_match_rate < ACTIVE_READY_MATERIAL_MATCH_RATE
    ):
        issues.append("material_ranking_drift")
        reasons.append("material_match_rate_below_threshold")

    average_rank = summary["average_actual_rank"]
    if average_rank is None or average_rank > ACTIVE_READY_AVERAGE_RANK:
        issues.append("ranking_order_drift")
        reasons.append("average_actual_rank_above_threshold")

    if (
        pass_high_score_rate is not None
        and pass_high_score_rate >= ACTIVE_READY_PASS_HIGH_SCORE_RATE
    ):
        issues.append("pass_gate_conflict")
        reasons.append("pass_high_score_rate_above_threshold")

    return {
        "sample_count": sample_count,
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "source_match_rate": source_match_rate,
        "material_match_rate": material_match_rate,
        "average_actual_rank": average_rank,
        "pass_high_score_count": summary["pass_high_score_count"],
        "pass_high_score_rate": pass_high_score_rate,
        "active_ready": not issues,
        "active_ready_reasons": reasons,
        "calibration_issues": issues,
        "thresholds": {
            "min_sample_count": ACTIVE_READY_MIN_SAMPLE_COUNT,
            "source_match_rate": ACTIVE_READY_SOURCE_MATCH_RATE,
            "material_match_rate": ACTIVE_READY_MATERIAL_MATCH_RATE,
            "average_actual_rank": ACTIVE_READY_AVERAGE_RANK,
            "pass_high_score_rate": ACTIVE_READY_PASS_HIGH_SCORE_RATE,
            "high_score_threshold": float(high_score_threshold),
        },
        "summary": summary,
    }


def summarize_recommendation_validation(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    window_seconds: int = CALIBRATION_WINDOW_SECONDS,
    sample_limit: int = CALIBRATION_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Classify shadow recommendation mismatches for manual rule calibration."""
    samples = get_recommendation_calibration_samples(
        observations,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    summary = summarize_recommendation_observations(
        samples,
        high_score_threshold=high_score_threshold,
    )
    delivered = [row for row in samples if row.get("delivered") is True]
    source_drift = [
        row for row in delivered if row.get("matched_actual_source") is False
    ]
    material_drift = [
        row
        for row in delivered
        if (
            row.get("matched_actual_source") is True
            and (row.get("matched_actual_material") is False or _actual_rank(row) > 1)
        )
    ]
    pass_conflict = [
        row
        for row in samples
        if (
            row.get("delivered") is not True
            and coerce_float_or_default(row.get("shadow_selected_score"), default=0.0)
            >= high_score_threshold
        )
    ]
    low_quality_top1 = [row for row in samples if _is_low_quality_top1(row)]
    top1_counts = Counter(
        _top1_source_type(row) for row in samples if _top1_source_type(row)
    )
    top1_candidate_counts = Counter(
        _top1_candidate_id(row) for row in samples if _top1_candidate_id(row)
    )
    dominant_source_type = ""
    dominant_source_count = 0
    if top1_counts:
        dominant_source_type, dominant_source_count = top1_counts.most_common(1)[0]
    dominant_source_rate = rounded_ratio_or_none(dominant_source_count, len(samples))
    source_overuse = bool(
        len(samples) >= VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT
        and dominant_source_type
        and dominant_source_rate is not None
        and dominant_source_rate >= VALIDATION_SOURCE_OVERUSE_RATE
    )
    dominant_candidate_id = ""
    dominant_candidate_count = 0
    if top1_candidate_counts:
        dominant_candidate_id, dominant_candidate_count = (
            top1_candidate_counts.most_common(1)[0]
        )
    dominant_candidate_rate = rounded_ratio_or_none(
        dominant_candidate_count, len(samples)
    )
    candidate_overuse = bool(
        len(samples) >= VALIDATION_CANDIDATE_OVERUSE_MIN_SAMPLE_COUNT
        and dominant_candidate_id
        and dominant_candidate_rate is not None
        and dominant_candidate_rate >= VALIDATION_CANDIDATE_OVERUSE_RATE
    )

    issue_counts = {
        "source_drift": len(source_drift),
        "material_drift": len(material_drift),
        "pass_conflict": len(pass_conflict),
        "source_overuse": 1 if source_overuse else 0,
        "candidate_overuse": 1 if candidate_overuse else 0,
        "low_quality_top1": len(low_quality_top1),
    }
    issues = [issue for issue, count in issue_counts.items() if count > 0]

    return {
        "sample_count": len(samples),
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "issues": issues,
        "issue_counts": issue_counts,
        "rates": {
            "source_drift": rounded_ratio_or_none(len(source_drift), len(delivered)),
            "material_drift": rounded_ratio_or_none(
                len(material_drift), len(delivered)
            ),
            "pass_conflict": rounded_ratio_or_none(len(pass_conflict), len(samples)),
            "source_overuse": dominant_source_rate if source_overuse else 0.0,
            "candidate_overuse": dominant_candidate_rate if candidate_overuse else 0.0,
            "low_quality_top1": rounded_ratio_or_none(
                len(low_quality_top1), len(samples)
            ),
        },
        "dominant_source_type": dominant_source_type or None,
        "dominant_source_rate": dominant_source_rate,
        "dominant_candidate_id": dominant_candidate_id or None,
        "dominant_candidate_rate": dominant_candidate_rate,
        "suggested_weight_adjustments": _suggested_weight_adjustments(
            issues,
            dominant_source_type=dominant_source_type,
        ),
        "examples": {
            "source_drift": _validation_examples(source_drift),
            "material_drift": _validation_examples(material_drift),
            "pass_conflict": _validation_examples(pass_conflict),
            "source_overuse": _validation_examples(
                [
                    row
                    for row in samples
                    if source_overuse and _top1_source_type(row) == dominant_source_type
                ]
            ),
            "candidate_overuse": _validation_examples(
                [
                    row
                    for row in samples
                    if candidate_overuse
                    and _top1_candidate_id(row) == dominant_candidate_id
                ]
            ),
            "low_quality_top1": _validation_examples(low_quality_top1),
        },
        "summary": summary,
        "thresholds": {
            "high_score_threshold": float(high_score_threshold),
            "source_overuse_rate": VALIDATION_SOURCE_OVERUSE_RATE,
            "source_overuse_min_sample_count": VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT,
            "candidate_overuse_rate": VALIDATION_CANDIDATE_OVERUSE_RATE,
            "candidate_overuse_min_sample_count": VALIDATION_CANDIDATE_OVERUSE_MIN_SAMPLE_COUNT,
        },
    }


def select_recommendation_observation_examples(
    observations: Iterable[Mapping[str, Any]],
    *,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Return compact diagnostic examples, prioritizing mismatches and high-score passes."""
    rows = [
        sanitize_recommendation_observation(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    example_limit = max(0, min(int(limit), MAX_EXAMPLE_LIMIT))
    if example_limit <= 0:
        return []

    def priority(row: Mapping[str, Any]) -> tuple[int, float]:
        mismatch = (
            row.get("delivered") is True and row.get("matched_actual_material") is False
        )
        pass_high_score = (
            row.get("delivered") is not True
            and coerce_float_or_default(row.get("shadow_selected_score"), default=0.0)
            >= high_score_threshold
        )
        if mismatch:
            group = 0
        elif pass_high_score:
            group = 1
        else:
            group = 2
        return (group, -coerce_float_or_default(row.get("ts"), default=0.0))

    selected = sorted(rows, key=priority)[:example_limit]
    return [_example_from_observation(row) for row in selected]


def _top1_source_type(row: Mapping[str, Any]) -> str:
    candidates = row.get("top_candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return ""
    if not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, Mapping):
        return ""
    return str(first.get("source_type") or "").strip()


def _top1_candidate(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidates = row.get("top_candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return None
    if not candidates:
        return None
    first = candidates[0]
    return first if isinstance(first, Mapping) else None


def _top1_candidate_id(row: Mapping[str, Any]) -> str:
    top = _top1_candidate(row)
    if top is None:
        return ""
    return str(top.get("id") or "").strip()


def _actual_rank(row: Mapping[str, Any]) -> int:
    value = row.get("actual_rank")
    return int(value) if isinstance(value, int) else 0


def _is_low_quality_top1(row: Mapping[str, Any]) -> bool:
    if row.get("candidate_count") == 0:
        return False
    top = _top1_candidate(row)
    if top is None:
        return True
    source_type = str(top.get("source_type") or "").strip()
    candidate_id = str(top.get("id") or "").strip()
    score = coerce_float_or_default(top.get("score"), default=-1.0)
    topic_usable = top.get("topic_usable") is True
    return not source_type or not candidate_id or not topic_usable or score < 0.2


def _is_recent_observation(
    row: Mapping[str, Any],
    *,
    now: float,
    window_seconds: int,
) -> bool:
    ts = coerce_float_or_default(row.get("ts"), default=-1.0)
    if ts < 0:
        return False
    return 0 <= now - ts <= window_seconds


def _example_from_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    example = {key: row.get(key) for key in _EXAMPLE_KEYS if key in row}
    if "actual_reason_code" in row:
        example["reason_code"] = row.get("actual_reason_code")
    return example


def _validation_examples(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        _example_from_observation(row)
        for row in rows[:VALIDATION_EXAMPLE_LIMIT_PER_ISSUE]
    ]


def _suggested_weight_adjustments(
    issues: Sequence[str],
    *,
    dominant_source_type: str,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    issue_set = set(issues)
    if "pass_conflict" in issue_set:
        suggestions.append(
            {
                "target": "interruption_cost",
                "adjustment": "+0.05",
                "reason": "pass_conflict",
            }
        )
    if "material_drift" in issue_set:
        suggestions.append(
            {
                "target": "source_quality",
                "adjustment": "+0.05",
                "reason": "material_drift",
            }
        )
    if "source_drift" in issue_set:
        suggestions.append(
            {
                "target": "context_match",
                "adjustment": "+0.05",
                "reason": "source_drift",
            }
        )
    if "source_overuse" in issue_set or "candidate_overuse" in issue_set:
        suggestions.append(
            {
                "target": "diversity_penalty",
                "adjustment": "+0.05",
                "reason": (
                    "candidate_overuse"
                    if "candidate_overuse" in issue_set
                    else "source_overuse"
                ),
            }
        )
    if "source_overuse" in issue_set and dominant_source_type:
        suggestions.append(
            {
                "target": f"source_type.{dominant_source_type}",
                "adjustment": "-0.05",
                "reason": "source_overuse",
            }
        )
        if dominant_source_type == "music":
            suggestions.append(
                {
                    "target": "music.novelty",
                    "adjustment": "+0.05",
                    "reason": "source_overuse",
                }
            )
    if "low_quality_top1" in issue_set and "material_drift" not in issue_set:
        suggestions.append(
            {
                "target": "source_quality",
                "adjustment": "+0.05",
                "reason": "low_quality_top1",
            }
        )
    return suggestions
