"""Thin, deterministic adapter over production proactive recommendation helpers."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

RECOMMENDATION_ADAPTER_VERSION = "1"


def _production():
    from main_logic import proactive_recommendation as recommendation
    return recommendation


def _context(scenario: dict[str, Any], variant: dict[str, Any]):
    mod = _production()
    raw = dict(scenario.get("context") or {})
    weights = dict(raw.get("source_weights") or {})
    weights.update(variant.get("source_weights") or {})
    adjustments = dict(raw.get("source_type_adjustments") or {})
    adjustments.update(variant.get("source_type_adjustments") or {})
    raw["source_weights"] = weights
    raw["source_type_adjustments"] = adjustments
    allowed = mod.ProactiveRecommendationContext.__dataclass_fields__
    return mod.ProactiveRecommendationContext(**{k: v for k, v in raw.items() if k in allowed})


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_GIT_REVISION: str | None = None


def _git_revision() -> str:
    global _GIT_REVISION
    if _GIT_REVISION is not None:
        return _GIT_REVISION
    try:
        root = Path(__file__).resolve().parents[3]
        _GIT_REVISION = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=2).strip()
    except Exception:
        _GIT_REVISION = "unknown"
    return _GIT_REVISION


def _snapshot(decision: Any, scenario: dict[str, Any], variant: dict[str, Any], *, bias: Any = None,
              before_topics: list[Any] | None = None, after_topics: list[Any] | None = None) -> dict[str, Any]:
    ranked = [candidate.to_log_dict() for candidate in decision.ranked_candidates]
    source_scores: dict[str, dict[str, Any]] = {}
    for candidate in ranked:
        source = str(candidate.get("source_type") or "unknown")
        bucket = source_scores.setdefault(source, {"score": None, "candidate_count": 0, "candidates": []})
        score = float(candidate.get("score", 0.0))
        bucket["candidate_count"] += 1
        bucket["score"] = score if bucket["score"] is None else max(float(bucket["score"]), score)
        bucket["candidates"].append({
            "id": candidate.get("id"), "topic": candidate.get("topic"),
            "score": score, "quality": candidate.get("quality"), "freshness": candidate.get("freshness"),
        })
    source_scores = dict(sorted(source_scores.items(), key=lambda item: float(item[1]["score"]), reverse=True))
    scores = [float(item.get("score", 0.0)) for item in ranked]
    non_finite = [item.get("id") for item in ranked if not math.isfinite(float(item.get("score", 0.0)))]
    gap = round(scores[0] - scores[1], 6) if len(scores) > 1 else None
    return {
        "stage": decision.decision_stage,
        "candidate_count": decision.candidate_count,
        "filtered_reasons": dict(decision.filtered_reasons),
        "ranked_candidates": ranked,
        "score_breakdown": decision.score_breakdown,
        "source_scores": source_scores,
        "top1_candidate_id": ranked[0]["id"] if ranked else None,
        "top1_source_type": ranked[0]["source_type"] if ranked else None,
        "top1_score": scores[0] if scores else None,
        "score_gap": gap,
        "active_bias": bias.to_log_dict() if bias is not None else None,
        "phase1_topics_before": before_topics,
        "phase1_topics_after": after_topics,
        "input_hash": _hash({"scenario": scenario, "variant": variant}),
        "variant_hash": _hash(variant),
        "git_revision": _git_revision(),
        "diagnostics": {"non_finite_candidate_ids": non_finite},
    }


def run_source_stage(scenario: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    mod = _production()
    decision = mod.build_shadow_recommendation_decision(_context(scenario, variant), (scenario.get("inputs") or {}).get("sources") or {})
    return _snapshot(decision, scenario, variant)


def run_material_stage(scenario: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    mod = _production()
    inputs = scenario.get("inputs") or {}
    topics = list(inputs.get("phase1_topics") or [])
    decision = mod.build_phase1_material_shadow_decision(
        _context(scenario, variant), phase1_topics=topics,
        selected_web_link=inputs.get("selected_web_link"), selected_music_link=inputs.get("selected_music_link"),
        selected_meme_link=inputs.get("selected_meme_link"), vision_content=inputs.get("vision_content"),
        active_channels=inputs.get("active_channels") or (),
    )
    gap = float(variant.get("active_min_score_gap", 0.05))
    bias = mod.build_active_source_bias(decision, min_score_gap=gap)
    reordered = mod.reorder_phase1_topics_for_bias(topics, bias)
    return _snapshot(decision, scenario, variant, bias=bias, before_topics=topics, after_topics=reordered)


def run_active_bias(decision: Any, scenario: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    mod = _production()
    bias = mod.build_active_source_bias(decision, min_score_gap=float(variant.get("active_min_score_gap", 0.05)))
    topics = list((scenario.get("inputs") or {}).get("phase1_topics") or [])
    return {"bias": bias.to_log_dict(), "phase1_topics_before": topics,
            "phase1_topics_after": mod.reorder_phase1_topics_for_bias(topics, bias)}


def run_calibration(dataset: dict[str, Any], variant: dict[str, Any] | None = None) -> dict[str, Any]:
    from main_logic.proactive_recommendation_feedback import summarize_feedback_calibration, summarize_recommendation_feedback
    from main_logic.proactive_recommendation_observer import summarize_recommendation_calibration, summarize_recommendation_validation
    observations = dataset.get("observations") or []
    feedback = dataset.get("feedback") or []
    return {
        "observation_calibration": summarize_recommendation_calibration(observations),
        "validation": summarize_recommendation_validation(observations),
        "feedback": summarize_recommendation_feedback(observations, feedback),
        "feedback_calibration": summarize_feedback_calibration(observations, feedback),
        "variant": variant or {},
    }


__all__ = ["run_active_bias", "run_calibration", "run_material_stage", "run_source_stage"]
