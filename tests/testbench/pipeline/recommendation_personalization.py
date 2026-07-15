"""Isolated multi-round traces over production auto-safe recommendation tuning."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from tests.testbench.pipeline.recommendation_runner import preview_scenario
from tests.testbench.pipeline.recommendation_scenario import read_scenario

MAX_USERS = 10
MAX_ROUNDS = 20
MAX_RECORDS = 1000


class PersonalizationTraceError(ValueError):
    pass


def run_personalization_trace(spec: dict[str, Any]) -> dict[str, Any]:
    users = spec.get("users") or []
    if not isinstance(users, list) or not 1 <= len(users) <= MAX_USERS:
        raise PersonalizationTraceError("users must contain 1-10 entries")
    scenario = read_scenario(str(spec.get("scenario_id") or "competition_15"))
    seen: set[str] = set()
    traces = []
    for user in users:
        user_id = str(user.get("user_id") or "").strip()
        if not user_id or user_id in seen:
            raise PersonalizationTraceError("user_id must be non-empty and unique")
        seen.add(user_id)
        rounds = user.get("rounds") or []
        if not isinstance(rounds, list) or len(rounds) > MAX_ROUNDS:
            raise PersonalizationTraceError("each user supports at most 20 rounds")
        traces.append(_run_user(user_id, rounds, scenario))
    return {"schema_version": 1, "scenario_id": scenario["id"], "users": traces,
            "production_config_modified": False,
            "capability_scope": "isolated_testbench_simulation_over_production_auto_safe_tuning"}


def _run_user(user_id: str, rounds: list[dict[str, Any]], scenario: dict[str, Any]) -> dict[str, Any]:
    from main_logic.proactive_recommendation_feedback import sanitize_recommendation_feedback_event
    from main_logic.proactive_recommendation_observer import sanitize_recommendation_observation
    from main_logic.proactive_recommendation_tuning import maybe_auto_apply_recommendation_tuning_from_logs

    observations: list[dict[str, Any]] = []
    feedback: list[dict[str, Any]] = []
    trace = []
    with tempfile.TemporaryDirectory(prefix="neko-recommend-personalization-") as td:
        root = Path(td); obs_path = root / "observations.jsonl"; fb_path = root / "feedback.jsonl"
        tuning_path = root / "tuning.json"
        for index, item in enumerate(rounds, 1):
            observations.extend(sanitize_recommendation_observation(row) for row in item.get("observations") or [])
            feedback.extend(sanitize_recommendation_feedback_event(row) for row in item.get("feedback") or [])
            if len(observations) + len(feedback) > MAX_RECORDS:
                raise PersonalizationTraceError("maximum 1000 records per user")
            _write_jsonl(obs_path, observations); _write_jsonl(fb_path, feedback)
            timestamps = [float(row.get("ts") or 0) for row in observations + feedback]
            now = max(timestamps, default=float(index) * 3601.0) + 1.0
            applied = maybe_auto_apply_recommendation_tuning_from_logs(
                mode="auto_safe", tuning_path=tuning_path, observation_path=obs_path,
                feedback_path=fb_path, now=now)
            tuning = (applied.get("tuning") or {})
            adjustments = dict(tuning.get("source_type_adjustment") or {})
            ranking = preview_scenario(scenario, {"id": f"{user_id}-round-{index}",
                                                   "source_type_adjustments": adjustments})["snapshot"]
            trace.append({"round": index, "observation_count": len(observations),
                          "feedback_count": len(feedback), "applied": bool(applied.get("applied")),
                          "blocked_reason": None if applied.get("applied") else applied.get("reason"),
                          "applied_steps": applied.get("adjustments") or {},
                          "user_adjustments": adjustments,
                          "top1_source": ranking.get("top1_source_type"),
                          "resource_scores": {source: info.get("score")
                                              for source, info in (ranking.get("source_scores") or {}).items()}})
    return {"user_id": user_id, "rounds": trace,
            "final_adjustments": trace[-1]["user_adjustments"] if trace else {}}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
