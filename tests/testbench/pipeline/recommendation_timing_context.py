"""Causally recover local pre-decision dialogue for P44-F2-R0 review only."""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from hashlib import sha256
import json
from statistics import median
from typing import Any, Mapping


def recover_timing_blind_context(
    manifest: Mapping[str, Any],
    freeze: Mapping[str, Any],
    turns: list[Mapping[str, Any]],
    source_meta: Mapping[str, Any],
    *,
    max_messages: int = 10,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach only messages at or before each observation; omit their timestamps.

    The returned manifest remains blind to delivery, feedback, score and timing.
    The raw database is read by the caller through a read-only connection.
    """
    if not 1 <= max_messages <= 100:
        raise ValueError("max_messages must be between 1 and 100")
    observations = {
        str(row.get("turn_id") or ""): row
        for row in freeze.get("observations") or []
        if str(row.get("turn_id") or "")
    }
    ordered = sorted(turns, key=lambda row: (float(row["ts_epoch"]), str(row.get("message_id") or "")))
    timeline = [float(row["ts_epoch"]) for row in ordered]
    output = deepcopy(dict(manifest))
    gaps: list[float] = []
    no_context: list[str] = []
    for item in output.get("items") or []:
        turn_id = str(item.get("turn_id") or "")
        observation = observations.get(turn_id)
        if observation is None:
            raise ValueError(f"manifest turn_id missing from freeze: {turn_id}")
        observation_ts = float(observation["ts"])
        cutoff = bisect_right(timeline, observation_ts)
        selected = ordered[max(0, cutoff - max_messages):cutoff]
        if any(float(message["ts_epoch"]) > observation_ts for message in selected):
            raise AssertionError(f"causal boundary violation for {turn_id}")
        context = dict(item.get("context_for_blind_review") or {})
        if not selected:
            no_context.append(turn_id)
            context["pre_decision_context"] = {"available": False, "messages": []}
        else:
            gaps.append(observation_ts - float(selected[-1]["ts_epoch"]))
            context["pre_decision_context"] = {
                "available": True,
                "messages": [
                    {"role": str(message.get("role") or "other"), "content": str(message.get("content") or "")}
                    for message in selected
                ],
            }
        item["context_for_blind_review"] = context
    audit = {
        "method": "last_n_time_indexed_rows_at_or_before_observation",
        "max_messages": max_messages,
        "source_db_name": str(source_meta.get("source_db_name") or ""),
        "source_db_sha256": str(source_meta.get("source_db_sha256") or ""),
        "recovered_count": len(output.get("items") or []) - len(no_context),
        "no_context_count": len(no_context),
        "no_context_turn_ids": no_context,
        "causal_violation_count": 0,
        "latest_message_gap_seconds": {
            "min": round(min(gaps), 3) if gaps else None,
            "median": round(float(median(gaps)), 3) if gaps else None,
            "max": round(max(gaps), 3) if gaps else None,
        },
    }
    output["context_recovery_provenance"] = {
        "method": audit["method"],
        "max_messages": max_messages,
        "source_db_sha256": audit["source_db_sha256"],
        "recovered_count": audit["recovered_count"],
        "no_context_count": audit["no_context_count"],
        "causal_rule": "all exposed messages are at or before the observation",
        "reviewer_note": "message timestamps are intentionally omitted from the blind manifest",
    }
    return output, audit


def build_timing_context_recovery_markdown(audit: Mapping[str, Any]) -> str:
    gap = audit["latest_message_gap_seconds"]
    return "\n".join([
        "# P44-F2-R0 决策前对话上下文回填审计",
        "",
        f"- 回填：{audit['recovered_count']}",
        f"- 无上下文：{audit['no_context_count']}",
        f"- 因果违规：{audit['causal_violation_count']}",
        f"- 最近消息间隔：min={gap['min']}s / median={gap['median']}s / max={gap['max']}s",
        f"- 来源 DB SHA-256：`{audit['source_db_sha256']}`",
        "",
        "回填只暴露 observation 时点或之前的 user/assistant 内容；不包含消息时间戳、",
        "投递结果、生成文本、反馈、production score 或 timing 字段。",
        "",
    ])


def context_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "recover_timing_blind_context", "build_timing_context_recovery_markdown",
    "context_manifest_sha256",
]
