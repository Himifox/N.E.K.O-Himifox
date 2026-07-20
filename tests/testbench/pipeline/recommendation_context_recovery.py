"""Recover candidate-first review context from a time-indexed conversation DB.

This is retrospective reconstruction, not an exact replay of the historical
``/new_dialog`` response.  The archive stores raw turns while production may
have supplied a compressed ``recent.json`` window plus other memory state.

The causal boundary is strict: only rows whose timestamp is less than or equal
to the recommendation observation timestamp may enter candidate review.
Downstream delivery text is removed from the recovered candidate context.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

RECOVERY_SCHEMA_VERSION = 1
DEFAULT_MAX_MESSAGES = 10
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _normalize_role(raw_type: Any) -> str:
    value = str(raw_type or "").lower()
    if value in {"human", "user"}:
        return "user"
    if value in {"ai", "assistant"}:
        return "assistant"
    return value or "other"


def _coerce_content(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                parts.append(text if isinstance(text, str) else f"[{block.get('type') or 'block'}]")
            else:
                parts.append(str(block))
        return " ".join(part for part in parts if part)
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("content")
        if isinstance(text, str):
            return text
    return json.dumps(raw, ensure_ascii=False, default=str)


def _parse_message(message_raw: Any) -> tuple[str, str]:
    if isinstance(message_raw, (bytes, bytearray)):
        message_raw = message_raw.decode("utf-8")
    if isinstance(message_raw, str):
        try:
            message_raw = json.loads(message_raw)
        except json.JSONDecodeError:
            return "other", message_raw
    if isinstance(message_raw, dict):
        payload = message_raw.get("data")
        content = payload.get("content") if isinstance(payload, dict) else None
        return _normalize_role(message_raw.get("type")), _coerce_content(content)
    return "other", _coerce_content(message_raw)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp_parts(raw: Any, assumed_timezone: ZoneInfo) -> tuple[float, str]:
    if isinstance(raw, datetime):
        value = raw
    else:
        value = datetime.fromisoformat(str(raw))
    if value.tzinfo is None:
        value = value.replace(tzinfo=assumed_timezone)
    return value.timestamp(), value.isoformat()


def load_time_indexed_archive(
    db_path: Path,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read ``time_indexed_original`` with SQLite read-only mode."""
    source = db_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    assumed_timezone = ZoneInfo(timezone_name)
    uri = f"file:{source.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='time_indexed_original'"
        ).fetchone()
        if table is None:
            raise ValueError("time_indexed_original table is missing")
        raw_rows = connection.execute(
            "SELECT id, session_id, message, timestamp "
            "FROM time_indexed_original ORDER BY timestamp ASC, id ASC"
        ).fetchall()
    finally:
        connection.close()

    turns: list[dict[str, Any]] = []
    skipped = Counter()
    for row_id, session_id, message_raw, timestamp_raw in raw_rows:
        try:
            ts_epoch, ts_iso = _timestamp_parts(timestamp_raw, assumed_timezone)
        except (TypeError, ValueError):
            skipped["invalid_timestamp"] += 1
            continue
        role, content = _parse_message(message_raw)
        if not content.strip():
            skipped["empty_content"] += 1
            continue
        turns.append({
            "db_row_id": int(row_id),
            "message_id": f"tdb:{session_id or ''}:{row_id}",
            "session_id": str(session_id) if session_id is not None else None,
            "role": role,
            "timestamp": ts_iso,
            "ts_epoch": ts_epoch,
            "content": content,
        })
    turns.sort(key=lambda row: (float(row["ts_epoch"]), int(row["db_row_id"])))
    return turns, {
        "source_db_name": source.name,
        "source_db_sha256": _sha256_file(source),
        "source_row_count": len(raw_rows),
        "usable_row_count": len(turns),
        "skipped_row_distribution": dict(sorted(skipped.items())),
        "timezone_assumption": timezone_name,
    }


def _confidence_for_gap(gap_seconds: float | None) -> str:
    if gap_seconds is None:
        return "none"
    if gap_seconds <= 300:
        return "high"
    if gap_seconds <= 900:
        return "medium"
    return "low"


