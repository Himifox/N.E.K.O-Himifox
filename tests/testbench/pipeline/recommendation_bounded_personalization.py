"""Point-in-time G1-R1 source-affinity impact simulation.

This module is Testbench-only. It never writes production state and never
interprets shared conversation acceptance as a relative source preference.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from typing import Any


PREVIEW_VERSION = "feedback_state_preview_v2"
CANDIDATE_ID = "persistent_source_affinity_max_003_v1"
DEFAULT_MAX_ABS_DELTA = 0.03
MAX_ALLOWED_ABS_DELTA = 0.03


class BoundedPersonalizationError(ValueError):
    pass


def analyze_bounded_personalization(
    dataset: Mapping[str, Any],
    *,
    max_abs_delta: float = DEFAULT_MAX_ABS_DELTA,
    as_of: float | None = None,
) -> dict[str, Any]:
    """Compare baseline ranking with one bounded persistent-affinity candidate."""
    delta_limit = _validate_delta(max_abs_delta)
    observations = _mapping_rows(dataset.get("observations"), "observations")
    feedback = _mapping_rows(dataset.get("feedback") or [], "feedback")
    cutoff = _resolve_as_of(observations, dataset.get("as_of") if as_of is None else as_of)
    issues: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    seen_turns: set[tuple[str, str]] = set()

    for observation in sorted(observations, key=_timestamp):
        if _timestamp(observation) > cutoff:
            issues["observation_after_as_of"] += 1
            continue
        preview = observation.get("feedback_state_preview")
        if not isinstance(preview, Mapping) or preview.get("version") != PREVIEW_VERSION:
            issues["not_feedback_state_preview_v2"] += 1
            continue
        if str(observation.get("recommendation_mode") or "").strip().lower() != "shadow":
            issues["not_shadow"] += 1
            continue
        turn_id = str(observation.get("turn_id") or "").strip()
        if not turn_id:
            issues["missing_turn_id"] += 1
            continue
        turn_key = (str(observation.get("lanlan_name") or ""), turn_id)
        if turn_key in seen_turns:
            issues["duplicate_turn_id"] += 1
            continue
        seen_turns.add(turn_key)
        candidates = _candidate_rows(observation.get("top_candidates"))
        if not candidates:
            issues["no_ranked_candidates"] += 1
            continue
        rows.append(_simulate_row(observation, candidates, delta_limit))

    baseline_sources = Counter(row["baseline_top1_source"] for row in rows)
    candidate_sources = Counter(row["candidate_top1_source"] for row in rows)
    adjusted_rows = [row for row in rows if row["adjusted_candidate_count"]]
    flips = [row for row in rows if row["top1_changed"]]
    source_evidence = _source_evidence_summary(observations, cutoff)
    source_scores = _source_score_impact(rows)
    hard_violations = _hard_violations(rows, delta_limit)
    status = "impact_only" if adjusted_rows and not hard_violations else "insufficient_evidence"
    positive_evidence = sum(item["positive_evidence_count"] for item in source_evidence.values())
    negative_evidence = sum(item["negative_evidence_count"] for item in source_evidence.values())
    effectiveness_blockers = ["no_counterfactual_or_human_outcome_labels"]
    if not positive_evidence:
        effectiveness_blockers.append("no_positive_source_evidence")
    if not negative_evidence:
        effectiveness_blockers.append("no_negative_source_evidence")

    return {
        "schema_version": 1,
        "analysis": "p44_g1_r1_bounded_personalization_impact",
        "candidate": {
            "id": CANDIDATE_ID,
            "max_abs_score_delta": delta_limit,
            "state_scope": "source_affinity.persistent",
            "minimum_evidence_respected": True,
            "conversation_acceptance_ranking_consumed": False,
            "production_ranking_consumed": False,
            "tuning_consumed": False,
        },
        "input": {
            "as_of": cutoff,
            "sha256": _hash({
                "observations": observations,
                "feedback": feedback,
                "as_of": cutoff,
            }),
            "observation_count": len(observations),
            "feedback_event_count": len(feedback),
            "eligible_ranked_observation_count": len(rows),
        },
        "impact": {
            "warm_state_observation_count": len(adjusted_rows),
            "adjusted_candidate_count": sum(row["adjusted_candidate_count"] for row in rows),
            "top1_flip_count": len(flips),
            "top1_flip_rate": _rate(len(flips), len(rows)),
            "baseline_top1_distribution": dict(sorted(baseline_sources.items())),
            "candidate_top1_distribution": dict(sorted(candidate_sources.items())),
            "baseline_max_source_exposure_rate": _max_share(baseline_sources),
            "candidate_max_source_exposure_rate": _max_share(candidate_sources),
            "baseline_hhi": _hhi(baseline_sources),
            "candidate_hhi": _hhi(candidate_sources),
            "source_score_impact": source_scores,
        },
        "source_evidence": source_evidence,
        "data_issues": {
            "count": sum(issues.values()),
            "distribution": dict(sorted(issues.items())),
        },
        "hard_violations": hard_violations,
        "rows": rows,
        "conclusion": {
            "status": status,
            "effectiveness_evaluated": False,
            "effectiveness_blockers": effectiveness_blockers,
            "candidate_for_shadow": False,
            "reason": (
                "bounded_ranking_impact_only_no_counterfactual_labels"
                if status == "impact_only"
                else "no_valid_warm_state_impact"
            ),
            "production_config_modified": False,
        },
    }


def render_bounded_personalization_markdown(report: Mapping[str, Any]) -> str:
    impact = report["impact"]
    conclusion = report["conclusion"]
    candidate = report["candidate"]
    lines = [
        "# P44-G1-R1 有界个性化排名影响模拟",
        "",
        f"- 状态：`{conclusion['status']}`",
        f"- 候选：`{candidate['id']}`",
        f"- 单候选最大分数变化：±{candidate['max_abs_score_delta']:.3f}",
        f"- 有效 observation：{report['input']['eligible_ranked_observation_count']}",
        f"- Warm-state observation：{impact['warm_state_observation_count']}",
        f"- Top-1 翻转：{impact['top1_flip_count']} ({_display_rate(impact['top1_flip_rate'])})",
        f"- 来源 HHI：{impact['baseline_hhi']:.4f} → {impact['candidate_hhi']:.4f}",
        f"- 最大来源曝光：{_display_rate(impact['baseline_max_source_exposure_rate'])} → "
        f"{_display_rate(impact['candidate_max_source_exposure_rate'])}",
        "- `conversation_acceptance` 未参与来源相对排名。",
        "- 本报告只描述排名影响，不评价效果，不修改生产权重或 tuning。",
        "",
        "## Top-1 来源分布",
        "",
        "| 来源 | Baseline | Candidate |",
        "|---|---:|---:|",
    ]
    sources = sorted(
        set(impact["baseline_top1_distribution"])
        | set(impact["candidate_top1_distribution"])
    )
    for source in sources:
        lines.append(
            f"| `{source}` | {impact['baseline_top1_distribution'].get(source, 0)} | "
            f"{impact['candidate_top1_distribution'].get(source, 0)} |"
        )
    lines.extend([
        "",
        "## 各资源候选分数",
        "",
        "| 来源 | 候选数 | 被调整 | Baseline 平均分 | Candidate 平均分 | 平均变化 | 最大绝对变化 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for source, item in impact["source_score_impact"].items():
        lines.append(
            f"| `{source}` | {item['candidate_count']} | {item['adjusted_candidate_count']} | "
            f"{item['average_baseline_score']:.4f} | {item['average_candidate_score']:.4f} | "
            f"{item['average_score_delta']:+.4f} | {item['max_abs_score_delta']:.4f} |"
        )
    lines.extend(["", "## Top-1 翻转", ""])
    flips = [row for row in report["rows"] if row["top1_changed"]]
    if not flips:
        lines.append("无。")
    else:
        lines.extend([
            "| turn_id | Baseline | Candidate | 调整来源 |",
            "|---|---|---|---|",
        ])
        for row in flips:
            adjusted_sources = sorted({
                item["source_type"]
                for item in row["candidate_scores"]
                if item["score_delta"]
            })
            lines.append(
                f"| `{row['turn_id']}` | `{row['baseline_top1_source']}` | "
                f"`{row['candidate_top1_source']}` | `{','.join(adjusted_sources)}` |"
            )
    return "\n".join(lines) + "\n"


def _simulate_row(
    observation: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    max_abs_delta: float,
) -> dict[str, Any]:
    preview = observation["feedback_state_preview"]
    source_affinity = preview.get("source_affinity") or {}
    persistent = source_affinity.get("persistent") or {}
    minimum = _nonnegative_int(persistent.get("min_explicit_evidence"))
    buckets = persistent.get("sources") if isinstance(persistent, Mapping) else {}
    buckets = buckets if isinstance(buckets, Mapping) else {}
    adjusted: list[dict[str, Any]] = []
    adjusted_count = 0

    for candidate in candidates:
        source = candidate["source_type"]
        bucket = buckets.get(source)
        delta = 0.0
        evidence = 0
        affinity = None
        if isinstance(bucket, Mapping):
            evidence = (
                _nonnegative_int(bucket.get("positive_evidence_count"))
                + _nonnegative_int(bucket.get("negative_evidence_count"))
            )
            affinity = _finite_optional(bucket.get("affinity_preview"))
            if minimum > 0 and evidence >= minimum and affinity is not None:
                delta = round(max(-max_abs_delta, min(max_abs_delta, affinity * max_abs_delta)), 6)
        if delta:
            adjusted_count += 1
        adjusted.append({
            **candidate,
            "baseline_score": candidate["score"],
            "candidate_score": round(candidate["score"] + delta, 6),
            "score_delta": delta,
            "persistent_affinity": affinity,
            "persistent_evidence_count": evidence,
        })

    baseline = sorted(candidates, key=lambda item: (-item["score"], item["rank"], item["id"]))
    reranked = sorted(
        adjusted,
        key=lambda item: (-item["candidate_score"], item["rank"], item["id"]),
    )
    return {
        "turn_id": str(observation.get("turn_id") or ""),
        "ts": _timestamp(observation),
        "baseline_top1_candidate_id": baseline[0]["id"],
        "baseline_top1_source": baseline[0]["source_type"],
        "candidate_top1_candidate_id": reranked[0]["id"],
        "candidate_top1_source": reranked[0]["source_type"],
        "top1_changed": baseline[0]["id"] != reranked[0]["id"],
        "adjusted_candidate_count": adjusted_count,
        "candidate_scores": reranked,
    }


def _candidate_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            continue
        candidate_id = str(raw.get("id") or "").strip()
        source = str(raw.get("source_type") or "").strip().lower()
        score = _finite_optional(raw.get("score"))
        if not candidate_id or not source or score is None:
            continue
        rank = raw.get("rank")
        rows.append({
            "id": candidate_id,
            "source_type": source,
            "rank": rank if isinstance(rank, int) and rank > 0 else index + 1,
            "score": score,
        })
    return rows


def _source_evidence_summary(
    observations: list[Mapping[str, Any]],
    cutoff: float,
) -> dict[str, dict[str, int]]:
    latest: dict[str, dict[str, int]] = {}
    for observation in sorted(observations, key=_timestamp):
        if _timestamp(observation) > cutoff:
            continue
        preview = observation.get("feedback_state_preview")
        if not isinstance(preview, Mapping) or preview.get("version") != PREVIEW_VERSION:
            continue
        persistent = ((preview.get("source_affinity") or {}).get("persistent") or {})
        sources = persistent.get("sources") if isinstance(persistent, Mapping) else None
        if not isinstance(sources, Mapping):
            continue
        for source, bucket in sources.items():
            if not isinstance(bucket, Mapping):
                continue
            latest[str(source)] = {
                "positive_evidence_count": _nonnegative_int(bucket.get("positive_evidence_count")),
                "negative_evidence_count": _nonnegative_int(bucket.get("negative_evidence_count")),
            }
    return dict(sorted(latest.items()))


def _source_score_impact(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        for candidate in row["candidate_scores"]:
            grouped.setdefault(candidate["source_type"], []).append(candidate)
    result: dict[str, dict[str, Any]] = {}
    for source, candidates in sorted(grouped.items()):
        count = len(candidates)
        deltas = [float(item["score_delta"]) for item in candidates]
        result[source] = {
            "candidate_count": count,
            "adjusted_candidate_count": sum(delta != 0 for delta in deltas),
            "average_baseline_score": round(
                sum(float(item["baseline_score"]) for item in candidates) / count,
                6,
            ),
            "average_candidate_score": round(
                sum(float(item["candidate_score"]) for item in candidates) / count,
                6,
            ),
            "average_score_delta": round(sum(deltas) / count, 6),
            "max_abs_score_delta": round(max(map(abs, deltas), default=0.0), 6),
        }
    return result


def _hard_violations(rows: list[Mapping[str, Any]], limit: float) -> list[str]:
    violations: set[str] = set()
    for row in rows:
        for candidate in row["candidate_scores"]:
            delta = candidate["score_delta"]
            score = candidate["candidate_score"]
            if not math.isfinite(delta) or abs(delta) > limit + 1e-9:
                violations.add("score_delta_out_of_bounds")
            if not math.isfinite(score):
                violations.add("non_finite_candidate_score")
    return sorted(violations)


def _mapping_rows(value: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BoundedPersonalizationError(f"{field} must be a list")
    if any(not isinstance(row, Mapping) for row in value):
        raise BoundedPersonalizationError(f"{field} must contain objects")
    return list(value)


def _validate_delta(value: Any) -> float:
    number = _finite_optional(value)
    if number is None or not 0 < number <= MAX_ALLOWED_ABS_DELTA:
        raise BoundedPersonalizationError(
            f"max_abs_delta must be within (0, {MAX_ALLOWED_ABS_DELTA}]"
        )
    return number


def _resolve_as_of(observations: list[Mapping[str, Any]], value: Any) -> float:
    if value is not None:
        cutoff = _finite_optional(value)
        if cutoff is None or cutoff < 0:
            raise BoundedPersonalizationError("as_of must be a finite nonnegative number")
        return cutoff
    return max((_timestamp(row) for row in observations), default=0.0)


def _timestamp(row: Mapping[str, Any]) -> float:
    value = _finite_optional(row.get("ts"))
    return value if value is not None else 0.0


def _finite_optional(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _max_share(counts: Counter[str]) -> float:
    total = sum(counts.values())
    return max((_rate(count, total) for count in counts.values()), default=0.0)


def _hhi(counts: Counter[str]) -> float:
    total = sum(counts.values())
    return round(sum((count / total) ** 2 for count in counts.values()), 6) if total else 0.0


def _display_rate(value: Any) -> str:
    return f"{float(value or 0.0) * 100:.2f}%"


__all__ = [
    "BoundedPersonalizationError",
    "CANDIDATE_ID",
    "DEFAULT_MAX_ABS_DELTA",
    "analyze_bounded_personalization",
    "render_bounded_personalization_markdown",
]
