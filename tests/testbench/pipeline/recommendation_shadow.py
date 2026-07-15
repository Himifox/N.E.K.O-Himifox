"""Quality audit, human annotation, and golden promotion for Shadow datasets."""
from __future__ import annotations

from collections import Counter
from typing import Any

ANNOTATION_VERSION = 1
ISSUE_LAYERS = {"candidate", "filter", "score", "bias", "data", "none"}
INTERRUPTION_LEVELS = {"acceptable", "disturbing", "severe"}
PRIVACY_RISKS = {"none", "suspected", "violation"}
SCORE_DIAGNOSES = {"over_scored", "reasonable", "under_scored", "not_applicable"}


def audit_shadow_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    observations = dataset.get("observations") or []
    feedback = dataset.get("feedback") or []
    turn_ids = [str(row.get("turn_id") or "") for row in observations]
    valid_turns = {turn_id for turn_id in turn_ids if turn_id}
    feedback_turns = {str(row.get("turn_id") or "") for row in feedback if row.get("turn_id")}
    duplicate_turn_ids = sorted(turn for turn, count in Counter(turn_ids).items() if turn and count > 1)
    invalid_observations = [index for index, row in enumerate(observations)
                            if not row.get("turn_id") or not isinstance(row.get("ts"), (int, float))]
    invalid_feedback = [index for index, row in enumerate(feedback)
                        if not row.get("turn_id") or not isinstance(row.get("ts"), (int, float))]
    sources = Counter(str(row.get("shadow_selected_source_type") or "none") for row in observations)
    activities = Counter(str(row.get("activity_state") or "unknown") for row in observations)
    versions = Counter(str(row.get("git_revision") or row.get("algorithm_version") or "unknown") for row in observations)
    joined = len(valid_turns & feedback_turns)
    return {
        "observation_count": len(observations), "feedback_count": len(feedback),
        "feedback_joined_count": joined,
        "feedback_join_rate": round(joined / len(valid_turns), 4) if valid_turns else 0.0,
        "feedback_orphan_turn_ids": sorted(feedback_turns - valid_turns),
        "duplicate_turn_ids": duplicate_turn_ids,
        "invalid_observation_indexes": invalid_observations,
        "invalid_feedback_indexes": invalid_feedback,
        "source_distribution": dict(sorted(sources.items())),
        "activity_distribution": dict(sorted(activities.items())),
        "algorithm_versions": dict(sorted(versions.items())),
        "mixed_algorithm_versions": len(versions) > 1,
    }


def validate_annotations(dataset: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    observations = {str(row.get("turn_id") or ""): row for row in dataset.get("observations") or [] if row.get("turn_id")}
    errors: list[dict[str, str]] = []
    normalized = []
    seen: set[str] = set()
    for index, raw in enumerate(annotations):
        path = f"annotations[{index}]"
        turn_id = str(raw.get("turn_id") or "")
        if not turn_id or turn_id not in observations:
            errors.append({"path": f"{path}.turn_id", "message": "must reference an observation turn_id"}); continue
        if turn_id in seen:
            errors.append({"path": f"{path}.turn_id", "message": "duplicate annotation"}); continue
        seen.add(turn_id)
        candidate_ids = _candidate_ids(observations[turn_id])
        relevance = raw.get("relevance") or {}
        if not isinstance(raw.get("should_recommend"), bool):
            errors.append({"path": f"{path}.should_recommend", "message": "must be boolean"})
        if not isinstance(relevance, dict):
            errors.append({"path": f"{path}.relevance", "message": "must be an object"}); relevance = {}
        for candidate_id, value in relevance.items():
            if candidate_id not in candidate_ids:
                errors.append({"path": f"{path}.relevance.{candidate_id}", "message": "unknown candidate id"})
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
                errors.append({"path": f"{path}.relevance.{candidate_id}", "message": "must be integer 0-3"})
        _enum(raw, "interruption_level", INTERRUPTION_LEVELS, path, errors)
        _enum(raw, "privacy_risk", PRIVACY_RISKS, path, errors)
        _enum(raw, "score_diagnosis", SCORE_DIAGNOSES, path, errors)
        _enum(raw, "issue_layer", ISSUE_LAYERS, path, errors)
        normalized.append({"schema_version": ANNOTATION_VERSION, "turn_id": turn_id,
                           "should_recommend": raw.get("should_recommend"),
                           "acceptable_top1_sources": list(raw.get("acceptable_top1_sources") or []),
                           "relevance": relevance,
                           "must_filter_candidate_ids": list(raw.get("must_filter_candidate_ids") or []),
                           "expected_filter_reasons": dict(raw.get("expected_filter_reasons") or {}),
                           "interruption_level": raw.get("interruption_level"), "privacy_risk": raw.get("privacy_risk"),
                           "score_diagnosis": raw.get("score_diagnosis"), "issue_layer": raw.get("issue_layer"),
                           "comment": str(raw.get("comment") or "")[:500],
                           "annotator_id": str(raw.get("annotator_id") or "anonymous")[:64],
                           "reviewed": bool(raw.get("reviewed")), "reviewer_id": str(raw.get("reviewer_id") or "")[:64]})
    return {"ok": not errors, "errors": errors, "normalized": normalized if not errors else None}


def annotation_summary(dataset: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(dataset.get("observations") or []); annotated = len(annotations)
    reviewed = sum(bool(row.get("reviewed")) for row in annotations)
    sensitive = [row for row in annotations if row.get("privacy_risk") in {"suspected", "violation"}]
    sensitive_reviewed = sum(bool(row.get("reviewed")) for row in sensitive)
    return {"total": total, "annotated": annotated, "completion_rate": round(annotated / total, 4) if total else 0.0,
            "reviewed": reviewed, "review_rate": round(reviewed / annotated, 4) if annotated else 0.0,
            "sensitive_count": len(sensitive), "sensitive_reviewed": sensitive_reviewed,
            "sensitive_review_rate": round(sensitive_reviewed / len(sensitive), 4) if sensitive else 1.0}


def p44_readiness(dataset: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    quality = audit_shadow_dataset(dataset); annotation = annotation_summary(dataset, annotations)
    blockers = []
    if quality["observation_count"] < 100: blockers.append("observation_count_below_100")
    if quality["feedback_joined_count"] < 30: blockers.append("feedback_joined_count_below_30")
    if quality["duplicate_turn_ids"]: blockers.append("duplicate_turn_ids")
    if quality["invalid_observation_indexes"] or quality["invalid_feedback_indexes"]: blockers.append("invalid_records")
    if quality["mixed_algorithm_versions"]: blockers.append("mixed_algorithm_versions")
    if annotation["completion_rate"] < 1.0: blockers.append("annotation_incomplete")
    if annotation["review_rate"] < 0.2: blockers.append("double_review_below_20_percent")
    if annotation["sensitive_review_rate"] < 1.0: blockers.append("sensitive_review_incomplete")
    return {"ready_for_weight_candidates": not blockers, "blockers": blockers,
            "quality": quality, "annotation": annotation}


def _candidate_ids(observation: dict[str, Any]) -> set[str]:
    return {str(row.get("id")) for row in observation.get("top_candidates") or [] if row.get("id")}


def _enum(raw: dict[str, Any], key: str, allowed: set[str], path: str, errors: list[dict[str, str]]) -> None:
    if raw.get(key) not in allowed:
        errors.append({"path": f"{path}.{key}", "message": f"must be one of {sorted(allowed)}"})
