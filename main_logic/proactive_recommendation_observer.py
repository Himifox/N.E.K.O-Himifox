"""Local observation sink for proactive recommendation shadow decisions.

This module deliberately handles only sanitized diagnostics. It does not fetch
sources, deliver messages, call LLMs, or alter proactive chat behavior.
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
import json
import logging
import os
from pathlib import Path
import time
from typing import Any


logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_observer")

OBSERVATION_LOG_FILENAME = "proactive_recommendation_observations.jsonl"
DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024
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

_TOP_LEVEL_KEYS = {
    "ts",
    "lanlan_name",
    "turn_id",
    "recommendation_mode",
    "decision_stage",
    "candidate_count",
    "shadow_selected_source_type",
    "shadow_selected_candidate_id",
    "shadow_selected_score",
    "top_candidates",
    "actual_primary_channel",
    "actual_source_tag",
    "actual_reason_code",
    "actual_stage",
    "active_channels",
    "delivered",
    "actual_rank",
    "actual_candidate_score",
    "matched_actual_material",
    "matched_actual_source",
    "active_bias_applied",
    "active_preferred_source_type",
    "active_preferred_source_tag",
    "active_preferred_candidate_id",
    "active_bias_fallback_reason",
    "active_model_followed_preference",
}
_TOP_CANDIDATE_KEYS = {"rank", "id", "source_type", "family", "topic", "score"}
_EXAMPLE_KEYS = {
    "turn_id",
    "ts",
    "decision_stage",
    "shadow_selected_source_type",
    "actual_primary_channel",
    "actual_rank",
    "top_candidates",
}


def sanitize_recommendation_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact, file-safe observation shape used by JSONL logging."""
    safe: dict[str, Any] = {}
    for key in _TOP_LEVEL_KEYS:
        if key not in observation:
            continue
        if key == "top_candidates":
            safe[key] = _sanitize_top_candidates(observation.get(key))
        elif key == "active_channels":
            safe[key] = _clean_string_list(observation.get(key))
        else:
            safe[key] = _json_safe_scalar(observation.get(key))
    return safe


def append_recommendation_observation_jsonl(
    observation: Mapping[str, Any],
    *,
    log_mode: str = "off",
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    rotate_bytes: int = DEFAULT_ROTATE_BYTES,
) -> bool:
    """Append one sanitized observation to a local JSONL file when enabled."""
    if log_mode != "jsonl":
        return False
    target = _resolve_observation_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target, rotate_bytes=rotate_bytes)
        safe = sanitize_recommendation_observation(observation)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        logger.debug("proactive recommendation observation append failed: %s", exc)
        return False


