"""P44-G1 read-only acceptance analysis for proactive-chat encounters."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from tests.testbench.pipeline.recommendation_adapter import (
    run_reward_score_v2_preview,
)


FEEDBACK_STATE_VERSION = "feedback_state_preview_v1"
ENCOUNTER_MODES = ("chat", "music")
CONVERSATION_COMPONENTS = ("reply", "continue", "relative_speed", "interrupt", "settings")
MIN_PREVIEW_OBSERVATIONS = 50
MIN_DELIVERED_ENCOUNTERS = 15
MIN_MODE_EXPLICIT_REWARDS = 8
MIN_COMPARISON_BIN = 4


def analyze_encounter_acceptance(
    dataset: Mapping[str, Any],
    *,
    as_of: float | None = None,
) -> dict[str, Any]:
    """Describe chat/music encounter acceptance from a frozen Shadow dataset."""
    observations = _mappings(dataset.get("observations"))
    feedback = _mappings(dataset.get("feedback"))
    requested_as_of = as_of if as_of is not None else dataset.get("as_of")
    cutoff = _resolve_as_of(observations, feedback, requested_as_of)
    input_hash = _hash({"observations": observations, "feedback": feedback, "as_of": cutoff})

    issues: Counter[str] = Counter()
    material_coverage: Counter[str] = Counter()
    channel_coverage: Counter[str] = Counter()
    preview_observations: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    seen_turns: set[tuple[str, str]] = set()

    for raw in sorted(observations, key=_timestamp):
        row = dict(raw)
        if _timestamp(row) > cutoff:
            issues["observation_after_as_of"] += 1
            continue
        preview = row.get("feedback_state_preview")
        if not isinstance(preview, Mapping) or preview.get("version") != FEEDBACK_STATE_VERSION:
            issues["missing_or_invalid_feedback_state_preview"] += 1
            continue
        if str(row.get("recommendation_mode") or "").strip().lower() != "shadow":
            issues["not_shadow"] += 1
            continue
        key = _turn_key(row)
        if not all(key):
            issues["invalid_turn_id"] += 1
            continue
        if key in seen_turns:
            issues["duplicate_turn_id"] += 1
            continue
        seen_turns.add(key)
        preview_observations.append(row)
        material_coverage[_clean(row.get("shadow_selected_source_type")) or "unknown"] += 1

        if row.get("delivered") is not True:
            issues["not_delivered"] += 1
            continue
        channel = _clean(row.get("actual_primary_channel")) or "unknown"
        channel_coverage[channel] += 1
        if channel not in ENCOUNTER_MODES:
            issues["unsupported_actual_primary_channel"] += 1
            continue
        eligible.append(row)

    eligible_by_turn = {_turn_key(row): row for row in eligible}
    bounded_feedback: list[dict[str, Any]] = []
    for raw in feedback:
        event = dict(raw)
        key = _turn_key(event)
        event_ts = _optional_number(event.get("ts"))
        if event_ts is None:
            issues["feedback_invalid_ts"] += 1
            continue
        if event_ts > cutoff:
            issues["feedback_after_as_of"] += 1
            continue
        observation = eligible_by_turn.get(key)
        if observation is None:
            issues["feedback_outside_eligible_encounter"] += 1
            continue
        if event_ts < _timestamp(observation):
            issues["feedback_before_observation"] += 1
            continue
        bounded_feedback.append(event)

    window_seconds = max(1, int(math.ceil(cutoff - min((_timestamp(row) for row in eligible), default=cutoff))) + 1)
    production = run_reward_score_v2_preview(
        eligible,
        bounded_feedback,
        now=cutoff,
        window_seconds=window_seconds,
        sample_limit=max(1, len(eligible)),
    )
    joined_by_turn = {
        (str(row.get("lanlan_name") or ""), str(row.get("turn_id") or "")): row
        for row in production["joined"]
    }

    rows_by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in eligible:
        key = _turn_key(observation)
        joined = dict(joined_by_turn.get(key) or {})
        joined["observation_ts"] = _timestamp(observation)
        joined["encounter_mode"] = _clean(observation.get("actual_primary_channel"))
        joined["material_source"] = (
            "music"
            if joined["encounter_mode"] == "music"
            else (_clean(observation.get("shadow_selected_source_type")) or "chat")
        )
        joined["material_verified"] = bool(
            joined["encounter_mode"] == "music"
            or observation.get("matched_actual_source") is True
        )
        joined["state"] = _state_snapshot(observation, joined["material_source"])
        joined["technical_only"] = _technical_only(joined)
        joined["unknown_only"] = _unknown_only(joined)
        rows_by_mode[joined["encounter_mode"]].append(joined)

    mode_reports = {
        mode: _summarize_mode(rows_by_mode.get(mode, []))
        for mode in ENCOUNTER_MODES
    }
    all_rows = [row for rows in rows_by_mode.values() for row in rows]
    material_reports = {
        source: _summarize_material(rows)
        for source, rows in sorted(_group_by(all_rows, "material_source").items())
    }
    explicit_rows = [
        row
        for row in all_rows
        if _is_explicit_reward(row)
    ]
    conversation_rows = [row for row in explicit_rows if _has_conversation_feedback(row)]
    positive_count = sum(_conversation_reward(row) > 0 for row in conversation_rows)
    negative_count = sum(_conversation_reward(row) < 0 for row in conversation_rows)
    shared_state_complete = not rows_by_mode.get("music")
    candidate_signals = {
        mode: _candidate_signal(rows_by_mode.get(mode, []))
        for mode in ENCOUNTER_MODES
    }

    blockers: list[str] = []
    descriptive_ready = (
        len(preview_observations) >= MIN_PREVIEW_OBSERVATIONS
        and len(eligible) >= MIN_DELIVERED_ENCOUNTERS
    )
    if len(preview_observations) < MIN_PREVIEW_OBSERVATIONS:
        blockers.append("preview_observations_below_50")
    if len(eligible) < MIN_DELIVERED_ENCOUNTERS:
        blockers.append("delivered_encounters_below_15")
    if positive_count == 0:
        blockers.append("no_explicit_positive_reward")
    if negative_count == 0:
        blockers.append("no_explicit_negative_reward")
    if not shared_state_complete:
        blockers.append("preview_v1_does_not_separate_shared_chat_from_music_resource_state")
    for mode in ENCOUNTER_MODES:
        if mode_reports[mode]["conversation_feedback_count"] < MIN_MODE_EXPLICIT_REWARDS:
            blockers.append(f"{mode}_conversation_feedback_below_8")

    stable_signal = any(signal["stable"] for signal in candidate_signals.values())
    if not stable_signal:
        blockers.append("no_time_stable_predecision_state_relationship")
    if not descriptive_ready:
        status = "insufficient_evidence"
    elif positive_count and negative_count and stable_signal and shared_state_complete:
        status = "candidate_for_scheduler_shadow_design"
    else:
        status = "descriptive_only"

    return {
        "schema_version": 1,
        "analysis": "p44_g1_encounter_acceptance",
        "input": {
            "as_of": cutoff,
            "sha256": input_hash,
            "observation_count": len(observations),
            "feedback_event_count": len(feedback),
            "preview_observation_count": len(preview_observations),
            "eligible_delivered_encounter_count": len(eligible),
        },
        "scope": {
            "encounter_unit": True,
            "reward_contract": production["summary"].get("version"),
            "conversation_feedback_shared_by_all_encounters": True,
            "music_resource_feedback_is_additive": True,
            "material_breakdown_does_not_update_long_term_source_preference": True,
            "unverified_shadow_material_is_not_causal_attribution": True,
            "shared_conversation_state_contract_complete": shared_state_complete,
            "production_weights_modified": False,
            "ranking_consumed": False,
            "tuning_consumed": False,
            "inferred_ignored_in_explicit_metrics": False,
        },
        "coverage": {
            "actual_primary_channel": dict(sorted(channel_coverage.items())),
            "shadow_material_source": dict(sorted(material_coverage.items())),
        },
        "data_issues": {
            "count": sum(issues.values()),
            "distribution": dict(sorted(issues.items())),
        },
        "production_reward_summary": production["summary"],
        "all_encounters": _summarize_material(all_rows),
        "encounters": mode_reports,
        "material_sources": material_reports,
        "candidate_signals": candidate_signals,
        "conclusion": {
            "status": status,
            "blockers": sorted(set(blockers)),
            "explicit_positive_conversation_count": positive_count,
            "explicit_negative_conversation_count": negative_count,
            "next_owner": (
                "scheduler_or_routing_shadow_review"
                if status == "candidate_for_scheduler_shadow_design"
                else None
            ),
            "weight_candidate_generated": False,
        },
    }


def render_encounter_acceptance_markdown(report: Mapping[str, Any]) -> str:
    """Render the stable human-readable G1 report."""
    input_data = report["input"]
    conclusion = report["conclusion"]
    lines = [
        "# P44-G1 主动搭话接受度离线分析",
        "",
        f"- 状态：`{conclusion['status']}`",
        f"- Preview observation：{input_data['preview_observation_count']} / 50",
        f"- 实际投递 encounter：{input_data['eligible_delivered_encounter_count']} / 15",
        f"- 输入 SHA-256：`{input_data['sha256']}`",
        "- 每次主动搭话都共享回复、继续对话、拒绝等聊天反馈；music 另加播放行为反馈。",
        "- news、meme、vision 等素材按来源展示接受度，但结果不自动升级为长期来源偏好。",
        "- 本报告未修改生产权重、排序、投递、scheduler 或 tuning。",
        "",
        "## 投递通道",
        "",
        "| 通道 | 投递 | 共同聊天反馈 | 平均聊天反馈 | 资源行为反馈 | 平均组合reward | inferred ignored | 归因问题 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in ENCOUNTER_MODES:
        item = report["encounters"][mode]
        lines.append(
            f"| `{mode}` | {item['encounter_count']} | {item['conversation_feedback_count']} | "
            f"{_display(item['average_conversation_reward'])} | {item['resource_feedback_count']} | "
            f"{_display(item['average_reward'])} | {item['inferred_ignored_count']} | "
            f"{item['attribution_issue_count']} |"
        )
    lines.extend([
        "",
        "## Shadow 候选素材（非偏好归因）",
        "",
        "| 素材 | encounter | 已验证实际匹配 | 共同聊天反馈 | 平均聊天反馈 | 资源行为反馈 | 平均组合reward |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for source, item in report["material_sources"].items():
        lines.append(
            f"| `{source}` | {item['encounter_count']} | {item['verified_material_count']} | "
            f"{item['conversation_feedback_count']} | "
            f"{_display(item['average_conversation_reward'])} | {item['resource_feedback_count']} | "
            f"{_display(item['average_combined_reward'])} |"
        )
    lines.extend(["", "## 共同聊天接受度的决策时 preview 分桶", ""])
    for mode in ENCOUNTER_MODES:
        item = report["encounters"][mode]
        lines.extend([
            f"### {mode}",
            "",
            "| 层级/分桶 | encounter | 显式reward | 平均reward |",
            "|---|---:|---:|---:|",
        ])
        for layer in ("temporary", "persistent"):
            for bucket, metric in item["state_bins"][layer].items():
                lines.append(
                    f"| `{layer}/{bucket}` | {metric['encounter_count']} | "
                    f"{metric['explicit_reward_count']} | {_display(metric['average_reward'])} |"
                )
        split = item["chronological_split"]
        lines.extend([
            "",
            f"时间前/后半平均 reward：{_display(split['early']['average_reward'])} / "
            f"{_display(split['late']['average_reward'])}。",
            "",
        ])
    lines.extend([
        "## 结论",
        "",
        f"- 显式正/负聊天反馈：{conclusion['explicit_positive_conversation_count']} / "
        f"{conclusion['explicit_negative_conversation_count']}。",
        "- 阻塞项：" + (", ".join(f"`{item}`" for item in conclusion["blockers"]) or "无"),
        "- 本阶段不生成来源权重或重排候选；若状态达到候选，也只进入 scheduler/routing Shadow 设计评审。",
        "",
    ])
    return "\n".join(lines)


def _summarize_mode(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    explicit = [row for row in rows if _is_explicit_reward(row)]
    rewards = [_reward(row) for row in explicit]
    components: dict[str, list[float]] = defaultdict(list)
    for row in explicit:
        raw = row.get("reward_components_v2_preview")
        if isinstance(raw, Mapping):
            for name, value in raw.items():
                number = _optional_number(value)
                if number is not None:
                    components[str(name)].append(number)
    attribution = Counter(
        str(row.get("attribution_issue"))
        for row in rows
        if row.get("attribution_issue")
    )
    conversation = [row for row in explicit if _has_conversation_feedback(row)]
    ordered = sorted(conversation, key=lambda row: float(row.get("observation_ts") or 0.0))
    midpoint = (len(ordered) + 1) // 2
    return {
        "encounter_count": len(rows),
        "verified_material_count": sum(row.get("material_verified") is True for row in rows),
        "explicit_reward_count": len(explicit),
        "positive_count": sum(value > 0 for value in rewards),
        "negative_count": sum(value < 0 for value in rewards),
        "neutral_count": sum(value == 0 for value in rewards),
        "average_reward": _average(rewards),
        "conversation_feedback_count": sum(_has_conversation_feedback(row) for row in explicit),
        "average_conversation_reward": _average([
            _conversation_reward(row) for row in explicit if _has_conversation_feedback(row)
        ]),
        "resource_feedback_count": sum(_has_resource_feedback(row) for row in explicit),
        "inferred_ignored_count": sum(row.get("feedback_inferred") is True for row in rows),
        "technical_event_count": sum(len(row.get("technical_zero_event_types") or []) for row in rows),
        "technical_only_turn_count": sum(row.get("technical_only") is True for row in rows),
        "unknown_event_count": sum(len(row.get("unknown_event_types") or []) for row in rows),
        "attribution_issue_count": sum(attribution.values()),
        "attribution_issue_distribution": dict(sorted(attribution.items())),
        "average_components": {
            name: _average(values) for name, values in sorted(components.items())
        },
        "state_bins": {
            layer: _state_bins(rows, layer) for layer in ("temporary", "persistent")
        },
        "chronological_split": {
            "early": _split_summary(ordered[:midpoint]),
            "late": _split_summary(ordered[midpoint:]),
        },
        "bin_comparison_enabled": len(conversation) >= MIN_MODE_EXPLICIT_REWARDS,
    }


def _summarize_material(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    explicit = [row for row in rows if _is_explicit_reward(row)]
    conversation = [row for row in explicit if _has_conversation_feedback(row)]
    resource = [row for row in explicit if _has_resource_feedback(row)]
    return {
        "encounter_count": len(rows),
        "verified_material_count": sum(
            row.get("material_verified") is True for row in rows
        ),
        "explicit_reward_count": len(explicit),
        "conversation_feedback_count": len(conversation),
        "average_conversation_reward": _average([_conversation_reward(row) for row in conversation]),
        "resource_feedback_count": len(resource),
        "average_resource_reward": _average([_resource_reward(row) for row in resource]),
        "average_combined_reward": _average([_reward(row) for row in explicit]),
    }


def _state_bins(rows: Sequence[Mapping[str, Any]], layer: str) -> dict[str, Any]:
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        state = row.get("state")
        bucket = str((state.get(layer) if isinstance(state, Mapping) else None) or "absent")
        buckets[bucket].append(row)
    result: dict[str, Any] = {}
    for bucket, items in sorted(buckets.items()):
        explicit = [
            row for row in items
            if _is_explicit_reward(row) and _has_conversation_feedback(row)
        ]
        result[bucket] = {
            "encounter_count": len(items),
            "explicit_reward_count": len(explicit),
            "average_reward": _average([_conversation_reward(row) for row in explicit]),
        }
    return result


def _candidate_signal(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    explicit = sorted(
        (
            row for row in rows
            if _is_explicit_reward(row) and _has_conversation_feedback(row)
        ),
        key=lambda row: float(row.get("observation_ts") or 0.0),
    )
    with_value = [
        row for row in explicit
        if _optional_number((row.get("state") or {}).get("temporary_value")) is not None
    ]
    positive = [row for row in with_value if float((row.get("state") or {})["temporary_value"]) > 0]
    nonpositive = [row for row in with_value if float((row.get("state") or {})["temporary_value"]) <= 0]
    full_delta = _delta(positive, nonpositive)
    midpoint = (len(with_value) + 1) // 2
    early_delta = _partition_delta(with_value[:midpoint])
    late_delta = _partition_delta(with_value[midpoint:])
    enough = len(positive) >= MIN_COMPARISON_BIN and len(nonpositive) >= MIN_COMPARISON_BIN
    stable = bool(
        enough
        and full_delta not in (None, 0)
        and early_delta not in (None, 0)
        and late_delta not in (None, 0)
        and math.copysign(1, full_delta) == math.copysign(1, early_delta)
        and math.copysign(1, full_delta) == math.copysign(1, late_delta)
    )
    return {
        "stable": stable,
        "positive_state_count": len(positive),
        "nonpositive_state_count": len(nonpositive),
        "mean_reward_delta_positive_minus_nonpositive": full_delta,
        "early_delta": early_delta,
        "late_delta": late_delta,
        "minimum_per_comparison_bin": MIN_COMPARISON_BIN,
    }


def _state_snapshot(observation: Mapping[str, Any], material_source: str) -> dict[str, Any]:
    preview = observation.get("feedback_state_preview")
    temporary = preview.get("temporary") if isinstance(preview, Mapping) else None
    persistent = preview.get("persistent") if isinstance(preview, Mapping) else None
    temp_sources = temporary.get("sources") if isinstance(temporary, Mapping) else None
    persistent_sources = persistent.get("sources") if isinstance(persistent, Mapping) else None
    # Chat acceptance is common to every proactive encounter.  Music/material
    # state remains separately visible and never replaces the shared chat layer.
    temp_bucket = temp_sources.get("chat") if isinstance(temp_sources, Mapping) else None
    persistent_bucket = persistent_sources.get("chat") if isinstance(persistent_sources, Mapping) else None
    temp_value = _optional_number(temp_bucket.get("interest_preview")) if isinstance(temp_bucket, Mapping) else None
    affinity = _optional_number(persistent_bucket.get("affinity_preview")) if isinstance(persistent_bucket, Mapping) else None
    minimum = int(_optional_number(persistent.get("min_explicit_evidence")) or 0) if isinstance(persistent, Mapping) else 0
    evidence = 0
    if isinstance(persistent_bucket, Mapping):
        evidence = int(_optional_number(persistent_bucket.get("positive_evidence_count")) or 0) + int(
            _optional_number(persistent_bucket.get("negative_evidence_count")) or 0
        )
    resource_temp = temp_sources.get(material_source) if isinstance(temp_sources, Mapping) else None
    resource_persistent = persistent_sources.get(material_source) if isinstance(persistent_sources, Mapping) else None
    return {
        "temporary": _sign_bucket(temp_value, absent="absent"),
        "temporary_value": temp_value,
        "persistent": "cold" if evidence < minimum else _sign_bucket(affinity, absent="neutral"),
        "persistent_value": affinity,
        "resource_temporary_value": (
            _optional_number(resource_temp.get("interest_preview"))
            if isinstance(resource_temp, Mapping) else None
        ),
        "resource_persistent_value": (
            _optional_number(resource_persistent.get("affinity_preview"))
            if isinstance(resource_persistent, Mapping) else None
        ),
    }


def _is_explicit_reward(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("attribution_valid") is True
        and row.get("feedback_inferred") is not True
        and row.get("technical_only") is not True
        and row.get("unknown_only") is not True
        and _optional_number(row.get("reward_score_v2_preview")) is not None
    )


def _technical_only(row: Mapping[str, Any]) -> bool:
    events = set(row.get("feedback_event_types") or [])
    technical = set(row.get("technical_zero_event_types") or [])
    unknown = set(row.get("unknown_event_types") or [])
    return bool(technical and not (events - technical - unknown))


def _unknown_only(row: Mapping[str, Any]) -> bool:
    events = set(row.get("feedback_event_types") or [])
    unknown = set(row.get("unknown_event_types") or [])
    technical = set(row.get("technical_zero_event_types") or [])
    return bool(unknown and not (events - technical - unknown))


def _partition_delta(rows: Sequence[Mapping[str, Any]]) -> float | None:
    positive = [row for row in rows if float((row.get("state") or {}).get("temporary_value") or 0.0) > 0]
    nonpositive = [row for row in rows if float((row.get("state") or {}).get("temporary_value") or 0.0) <= 0]
    if len(positive) < 2 or len(nonpositive) < 2:
        return None
    return _delta(positive, nonpositive)


def _delta(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> float | None:
    if not left or not right:
        return None
    return round(
        mean(_conversation_reward(row) for row in left)
        - mean(_conversation_reward(row) for row in right),
        6,
    )


def _split_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rewards = [_conversation_reward(row) for row in rows]
    return {"count": len(rows), "average_reward": _average(rewards)}


def _reward(row: Mapping[str, Any]) -> float:
    return float(row.get("reward_score_v2_preview") or 0.0)


def _conversation_reward(row: Mapping[str, Any]) -> float:
    components = row.get("reward_components_v2_preview")
    if not isinstance(components, Mapping):
        return 0.0
    return round(sum(float(components.get(name) or 0.0) for name in CONVERSATION_COMPONENTS), 6)


def _resource_reward(row: Mapping[str, Any]) -> float:
    components = row.get("reward_components_v2_preview")
    if not isinstance(components, Mapping):
        return 0.0
    return round(float(components.get("consumption") or 0.0), 6)


def _has_conversation_feedback(row: Mapping[str, Any]) -> bool:
    return _conversation_reward(row) != 0.0


def _has_resource_feedback(row: Mapping[str, Any]) -> bool:
    return row.get("encounter_mode") == "music" and _resource_reward(row) != 0.0


def _sign_bucket(value: float | None, *, absent: str) -> str:
    if value is None:
        return absent
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _group_by(
    rows: Sequence[dict[str, Any]],
    field: str,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(field) or "unknown")].append(row)
    return grouped


def _turn_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (str(row.get("lanlan_name") or "").strip(), str(row.get("turn_id") or "").strip())


def _timestamp(row: Mapping[str, Any]) -> float:
    return _optional_number(row.get("ts")) or 0.0


def _resolve_as_of(
    observations: Sequence[Mapping[str, Any]],
    feedback: Sequence[Mapping[str, Any]],
    value: Any,
) -> float:
    explicit = _optional_number(value)
    if explicit is not None:
        return explicit
    timestamps = [_timestamp(row) for row in (*observations, *feedback)]
    return max(timestamps, default=0.0)


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _average(values: Sequence[float]) -> float | None:
    return round(mean(values), 6) if values else None


def _display(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["analyze_encounter_acceptance", "render_encounter_acceptance_markdown"]