def recover_candidate_review_context(
    freeze: dict[str, Any],
    workbook: dict[str, Any],
    turns: list[dict[str, Any]],
    source_meta: dict[str, Any],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a new workbook with causal pre-decision context attached."""
    if max_messages < 1 or max_messages > 100:
        raise ValueError("max_messages must be between 1 and 100")
    observations = {
        str(row.get("turn_id") or ""): row
        for row in freeze.get("observations") or []
        if str(row.get("turn_id") or "")
    }
    annotations = list(workbook.get("annotations") or [])
    if not observations:
        raise ValueError("freeze contains no observations")
    missing = [
        str(row.get("turn_id") or "")
        for row in annotations
        if str(row.get("turn_id") or "") not in observations
    ]
    if missing:
        raise ValueError(f"annotations missing from freeze: {missing[:5]}")

    timeline = [float(row["ts_epoch"]) for row in turns]
    recovered = deepcopy(workbook)
    recovered_annotations: list[dict[str, Any]] = []
    confidence_distribution = Counter()
    message_count_distribution = Counter()
    gaps: list[float] = []
    no_context_ids: list[str] = []

    downstream_keys = ("delivered", "reason", "delivered_excerpt")
    for original in annotations:
        annotation = deepcopy(original)
        turn_id = str(annotation.get("turn_id") or "")
        observation = observations[turn_id]
        observation_ts = float(observation.get("ts"))
        cutoff = bisect_right(timeline, observation_ts)
        selected = deepcopy(turns[max(0, cutoff - max_messages):cutoff])
        for message in selected:
            if float(message["ts_epoch"]) > observation_ts:
                raise AssertionError(
                    f"causal leak for {turn_id}: {message['message_id']}"
                )
        latest_gap = (
            round(observation_ts - float(selected[-1]["ts_epoch"]), 3)
            if selected else None
        )
        if latest_gap is not None:
            gaps.append(latest_gap)
        confidence = _confidence_for_gap(latest_gap)
        confidence_distribution[confidence] += 1
        message_count_distribution[len(selected)] += 1
        if not selected:
            no_context_ids.append(turn_id)

        legacy_context = dict(annotation.get("context_for_review") or {})
        downstream = {
            key: legacy_context.pop(key)
            for key in downstream_keys
            if key in legacy_context
        }
        legacy_context["pre_decision_context"] = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "method": "last_n_time_indexed_rows_before_observation",
            "fidelity": "retrospective_proxy",
            "observation_ts": observation_ts,
            "max_messages": max_messages,
            "message_count": len(selected),
            "latest_message_gap_seconds": latest_gap,
            "temporal_confidence": confidence,
            "messages": selected,
        }
        annotation["context_for_review"] = legacy_context
        annotation["realization_review_context"] = downstream
        recovered_annotations.append(annotation)

    recovered["kind"] = "recommendation_human_review_context_recovered"
    recovered["context_recovery"] = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "method": "last_n_time_indexed_rows_before_observation",
        "fidelity": "retrospective_proxy_not_exact_prompt_replay",
        "causal_rule": "message.ts_epoch <= observation.ts",
        "max_messages": max_messages,
        **source_meta,
        "annotation_count": len(recovered_annotations),
        "recovered_count": len(recovered_annotations) - len(no_context_ids),
        "no_context_count": len(no_context_ids),
        "no_context_turn_ids": no_context_ids,
        "temporal_confidence_distribution": dict(
            sorted(confidence_distribution.items())
        ),
        "message_count_distribution": {
            str(key): value
            for key, value in sorted(message_count_distribution.items())
        },
        "latest_message_gap_seconds": {
            "min": round(min(gaps), 3) if gaps else None,
            "median": round(float(median(gaps)), 3) if gaps else None,
            "max": round(max(gaps), 3) if gaps else None,
        },
        "limitations": [
            "time_indexed.db stores raw turns, not the historical compressed recent.json snapshot",
            "historical persona, reflection, open-thread, and proactive-history prompt state is unavailable",
            "realization_review_context is downstream and must not be used for candidate relevance",
        ],
    }
    instructions = dict(recovered.get("instructions") or {})
    instructions.update({
        "candidate_review_context": (
            "use context_for_review.pre_decision_context plus candidate metadata"
        ),
        "realization_review_context": (
            "downstream-only; excluded from candidate relevance and source ranking"
        ),
        "causal_hard_gate": "every recovered message timestamp must be <= observation.ts",
    })
    recovered["instructions"] = instructions
    recovered["annotations"] = recovered_annotations
    summary = {
        "annotation_count": len(recovered_annotations),
        "recovered_count": len(recovered_annotations) - len(no_context_ids),
        "no_context_count": len(no_context_ids),
        "temporal_confidence_distribution": dict(
            sorted(confidence_distribution.items())
        ),
        "latest_message_gap_seconds": recovered["context_recovery"][
            "latest_message_gap_seconds"
        ],
        "causal_violation_count": 0,
    }
    return recovered, summary


def build_context_recovery_audit_markdown(
    summary: dict[str, Any],
    source_meta: dict[str, Any],
) -> str:
    confidence = summary["temporal_confidence_distribution"]
    gaps = summary["latest_message_gap_seconds"]
    return "\n".join([
        "# P44-E 对话上下文回填审计",
        "",
        "本报告只统计恢复质量，不展示对话正文。",
        "",
        f"- Annotation：{summary['annotation_count']}",
        f"- 已恢复：{summary['recovered_count']}",
        f"- 无历史上下文：{summary['no_context_count']}",
        f"- 因果违规：{summary['causal_violation_count']}",
        f"- 高时序置信度（≤5分钟）：{confidence.get('high', 0)}",
        f"- 中时序置信度（5–15分钟）：{confidence.get('medium', 0)}",
        f"- 低时序置信度（>15分钟）：{confidence.get('low', 0)}",
        f"- 最近消息间隔：min={gaps['min']}s / median={gaps['median']}s / max={gaps['max']}s",
        f"- 来源数据库：{source_meta['source_db_name']}",
        f"- 数据库 SHA-256：`{source_meta['source_db_sha256']}`",
        "",
        "## 使用边界",
        "",
        "- 回填消息全部早于或等于 observation 时间。",
        "- 这是原始历史的回溯代理，不是当时 `/new_dialog` prompt 的逐字重放。",
        "- 候选评审不得读取 `realization_review_context`。",
        "",
    ])