def load_recommendation_observations_jsonl(
    path: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read observations from JSONL, returning the newest ``limit`` rows."""
    target = Path(path)
    if not target.exists():
        return []
    rows: deque[dict[str, Any]] | list[dict[str, Any]]
    rows = deque(maxlen=limit) if limit and limit > 0 else []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, Mapping):
                    rows.append(sanitize_recommendation_observation(item))
    except Exception as exc:
        logger.debug("proactive recommendation observation read failed: %s", exc)
        return []
    return list(rows)


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
        _top1_source_type(row)
        for row in rows
        if _top1_source_type(row)
    )
    stage_counts = Counter(
        str(row.get("decision_stage") or "unknown")
        for row in rows
    )
    pass_high_score_count = sum(
        1
        for row in passes
        if _number(row.get("shadow_selected_score"), 0.0) >= high_score_threshold
    )

    return {
        "total": total,
        "delivered_count": len(delivered),
        "pass_count": len(passes),
        "source_match_rate": _rate(
            sum(1 for row in delivered if row.get("matched_actual_source") is True),
            len(delivered),
        ),
        "material_match_rate": _rate(
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
    pass_high_score_rate = _rate(summary["pass_high_score_count"], sample_count)

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
    if material_match_rate is None or material_match_rate < ACTIVE_READY_MATERIAL_MATCH_RATE:
        issues.append("material_ranking_drift")
        reasons.append("material_match_rate_below_threshold")

    average_rank = summary["average_actual_rank"]
    if average_rank is None or average_rank > ACTIVE_READY_AVERAGE_RANK:
        issues.append("ranking_order_drift")
        reasons.append("average_actual_rank_above_threshold")

    if pass_high_score_rate is not None and pass_high_score_rate >= ACTIVE_READY_PASS_HIGH_SCORE_RATE:
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
        row
        for row in delivered
        if row.get("matched_actual_source") is False
    ]
    material_drift = [
        row
        for row in delivered
        if (
            row.get("matched_actual_source") is True
            and (
                row.get("matched_actual_material") is False
                or _actual_rank(row) > 1
            )
        )
    ]
    pass_conflict = [
        row
        for row in samples
        if (
            row.get("delivered") is not True
            and _number(row.get("shadow_selected_score"), 0.0) >= high_score_threshold
        )
    ]
    low_quality_top1 = [
        row
        for row in samples
        if _is_low_quality_top1(row)
    ]
    top1_counts = Counter(
        _top1_source_type(row)
        for row in samples
        if _top1_source_type(row)
    )
    top1_candidate_counts = Counter(
        _top1_candidate_id(row)
        for row in samples
        if _top1_candidate_id(row)
    )
    dominant_source_type = ""
    dominant_source_count = 0
    if top1_counts:
        dominant_source_type, dominant_source_count = top1_counts.most_common(1)[0]
    dominant_source_rate = _rate(dominant_source_count, len(samples))
    source_overuse = bool(
        len(samples) >= VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT
        and dominant_source_type
        and dominant_source_rate is not None
        and dominant_source_rate >= VALIDATION_SOURCE_OVERUSE_RATE
    )
    dominant_candidate_id = ""
    dominant_candidate_count = 0
    if top1_candidate_counts:
        dominant_candidate_id, dominant_candidate_count = top1_candidate_counts.most_common(1)[0]
    dominant_candidate_rate = _rate(dominant_candidate_count, len(samples))
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
    issues = [
        issue
        for issue, count in issue_counts.items()
        if count > 0
    ]

    return {
        "sample_count": len(samples),
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "issues": issues,
        "issue_counts": issue_counts,
        "rates": {
            "source_drift": _rate(len(source_drift), len(delivered)),
            "material_drift": _rate(len(material_drift), len(delivered)),
            "pass_conflict": _rate(len(pass_conflict), len(samples)),
            "source_overuse": dominant_source_rate if source_overuse else 0.0,
            "candidate_overuse": dominant_candidate_rate if candidate_overuse else 0.0,
            "low_quality_top1": _rate(len(low_quality_top1), len(samples)),
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
                    if candidate_overuse and _top1_candidate_id(row) == dominant_candidate_id
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
        mismatch = row.get("delivered") is True and row.get("matched_actual_material") is False
        pass_high_score = (
            row.get("delivered") is not True
            and _number(row.get("shadow_selected_score"), 0.0) >= high_score_threshold
        )
        if mismatch:
            group = 0
        elif pass_high_score:
            group = 1
        else:
            group = 2
        return (group, -_number(row.get("ts"), 0.0))

    selected = sorted(rows, key=priority)[:example_limit]
    return [_example_from_observation(row) for row in selected]


def _resolve_observation_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    if config_dir is None:
        return None
    return Path(config_dir) / OBSERVATION_LOG_FILENAME


def _rotate_if_needed(path: Path, *, rotate_bytes: int) -> None:
    if rotate_bytes <= 0:
        return
    try:
        if path.exists() and path.stat().st_size > rotate_bytes:
            os.replace(path, path.parent / (path.name + ".1"))
    except OSError as exc:
        logger.debug("proactive recommendation observation rotate failed: %s", exc)


def _sanitize_top_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        clean = {
            key: _json_safe_scalar(item.get(key))
            for key in _TOP_CANDIDATE_KEYS
            if key in item
        }
        if clean:
            out.append(clean)
    return out


def _json_safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe_scalar(item) for item in value]
    return str(value)


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


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
    topic = str(top.get("topic") or "").strip()
    source_type = str(top.get("source_type") or "").strip()
    candidate_id = str(top.get("id") or "").strip()
    score = _number(top.get("score"), -1.0)
    return not source_type or not candidate_id or len(topic) < 4 or score < 0.2


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _is_recent_observation(
    row: Mapping[str, Any],
    *,
    now: float,
    window_seconds: int,
) -> bool:
    ts = _number(row.get("ts"), -1.0)
    if ts < 0:
        return False
    return 0 <= now - ts <= window_seconds


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _example_from_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    example = {
        key: row.get(key)
        for key in _EXAMPLE_KEYS
        if key in row
    }
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
