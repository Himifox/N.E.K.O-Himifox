"""Quality audit, human annotation, and golden promotion for Shadow datasets."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
from typing import Any

ANNOTATION_VERSION = 1
ISSUE_LAYERS = {"candidate", "filter", "score", "bias", "data", "none"}
INTERRUPTION_LEVELS = {"acceptable", "borderline", "interruptive", "none"}
PRIVACY_RISKS = {"none", "low", "medium", "high"}
SCORE_DIAGNOSES = {"missing_candidate", "not_enough_context", "over_scored", "reasonable",
                    "under_scored", "wrong_source"}
PRIMARY_REVIEW_STATUSES = {"pending", "accepted", "corrected"}
SECOND_REVIEW_STATUSES = {"not_required", "pending", "completed"}
ENUM_ALIASES = {
    "interruption_level": {"disturbing": "interruptive", "severe": "interruptive"},
    "privacy_risk": {"suspected": "medium", "violation": "high"},
    "score_diagnosis": {"not_applicable": "not_enough_context"},
}


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
    # Algorithm identity and source revision are independent dimensions.  Do
    # not fall back from one to the other: doing so makes a partially stamped
    # dataset look like it contains multiple algorithm versions.
    versions = Counter(str(row.get("algorithm_version") or "unknown") for row in observations)
    revisions = Counter(str(row.get("git_revision") or "unknown") for row in observations)
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
        "git_revisions": dict(sorted(revisions.items())),
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
        interruption_level = _enum(raw, "interruption_level", INTERRUPTION_LEVELS, path, errors)
        privacy_risk = _enum(raw, "privacy_risk", PRIVACY_RISKS, path, errors)
        score_diagnosis = _enum(raw, "score_diagnosis", SCORE_DIAGNOSES, path, errors)
        issue_layer = _enum(raw, "issue_layer", ISSUE_LAYERS, path, errors)
        primary_review_status = str(raw.get("primary_review_status") or "pending")
        if primary_review_status not in PRIMARY_REVIEW_STATUSES:
            errors.append({"path": f"{path}.primary_review_status",
                           "message": f"must be one of {sorted(PRIMARY_REVIEW_STATUSES)}"})
        primary_reviewer_id = str(raw.get("primary_reviewer_id") or "")[:64]
        primary_reviewed_at = str(raw.get("primary_reviewed_at") or "")[:64]
        if primary_review_status in {"accepted", "corrected"} and not primary_reviewer_id:
            errors.append({"path": f"{path}.primary_reviewer_id",
                           "message": "is required after human acceptance or correction"})
        if primary_review_status in {"accepted", "corrected"} and not primary_reviewed_at:
            errors.append({"path": f"{path}.primary_reviewed_at",
                           "message": "is required after human acceptance or correction"})
        elif primary_reviewed_at and not _is_aware_iso8601(primary_reviewed_at):
            errors.append({"path": f"{path}.primary_reviewed_at",
                           "message": "must be an ISO-8601 timestamp with timezone"})
        second_review = _validate_second_review(
            raw.get("second_review"), candidate_ids, path, errors
        )
        normalized.append({"schema_version": ANNOTATION_VERSION, "turn_id": turn_id,
                           "should_recommend": raw.get("should_recommend"),
                           "acceptable_top1_sources": list(raw.get("acceptable_top1_sources") or []),
                           "relevance": relevance,
                           "must_filter_candidate_ids": list(raw.get("must_filter_candidate_ids") or []),
                           "expected_filter_reasons": dict(raw.get("expected_filter_reasons") or {}),
                           "interruption_level": interruption_level, "privacy_risk": privacy_risk,
                           "score_diagnosis": score_diagnosis, "issue_layer": issue_layer,
                           "comment": str(raw.get("comment") or "")[:500],
                           "annotator_id": str(raw.get("annotator_id") or "anonymous")[:64],
                           "primary_review_status": primary_review_status,
                           "primary_reviewer_id": primary_reviewer_id,
                           "primary_reviewed_at": primary_reviewed_at,
                           "second_review": second_review,
                           "reviewed": second_review["status"] == "completed",
                           "reviewer_id": second_review["reviewer_id"]})
    return {"ok": not errors, "errors": errors, "normalized": normalized if not errors else None}


def annotation_summary(dataset: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(dataset.get("observations") or []); annotated = len(annotations)
    human_confirmed = sum(
        row.get("primary_review_status") in {"accepted", "corrected"}
        and bool(row.get("primary_reviewer_id"))
        and _is_aware_iso8601(row.get("primary_reviewed_at")) for row in annotations
    )
    required_second = [row for row in annotations if (row.get("second_review") or {}).get("required")]
    reviewed = sum((row.get("second_review") or {}).get("status") == "completed"
                   and _is_aware_iso8601((row.get("second_review") or {}).get("reviewed_at"))
                   for row in required_second)
    sensitive = [row for row in annotations if row.get("privacy_risk") not in {None, "none"}]
    sensitive_reviewed = sum((row.get("second_review") or {}).get("status") == "completed"
                             and _is_aware_iso8601((row.get("second_review") or {}).get("reviewed_at"))
                             for row in sensitive)
    return {"total": total, "annotated": annotated, "completion_rate": round(annotated / total, 4) if total else 0.0,
            "human_confirmed": human_confirmed,
            "human_confirmation_rate": round(human_confirmed / total, 4) if total else 0.0,
            "second_review_required": len(required_second), "second_reviewed": reviewed,
            "second_review_completion_rate": round(reviewed / len(required_second), 4) if required_second else 0.0,
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
    if annotation["human_confirmation_rate"] < 1.0: blockers.append("primary_human_review_incomplete")
    if annotation["second_review_required"] < math.ceil(max(annotation["total"], 1) * 0.2):
        blockers.append("second_review_sample_below_20_percent")
    elif annotation["second_review_completion_rate"] < 1.0:
        blockers.append("second_review_incomplete")
    if annotation["sensitive_review_rate"] < 1.0: blockers.append("sensitive_review_incomplete")
    return {"ready_for_weight_candidates": not blockers, "blockers": blockers,
            "quality": quality, "annotation": annotation}


def _candidate_ids(observation: dict[str, Any]) -> set[str]:
    return {str(row.get("id")) for row in observation.get("top_candidates") or [] if row.get("id")}


def _enum(raw: dict[str, Any], key: str, allowed: set[str], path: str,
          errors: list[dict[str, str]]) -> Any:
    value = ENUM_ALIASES.get(key, {}).get(raw.get(key), raw.get(key))
    if value not in allowed:
        errors.append({"path": f"{path}.{key}", "message": f"must be one of {sorted(allowed)}"})
    return value


def _is_aware_iso8601(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _validate_second_review(value: Any, candidate_ids: set[str], path: str,
                            errors: list[dict[str, str]]) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    required = bool(raw.get("required"))
    status = str(raw.get("status") or ("pending" if required else "not_required"))
    if status not in SECOND_REVIEW_STATUSES:
        errors.append({"path": f"{path}.second_review.status",
                       "message": f"must be one of {sorted(SECOND_REVIEW_STATUSES)}"})
    reviewer_id = str(raw.get("reviewer_id") or "")[:64]
    reviewed_at = str(raw.get("reviewed_at") or "")[:64]
    second_relevance = raw.get("relevance") or {}
    if status == "completed":
        if not required:
            errors.append({"path": f"{path}.second_review.required",
                           "message": "must be true for a completed second review"})
        if not reviewer_id:
            errors.append({"path": f"{path}.second_review.reviewer_id",
                           "message": "is required for a completed second review"})
        if not reviewed_at:
            errors.append({"path": f"{path}.second_review.reviewed_at",
                           "message": "is required for a completed second review"})
        elif not _is_aware_iso8601(reviewed_at):
            errors.append({"path": f"{path}.second_review.reviewed_at",
                           "message": "must be an ISO-8601 timestamp with timezone"})
        if not isinstance(raw.get("should_recommend"), bool):
            errors.append({"path": f"{path}.second_review.should_recommend",
                           "message": "must be boolean for a completed second review"})
        if set(second_relevance) != candidate_ids:
            errors.append({"path": f"{path}.second_review.relevance",
                           "message": "must contain every and only candidate id"})
        for candidate_id, score in second_relevance.items():
            if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 3:
                errors.append({"path": f"{path}.second_review.relevance.{candidate_id}",
                               "message": "must be integer 0-3"})
    return {"required": required, "status": status, "reviewer_id": reviewer_id,
            "reviewed_at": reviewed_at,
            "should_recommend": raw.get("should_recommend"),
            "relevance": dict(second_relevance),
            "comment": str(raw.get("comment") or "")[:500]}
