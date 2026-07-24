"""Testbench-only G1-R2 gradual personalization response curves.

The analysis consumes only point-in-time ``feedback_state_preview_v2``
snapshots embedded in observations.  It never reads or writes production
state, ranking configuration, or tuning configuration.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from typing import Any

from tests.testbench.pipeline.recommendation_bounded_personalization import (
    analyze_bounded_personalization,
)


PREVIEW_VERSION = "feedback_state_preview_v2"
ANALYSIS_ID = "p44_g1_r2_personalization_response_curves"
DEFAULT_VARIANT_ID = "gradual_12"
MAX_ABS_SCORE_DELTA = 0.03
CURVE_SATURATION_EVIDENCE = {
    "gradual_8": 8,
    "gradual_12": 12,
    "gradual_20": 20,
}
VARIANT_IDS = ("current_v1", *CURVE_SATURATION_EVIDENCE)


class PersonalizationResponseCurveError(ValueError):
    pass


def analyze_personalization_response_curves(
    dataset: Mapping[str, Any],
    *,
    as_of: float | None = None,
) -> dict[str, Any]:
    """Compare the registered gradual curves against R1 and baseline."""
    affinity_max, production_minimum = _load_production_contract()
    observations = _mapping_rows(dataset.get("observations"), "observations")
    feedback = _mapping_rows(dataset.get("feedback") or [], "feedback")
    cutoff = _resolve_as_of(
        observations,
        dataset.get("as_of") if as_of is None else as_of,
    )
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
        rows.append(
            _simulate_row(
                observation,
                candidates,
                affinity_max=affinity_max,
                production_minimum=production_minimum,
            )
        )

    variant_impact = {
        variant_id: _variant_impact(rows, variant_id)
        for variant_id in VARIANT_IDS
    }
    trajectories = _evidence_trajectories(
        observations,
        cutoff=cutoff,
        affinity_max=affinity_max,
        production_minimum=production_minimum,
    )
    hard_violations = _hard_violations(
        rows,
        production_minimum=production_minimum,
    )
    r1_match = _compare_with_r1(dataset, rows, cutoff)
    if not r1_match["matches"]:
        hard_violations.append("r1_baseline_or_current_v1_mismatch")
    hard_violations = sorted(set(hard_violations))

    default_impact = variant_impact[DEFAULT_VARIANT_ID]
    default_deltas = sorted(
        abs(float(candidate["variants"][DEFAULT_VARIANT_ID]["score_delta"]))
        for row in rows
        for candidate in row["candidate_scores"]
        if candidate["variants"][DEFAULT_VARIANT_ID]["score_delta"]
    )
    median_delta = _quantile(default_deltas, 0.5)
    baseline_impact = _baseline_impact(rows)
    negative_evidence = sum(
        int(item["negative_evidence_count"])
        for item in _latest_source_evidence(trajectories).values()
    )
    positive_evidence = sum(
        int(item["positive_evidence_count"])
        for item in _latest_source_evidence(trajectories).values()
    )
    mechanical_gates = {
        "median_abs_delta_between_001_and_002": 0.01 <= median_delta <= 0.020000001,
        "cap_rate_at_most_025": default_impact["cap_rate"] <= 0.25,
        "max_source_exposure_increase_at_most_005": (
            default_impact["max_source_exposure_rate"]
            - baseline_impact["max_source_exposure_rate"]
            <= 0.05
        ),
        "hhi_increase_at_most_002": (
            default_impact["hhi"] - baseline_impact["hhi"] <= 0.02
        ),
        "hard_violations_zero": not hard_violations,
    }
    mechanical_pass = all(mechanical_gates.values())

    adjusted_count = default_impact["adjusted_candidate_count"]
    if hard_violations or not adjusted_count:
        status = "insufficient_evidence"
    elif median_delta < 0.01:
        status = "mapping_too_inert"
    elif not negative_evidence:
        status = "hold_for_negative_evidence"
    else:
        status = "response_curve_descriptive_only"

    input_hash = _hash({
        "observations": observations,
        "feedback": feedback,
        "as_of": cutoff,
        "curves": CURVE_SATURATION_EVIDENCE,
        "max_abs_score_delta": MAX_ABS_SCORE_DELTA,
        "production_affinity_max": affinity_max,
        "production_minimum_evidence": production_minimum,
    })
    return {
        "schema_version": 1,
        "analysis": ANALYSIS_ID,
        "input": {
            "as_of": cutoff,
            "sha256": input_hash,
            "observation_count": len(observations),
            "feedback_event_count": len(feedback),
            "eligible_ranked_observation_count": len(rows),
        },
        "contract": {
            "preview_version": PREVIEW_VERSION,
            "production_affinity_max": affinity_max,
            "production_minimum_evidence": production_minimum,
            "max_abs_score_delta": MAX_ABS_SCORE_DELTA,
            "default_variant_id": DEFAULT_VARIANT_ID,
            "curve_saturation_evidence": dict(CURVE_SATURATION_EVIDENCE),
            "conversation_acceptance_ranking_consumed": False,
            "production_ranking_consumed": False,
            "tuning_consumed": False,
        },
        "diagnosis": {
            "current_v1_formula": "affinity_preview * 0.03",
            "gradual_formula": (
                "clip((affinity_preview / production_affinity_max) * "
                "min(1, evidence / saturation_evidence) * 0.03, -0.03, 0.03)"
            ),
            "r1_comparison": r1_match,
            "positive_source_evidence_count": positive_evidence,
            "negative_source_evidence_count": negative_evidence,
        },
        "baseline_impact": baseline_impact,
        "variant_impact": variant_impact,
        "evidence_trajectories": trajectories,
        "data_issues": {
            "count": sum(issues.values()),
            "distribution": dict(sorted(issues.items())),
        },
        "hard_violations": hard_violations,
        "rows": rows,
        "conclusion": {
            "status": status,
            "default_curve_mechanical_pass": mechanical_pass,
            "default_curve_mechanical_gates": mechanical_gates,
            "effectiveness_evaluated": False,
            "candidate_for_shadow": False,
            "production_config_modified": False,
            "blockers": _conclusion_blockers(
                hard_violations=hard_violations,
                negative_evidence=negative_evidence,
                mechanical_pass=mechanical_pass,
            ),
        },
    }


def render_personalization_response_curves_markdown(
    report: Mapping[str, Any],
) -> str:
    """Render a readable report including every observation resource score."""
    contract = report["contract"]
    conclusion = report["conclusion"]
    baseline = report["baseline_impact"]
    variants = report["variant_impact"]
    lines = [
        "# P44-G1-R2 渐进式个性化积分响应曲线",
        "",
        f"- 状态：`{conclusion['status']}`",
        f"- 输入 SHA-256：`{report['input']['sha256']}`",
        f"- as_of：`{report['input']['as_of']}`",
        f"- 有效 observation：{report['input']['eligible_ranked_observation_count']}",
        f"- 默认曲线：`{contract['default_variant_id']}`",
        f"- 积分硬上限：±{contract['max_abs_score_delta']:.3f}",
        "- 本报告只描述离线影响，不修改生产权重、ranking 或 tuning。",
        "",
        "## 当前映射诊断",
        "",
        f"生产 persistent affinity 上限为 {contract['production_affinity_max']:.3f}，"
        f"最少显式证据为 {contract['production_minimum_evidence']} 条。",
        "R1 使用 `affinity_preview × 0.03`；全正向 affinity=0.2 时只增加 0.006，"
        "且证据继续增加不会改变该值。R2 保留方向，另用证据数量逐步提高置信度。",
        f"R1 对照一致：`{report['diagnosis']['r1_comparison']['matches']}`。",
        "",
        "## Variant 总览",
        "",
        "| Variant | 调整候选 | Top-1 翻转 | Top-3 换位 | HHI | 最大来源曝光 | 中位绝对积分 | 触顶率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| `baseline` | 0 | 0 | 0 | {baseline['hhi']:.4f} | "
            f"{_percent(baseline['max_source_exposure_rate'])} | 0.0000 | 0.00% |"
        ),
    ]
    for variant_id in VARIANT_IDS:
        impact = variants[variant_id]
        lines.append(
            f"| `{variant_id}` | {impact['adjusted_candidate_count']} | "
            f"{impact['top1_flip_count']} | {impact['top3_reorder_count']} | "
            f"{impact['hhi']:.4f} | {_percent(impact['max_source_exposure_rate'])} | "
            f"{impact['median_abs_score_delta']:.4f} | {_percent(impact['cap_rate'])} |"
        )

    lines.extend([
        "",
        "## 各来源平均分",
        "",
        "| 来源 | 候选数 | Baseline | current_v1 | gradual_8 | gradual_12 | gradual_20 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    sources = sorted({
        source
        for impact in variants.values()
        for source in impact["source_score_impact"]
    })
    for source in sources:
        default_item = variants[DEFAULT_VARIANT_ID]["source_score_impact"][source]
        cells = []
        for variant_id in VARIANT_IDS:
            item = variants[variant_id]["source_score_impact"][source]
            cells.append(f"{item['average_variant_score']:.4f}")
        lines.append(
            f"| `{source}` | {default_item['candidate_count']} | "
            f"{default_item['average_baseline_score']:.4f} | "
            + " | ".join(cells)
            + " |"
        )

    lines.extend([
        "",
        "## 证据累计轨迹",
        "",
        "| 来源 | 正向 | 负向 | 总证据 | Affinity | current_v1 | gradual_8 | gradual_12 | gradual_20 | 首次 turn_id | 重复快照 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ])
    for source, trajectory in report["evidence_trajectories"].items():
        for point in trajectory:
            deltas = point["score_deltas"]
            lines.append(
                f"| `{source}` | {point['positive_evidence_count']} | "
                f"{point['negative_evidence_count']} | {point['total_evidence_count']} | "
                f"{point['affinity_preview']:+.3f} | {deltas['current_v1']:+.4f} | "
                f"{deltas['gradual_8']:+.4f} | {deltas['gradual_12']:+.4f} | "
                f"{deltas['gradual_20']:+.4f} | `{point['first_turn_id']}` | "
                f"{point['observation_count']} |"
            )

    lines.extend([
        "",
        "## 非 Top-1 Music 分差",
        "",
        "| turn_id | Top-1 来源 | Top-1 分数 | Music 分数 | 需要积分 | gradual_12 积分 | 模拟名次 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    challenger_count = 0
    for row in report["rows"]:
        for candidate in row["candidate_scores"]:
            if candidate["source_type"] != "music" or candidate["baseline_rank"] == 1:
                continue
            challenger_count += 1
            value = candidate["variants"][DEFAULT_VARIANT_ID]
            lines.append(
                f"| `{row['turn_id']}` | `{row['baseline_top1_source']}` | "
                f"{row['baseline_top1_score']:.4f} | {candidate['baseline_score']:.4f} | "
                f"{candidate['gap_to_top']:.4f} | {value['score_delta']:+.4f} | "
                f"{value['rank']} |"
            )
    if not challenger_count:
        lines.append("| — | — | — | — | — | — | — |")

    lines.extend(["", "## 逐 observation 资源分数", ""])
    for index, row in enumerate(report["rows"], start=1):
        lines.extend([
            f"### {index:03d} · `{row['turn_id']}`",
            "",
            f"Baseline Top-1：`{row['baseline_top1_source']}` / "
            f"`{row['baseline_top1_candidate_id']}` / {row['baseline_top1_score']:.4f}",
            "",
            "| 资源 | Candidate ID | 原名次 | Baseline | current_v1 | gradual_8 | gradual_12 | gradual_20 | 证据(+/-) | Affinity | 距 Top-1 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for candidate in row["candidate_scores"]:
            values = candidate["variants"]
            lines.append(
                f"| `{candidate['source_type']}` | `{candidate['id']}` | "
                f"{candidate['baseline_rank']} | {candidate['baseline_score']:.4f} | "
                f"{values['current_v1']['score']:.4f} | "
                f"{values['gradual_8']['score']:.4f} | "
                f"{values['gradual_12']['score']:.4f} | "
                f"{values['gradual_20']['score']:.4f} | "
                f"{candidate['positive_evidence_count']}/{candidate['negative_evidence_count']} | "
                f"{_optional_number(candidate['affinity_preview'])} | "
                f"{candidate['gap_to_top']:.4f} |"
            )
        lines.append("")

    lines.extend([
        "## 结论与门禁",
        "",
        f"- 默认曲线机械门禁：`{conclusion['default_curve_mechanical_pass']}`",
        f"- 效果是否已验证：`{conclusion['effectiveness_evaluated']}`",
        f"- 可进入 Shadow：`{conclusion['candidate_for_shadow']}`",
        f"- 阻塞原因：`{', '.join(conclusion['blockers']) or 'none'}`",
    ])
    return "\n".join(lines) + "\n"


def _load_production_contract() -> tuple[float, int]:
    from main_logic.proactive_recommendation_feedback_state import (
        PERSISTENT_AFFINITY_MAX,
        PERSISTENT_INTEREST_MIN_EVIDENCE,
    )

    affinity_max = float(PERSISTENT_AFFINITY_MAX)
    minimum = int(PERSISTENT_INTEREST_MIN_EVIDENCE)
    if not math.isfinite(affinity_max) or affinity_max <= 0 or minimum <= 0:
        raise PersonalizationResponseCurveError("invalid production preview contract")
    return affinity_max, minimum


def _simulate_row(
    observation: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    *,
    affinity_max: float,
    production_minimum: int,
) -> dict[str, Any]:
    preview = observation["feedback_state_preview"]
    persistent = ((preview.get("source_affinity") or {}).get("persistent") or {})
    snapshot_minimum = _nonnegative_int(persistent.get("min_explicit_evidence"))
    minimum = max(production_minimum, snapshot_minimum)
    buckets = persistent.get("sources") if isinstance(persistent, Mapping) else {}
    buckets = buckets if isinstance(buckets, Mapping) else {}
    baseline = sorted(candidates, key=_baseline_sort_key)
    baseline_rank = {item["id"]: index + 1 for index, item in enumerate(baseline)}
    baseline_top_score = baseline[0]["score"]
    scored: list[dict[str, Any]] = []

    for candidate in baseline:
        bucket = buckets.get(candidate["source_type"])
        positive = negative = 0
        affinity: float | None = None
        if isinstance(bucket, Mapping):
            positive = _nonnegative_int(bucket.get("positive_evidence_count"))
            negative = _nonnegative_int(bucket.get("negative_evidence_count"))
            affinity = _finite_optional(bucket.get("affinity_preview"))
        evidence = positive + negative
        eligible = evidence >= minimum and affinity is not None
        direction = (
            _clamp(affinity / affinity_max, -1.0, 1.0)
            if eligible and affinity is not None
            else 0.0
        )
        variants: dict[str, dict[str, Any]] = {}
        current_delta = (
            _round_delta(_clamp(affinity * MAX_ABS_SCORE_DELTA, -MAX_ABS_SCORE_DELTA, MAX_ABS_SCORE_DELTA))
            if eligible and affinity is not None
            else 0.0
        )
        variants["current_v1"] = _variant_value(
            candidate["score"],
            current_delta,
            confidence=1.0 if eligible else 0.0,
        )
        for variant_id, saturation in CURVE_SATURATION_EVIDENCE.items():
            confidence = min(1.0, evidence / saturation) if eligible else 0.0
            delta = _round_delta(
                _clamp(
                    direction * confidence * MAX_ABS_SCORE_DELTA,
                    -MAX_ABS_SCORE_DELTA,
                    MAX_ABS_SCORE_DELTA,
                )
            )
            variants[variant_id] = _variant_value(
                candidate["score"],
                delta,
                confidence=confidence,
            )
        rank = baseline_rank[candidate["id"]]
        previous_score = baseline[rank - 2]["score"] if rank > 1 else None
        scored.append({
            "id": candidate["id"],
            "source_type": candidate["source_type"],
            "input_rank": candidate["rank"],
            "baseline_rank": rank,
            "baseline_score": candidate["score"],
            "gap_to_previous": (
                round(previous_score - candidate["score"], 6)
                if previous_score is not None
                else None
            ),
            "gap_to_top": round(baseline_top_score - candidate["score"], 6),
            "positive_evidence_count": positive,
            "negative_evidence_count": negative,
            "total_evidence_count": evidence,
            "minimum_evidence": minimum,
            "affinity_preview": affinity,
            "direction": round(direction, 6),
            "variants": variants,
        })

    variant_results: dict[str, dict[str, Any]] = {}
    baseline_order = [item["id"] for item in scored]
    for variant_id in VARIANT_IDS:
        ranked = sorted(
            scored,
            key=lambda item: (
                -float(item["variants"][variant_id]["score"]),
                item["baseline_rank"],
                item["id"],
            ),
        )
        for rank, item in enumerate(ranked, start=1):
            item["variants"][variant_id]["rank"] = rank
        order = [item["id"] for item in ranked]
        variant_results[variant_id] = {
            "top1_candidate_id": ranked[0]["id"],
            "top1_source": ranked[0]["source_type"],
            "top1_score": ranked[0]["variants"][variant_id]["score"],
            "top1_changed": ranked[0]["id"] != baseline_order[0],
            "top3_changed": order[:3] != baseline_order[:3],
            "adjusted_candidate_count": sum(
                bool(item["variants"][variant_id]["score_delta"])
                for item in scored
            ),
        }

    return {
        "turn_id": str(observation.get("turn_id") or ""),
        "ts": _timestamp(observation),
        "baseline_top1_candidate_id": baseline[0]["id"],
        "baseline_top1_source": baseline[0]["source_type"],
        "baseline_top1_score": baseline[0]["score"],
        "candidate_count": len(scored),
        "candidate_scores": scored,
        "variant_results": variant_results,
    }


def _variant_value(score: float, delta: float, *, confidence: float) -> dict[str, Any]:
    return {
        "confidence": round(confidence, 6),
        "score_delta": delta,
        "score": round(score + delta, 6),
        "rank": 0,
        "at_cap": bool(delta) and abs(delta) >= MAX_ABS_SCORE_DELTA - 1e-9,
    }


def _baseline_impact(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    sources = Counter(str(row["baseline_top1_source"]) for row in rows)
    return {
        "top1_distribution": dict(sorted(sources.items())),
        "max_source_exposure_rate": _max_share(sources),
        "hhi": _hhi(sources),
    }


def _variant_impact(rows: list[Mapping[str, Any]], variant_id: str) -> dict[str, Any]:
    sources = Counter(
        str(row["variant_results"][variant_id]["top1_source"])
        for row in rows
    )
    candidates = [
        candidate
        for row in rows
        for candidate in row["candidate_scores"]
    ]
    adjusted = [
        candidate
        for candidate in candidates
        if candidate["variants"][variant_id]["score_delta"]
    ]
    eligible = [
        candidate
        for candidate in candidates
        if candidate["total_evidence_count"] >= candidate["minimum_evidence"]
        and candidate["affinity_preview"] is not None
    ]
    deltas = sorted(
        abs(float(candidate["variants"][variant_id]["score_delta"]))
        for candidate in adjusted
    )
    cap_count = sum(
        bool(candidate["variants"][variant_id]["at_cap"])
        for candidate in eligible
    )
    displacement = [
        abs(
            int(candidate["variants"][variant_id]["rank"])
            - int(candidate["baseline_rank"])
        )
        for candidate in candidates
    ]
    return {
        "eligible_candidate_count": len(eligible),
        "adjusted_candidate_count": len(adjusted),
        "top1_flip_count": sum(
            bool(row["variant_results"][variant_id]["top1_changed"])
            for row in rows
        ),
        "top3_reorder_count": sum(
            bool(row["variant_results"][variant_id]["top3_changed"])
            for row in rows
        ),
        "top1_distribution": dict(sorted(sources.items())),
        "max_source_exposure_rate": _max_share(sources),
        "hhi": _hhi(sources),
        "median_abs_score_delta": _quantile(deltas, 0.5),
        "p90_abs_score_delta": _quantile(deltas, 0.9),
        "max_abs_score_delta": max(deltas, default=0.0),
        "cap_count": cap_count,
        "cap_rate": _rate(cap_count, len(eligible)),
        "average_abs_rank_displacement": (
            round(sum(displacement) / len(displacement), 6)
            if displacement
            else 0.0
        ),
        "source_score_impact": _source_score_impact(candidates, variant_id),
    }


def _source_score_impact(
    candidates: list[Mapping[str, Any]],
    variant_id: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate["source_type"]), []).append(candidate)
    result: dict[str, dict[str, Any]] = {}
    for source, source_candidates in sorted(grouped.items()):
        deltas = sorted(
            abs(float(item["variants"][variant_id]["score_delta"]))
            for item in source_candidates
            if item["variants"][variant_id]["score_delta"]
        )
        count = len(source_candidates)
        result[source] = {
            "candidate_count": count,
            "eligible_candidate_count": sum(
                item["total_evidence_count"] >= item["minimum_evidence"]
                and item["affinity_preview"] is not None
                for item in source_candidates
            ),
            "adjusted_candidate_count": len(deltas),
            "average_baseline_score": round(
                sum(float(item["baseline_score"]) for item in source_candidates) / count,
                6,
            ),
            "average_variant_score": round(
                sum(float(item["variants"][variant_id]["score"]) for item in source_candidates)
                / count,
                6,
            ),
            "average_score_delta": round(
                sum(float(item["variants"][variant_id]["score_delta"]) for item in source_candidates)
                / count,
                6,
            ),
            "median_abs_score_delta": _quantile(deltas, 0.5),
            "p90_abs_score_delta": _quantile(deltas, 0.9),
            "max_abs_score_delta": max(deltas, default=0.0),
            "delta_at_least_001_count": sum(delta >= 0.01 - 1e-9 for delta in deltas),
            "delta_at_least_002_count": sum(delta >= 0.02 - 1e-9 for delta in deltas),
            "delta_at_least_003_count": sum(delta >= 0.03 - 1e-9 for delta in deltas),
            "average_abs_rank_displacement": round(
                sum(
                    abs(
                        int(item["variants"][variant_id]["rank"])
                        - int(item["baseline_rank"])
                    )
                    for item in source_candidates
                )
                / count,
                6,
            ),
        }
    return result


def _evidence_trajectories(
    observations: list[Mapping[str, Any]],
    *,
    cutoff: float,
    affinity_max: float,
    production_minimum: int,
) -> dict[str, list[dict[str, Any]]]:
    points: dict[str, dict[tuple[int, int, float], dict[str, Any]]] = {}
    seen_turns: set[tuple[str, str]] = set()
    for observation in sorted(observations, key=_timestamp):
        if _timestamp(observation) > cutoff:
            continue
        preview = observation.get("feedback_state_preview")
        if not isinstance(preview, Mapping) or preview.get("version") != PREVIEW_VERSION:
            continue
        if str(observation.get("recommendation_mode") or "").strip().lower() != "shadow":
            continue
        turn_id = str(observation.get("turn_id") or "").strip()
        turn_key = (str(observation.get("lanlan_name") or ""), turn_id)
        if not turn_id or turn_key in seen_turns:
            continue
        seen_turns.add(turn_key)
        persistent = ((preview.get("source_affinity") or {}).get("persistent") or {})
        snapshot_minimum = _nonnegative_int(persistent.get("min_explicit_evidence"))
        minimum = max(production_minimum, snapshot_minimum)
        sources = persistent.get("sources") if isinstance(persistent, Mapping) else None
        if not isinstance(sources, Mapping):
            continue
        for raw_source, bucket in sorted(sources.items()):
            if not isinstance(bucket, Mapping):
                continue
            source = str(raw_source)
            positive = _nonnegative_int(bucket.get("positive_evidence_count"))
            negative = _nonnegative_int(bucket.get("negative_evidence_count"))
            evidence = positive + negative
            affinity = _finite_optional(bucket.get("affinity_preview"))
            if affinity is None:
                continue
            key = (positive, negative, affinity)
            existing = points.setdefault(source, {}).get(key)
            if existing is not None:
                existing["observation_count"] += 1
                continue
            deltas = _trajectory_deltas(
                affinity=affinity,
                evidence=evidence,
                minimum=minimum,
                affinity_max=affinity_max,
            )
            points[source][key] = {
                "first_turn_id": turn_id,
                "first_ts": _timestamp(observation),
                "observation_count": 1,
                "positive_evidence_count": positive,
                "negative_evidence_count": negative,
                "total_evidence_count": evidence,
                "minimum_evidence": minimum,
                "affinity_preview": affinity,
                "direction": round(
                    _clamp(affinity / affinity_max, -1.0, 1.0),
                    6,
                ),
                "score_deltas": deltas,
            }
    return {
        source: sorted(
            source_points.values(),
            key=lambda item: (item["first_ts"], item["total_evidence_count"]),
        )
        for source, source_points in sorted(points.items())
    }


def _trajectory_deltas(
    *,
    affinity: float,
    evidence: int,
    minimum: int,
    affinity_max: float,
) -> dict[str, float]:
    if evidence < minimum:
        return {variant_id: 0.0 for variant_id in VARIANT_IDS}
    direction = _clamp(affinity / affinity_max, -1.0, 1.0)
    result = {
        "current_v1": _round_delta(
            _clamp(
                affinity * MAX_ABS_SCORE_DELTA,
                -MAX_ABS_SCORE_DELTA,
                MAX_ABS_SCORE_DELTA,
            )
        )
    }
    for variant_id, saturation in CURVE_SATURATION_EVIDENCE.items():
        result[variant_id] = _round_delta(
            _clamp(
                direction * min(1.0, evidence / saturation) * MAX_ABS_SCORE_DELTA,
                -MAX_ABS_SCORE_DELTA,
                MAX_ABS_SCORE_DELTA,
            )
        )
    return result


def _latest_source_evidence(
    trajectories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Mapping[str, Any]]:
    return {
        source: max(points, key=lambda item: float(item["first_ts"]))
        for source, points in trajectories.items()
        if points
    }


def _compare_with_r1(
    dataset: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    cutoff: float,
) -> dict[str, Any]:
    r1 = analyze_bounded_personalization(
        dataset,
        max_abs_delta=MAX_ABS_SCORE_DELTA,
        as_of=cutoff,
    )
    r1_rows = r1["rows"]
    mismatches: list[str] = []
    if len(r1_rows) != len(rows):
        mismatches.append("eligible_row_count")
    for index, (r1_row, row) in enumerate(zip(r1_rows, rows, strict=False)):
        if r1_row["turn_id"] != row["turn_id"]:
            mismatches.append(f"turn_order:{index}")
            continue
        if r1_row["baseline_top1_candidate_id"] != row["baseline_top1_candidate_id"]:
            mismatches.append(f"baseline_top1:{row['turn_id']}")
        r1_candidates = {item["id"]: item for item in r1_row["candidate_scores"]}
        for candidate in row["candidate_scores"]:
            old = r1_candidates.get(candidate["id"])
            current = candidate["variants"]["current_v1"]
            if old is None:
                mismatches.append(f"candidate_set:{row['turn_id']}")
                continue
            if (
                abs(float(old["score_delta"]) - float(current["score_delta"])) > 1e-9
                or abs(float(old["candidate_score"]) - float(current["score"])) > 1e-9
            ):
                mismatches.append(f"current_v1:{row['turn_id']}:{candidate['id']}")
    return {
        "matches": not mismatches,
        "r1_status": r1["conclusion"]["status"],
        "mismatches": sorted(set(mismatches)),
    }


def _hard_violations(
    rows: list[Mapping[str, Any]],
    *,
    production_minimum: int,
) -> list[str]:
    violations: set[str] = set()
    for row in rows:
        if row["candidate_count"] != len(row["candidate_scores"]):
            violations.add("candidate_set_changed")
        for candidate in row["candidate_scores"]:
            evidence = int(candidate["total_evidence_count"])
            minimum = max(production_minimum, int(candidate["minimum_evidence"]))
            affinity = candidate["affinity_preview"]
            for value in candidate["variants"].values():
                delta = float(value["score_delta"])
                score = float(value["score"])
                if not math.isfinite(delta) or abs(delta) > MAX_ABS_SCORE_DELTA + 1e-9:
                    violations.add("score_delta_out_of_bounds")
                if not math.isfinite(score):
                    violations.add("non_finite_candidate_score")
                if evidence < minimum and delta:
                    violations.add("below_minimum_evidence_adjusted")
                if not evidence and delta:
                    violations.add("no_evidence_adjusted")
                if affinity is not None and float(affinity) > 0 and delta < 0:
                    violations.add("positive_affinity_decreased_score")
                if affinity is not None and float(affinity) < 0 and delta > 0:
                    violations.add("negative_affinity_increased_score")
    return sorted(violations)


def _conclusion_blockers(
    *,
    hard_violations: list[str],
    negative_evidence: int,
    mechanical_pass: bool,
) -> list[str]:
    blockers = ["no_counterfactual_or_human_outcome_labels"]
    if hard_violations:
        blockers.append("hard_contract_violations")
    if not negative_evidence:
        blockers.append("no_negative_source_evidence")
    if not mechanical_pass:
        blockers.append("default_curve_mechanical_gates_not_met")
    return blockers


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


def _baseline_sort_key(item: Mapping[str, Any]) -> tuple[float, int, str]:
    return (-float(item["score"]), int(item["rank"]), str(item["id"]))


def _mapping_rows(value: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PersonalizationResponseCurveError(f"{field} must be a list")
    if any(not isinstance(row, Mapping) for row in value):
        raise PersonalizationResponseCurveError(f"{field} must contain objects")
    return list(value)


def _resolve_as_of(observations: list[Mapping[str, Any]], value: Any) -> float:
    if value is not None:
        cutoff = _finite_optional(value)
        if cutoff is None or cutoff < 0:
            raise PersonalizationResponseCurveError(
                "as_of must be a finite nonnegative number"
            )
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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_delta(value: float) -> float:
    return round(value, 6)


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    return round(
        ordered[lower] * (1 - fraction) + ordered[upper] * fraction,
        6,
    )


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


def _percent(value: Any) -> str:
    return f"{float(value or 0.0) * 100:.2f}%"


def _optional_number(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.3f}"


__all__ = [
    "ANALYSIS_ID",
    "CURVE_SATURATION_EVIDENCE",
    "DEFAULT_VARIANT_ID",
    "MAX_ABS_SCORE_DELTA",
    "PersonalizationResponseCurveError",
    "analyze_personalization_response_curves",
    "render_personalization_response_curves_markdown",
]
