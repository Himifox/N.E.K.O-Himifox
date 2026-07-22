"""P44-F2-R0 blind timing-label manifest and readiness gate.

This module is deliberately Testbench-only.  It never rewrites a freeze and
never exposes outcome, feedback, score, or timing fields to a human reviewer.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import math
from typing import Any, Mapping


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "recommendation_p44f2_timing_annotation_manifest"
REVIEW_STATUSES = {"pending", "completed", "abstained"}
CONFIDENCE_VALUES = {"low", "medium", "high"}
ABSTAIN_REASONS = {
    "insufficient_review_context",
    "privacy_redaction",
    "ambiguous_candidate_context",
    "other",
}
REASON_CODES = {
    "candidate_appropriate",
    "candidate_irrelevant",
    "activity_unsuitable",
    "repeat_or_fatigue",
    "privacy_or_safety",
    "insufficient_review_context",
    "other",
}
ADJUDICATION_STATUSES = {"not_required", "pending", "completed", "excluded"}
FORBIDDEN_BLIND_KEYS = {
    "delivered", "delivered_excerpt", "feedback", "feedback_inferred",
    "decision_context", "timing", "actual_reason_code", "actual_stage",
    "actual_candidate_score", "actual_rank", "shadow_selected_score",
    "shadow_selected_candidate_id", "shadow_selected_source_type", "score",
    "rank", "reason", "realization_review_context",
}
TECHNICAL_REASON_CODES = {"DELIVERY_PREEMPTED", "PASS_GENERATION_EMPTY"}
MIN_STRATUM_COUNT = 8
MIN_DELIVERY_SIDE_COUNT = 20


def build_timing_annotation_manifest(
    freeze: Mapping[str, Any],
    *,
    source_freeze_filename: str,
    source_freeze_sha256: str,
    created_at: str,
    second_review_rate: float = 0.20,
) -> dict[str, Any]:
    """Create a blind-review manifest that references, but never changes, a freeze."""
    if not _valid_sha256(source_freeze_sha256):
        raise ValueError("source_freeze_sha256 must be a 64-character SHA-256")
    if not _aware_iso8601(created_at):
        raise ValueError("created_at must be ISO-8601 with timezone")
    if not 0 < second_review_rate <= 1:
        raise ValueError("second_review_rate must be in (0, 1]")

    raw_items: list[dict[str, Any]] = []
    seen_turn_ids: set[str] = set()
    eligible_by_delivery: dict[bool, list[str]] = {True: [], False: []}
    for index, observation in enumerate(freeze.get("observations") or []):
        item, reviewable = _blind_item(observation, index=index)
        turn_id = item["turn_id"]
        if turn_id and turn_id in seen_turn_ids:
            item["review_eligible"] = False
            item["exclusion_reasons"].append("duplicate_turn_id")
            reviewable = False
        if turn_id:
            seen_turn_ids.add(turn_id)
        raw_items.append(item)
        # Technical/privacy outcomes are still left available for a reviewer to
        # abstain from, but cannot enter the future F2 denominators or consume
        # quota in the precommitted blind-second-review sample.
        if reviewable and not _excluded_outcome(observation):
            eligible_by_delivery[observation.get("delivered") is True].append(turn_id)

    required_second = _second_review_selection(
        eligible_by_delivery, rate=second_review_rate
    )
    for item in raw_items:
        item["second_review"]["required"] = item["turn_id"] in required_second
        if item["second_review"]["required"]:
            item["second_review"]["status"] = "pending"
        item["adjudication"]["status"] = (
            "pending" if item["second_review"]["required"] else "not_required"
        )

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "created_at": created_at,
        "source_freeze": {
            "filename": source_freeze_filename,
            "sha256": source_freeze_sha256.lower(),
            "observation_count": len(raw_items),
        },
        "blindness_contract": {
            "hidden_from_reviewers": [
                "production score/rank/selected source", "delivered and reason",
                "explicit feedback and inferred ignored", "all timing values",
                "downstream generated text",
            ],
            "allowed_evidence": [
                "sanitized candidate title/summary/source", "activity_state",
                "review_context redaction notes",
            ],
            "rule": "Do not infer a label from outcome or feedback; those fields are absent.",
        },
        "review_protocol": {
            "primary": {
                "labels": ["should_recommend", "confidence", "reason_code", "comment"],
                "should_recommend": [True, False, "abstain"],
            },
            "second_review": {
                "selection": "deterministic stratified minimum 20% blind sample",
                "independence_required": True,
            },
            "adjudication": {
                "required_when": "completed primary and second labels disagree",
                "preserve_raw_reviews": True,
            },
        },
        "items": raw_items,
    }


def validate_timing_annotation_manifest(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return all structural and blindness errors without interpreting labels."""
    errors: list[dict[str, str]] = []
    if manifest.get("kind") != MANIFEST_KIND:
        errors.append(_error("kind", f"must be {MANIFEST_KIND}"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(_error("schema_version", "unsupported schema version"))
    source = manifest.get("source_freeze") or {}
    if not _valid_sha256(source.get("sha256")):
        errors.append(_error("source_freeze.sha256", "must be a SHA-256"))
    items = manifest.get("items") or []
    if not isinstance(items, list) or not items:
        return errors + [_error("items", "must be a non-empty list")]
    seen: set[str] = set()
    for index, item in enumerate(items):
        path = f"items[{index}]"
        turn_id = str(item.get("turn_id") or "")
        if not turn_id:
            errors.append(_error(f"{path}.turn_id", "is required"))
        elif turn_id in seen:
            errors.append(_error(f"{path}.turn_id", "must be unique"))
        seen.add(turn_id)
        errors.extend(_blindness_errors(item, path))
        errors.extend(_validate_review(item.get("primary_review"), f"{path}.primary_review", required=False))
        second = item.get("second_review") or {}
        if not isinstance(second.get("required"), bool):
            errors.append(_error(f"{path}.second_review.required", "must be boolean"))
        errors.extend(_validate_review(second, f"{path}.second_review", required=bool(second.get("required"))))
        adjudication = item.get("adjudication") or {}
        status = str(adjudication.get("status") or "")
        if status not in ADJUDICATION_STATUSES:
            errors.append(_error(f"{path}.adjudication.status", "invalid status"))
        if status == "completed" and not isinstance(adjudication.get("should_recommend"), bool):
            errors.append(_error(f"{path}.adjudication.should_recommend", "must be boolean when completed"))
    return errors


def timing_annotation_readiness(
    freeze: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Assess whether blind labels permit a fresh P44-F2 association analysis.

    It intentionally reports `hold` until enough non-abstained labels exist in
    every delivered/pass × should_recommend cell.  It does not run F2 itself.
    """
    errors = validate_timing_annotation_manifest(manifest)
    observations = {
        str(row.get("turn_id") or ""): row
        for row in freeze.get("observations") or []
        if str(row.get("turn_id") or "")
    }
    manifest_ids = {str(item.get("turn_id") or "") for item in manifest.get("items") or []}
    unknown = sorted(turn_id for turn_id in manifest_ids if turn_id not in observations)
    if unknown:
        errors.append(_error("items", f"unknown freeze turn IDs: {unknown[:3]}"))

    feedback_turns = {
        str(event.get("turn_id") or "")
        for event in freeze.get("feedback") or []
        if str(event.get("turn_id") or "") and event.get("feedback_inferred") is not True
    }
    cells: Counter[str] = Counter()
    eligible_structural = 0
    primary_handled = 0
    primary_abstained = 0
    qualified: list[tuple[dict[str, Any], bool]] = []
    second_required = 0
    second_handled = 0
    unresolved_disagreements = 0
    for item in manifest.get("items") or []:
        if not item.get("review_eligible"):
            continue
        turn_id = str(item.get("turn_id") or "")
        observation = observations.get(turn_id)
        if observation is None or _excluded_outcome(observation):
            continue
        eligible_structural += 1
        primary = item.get("primary_review") or {}
        if primary.get("status") == "abstained":
            primary_handled += 1
            primary_abstained += 1
        elif primary.get("status") == "completed":
            primary_handled += 1
        second = item.get("second_review") or {}
        if second.get("required"):
            second_required += 1
            if second.get("status") in {"completed", "abstained"}:
                second_handled += 1
        label, unresolved = _resolved_label(item)
        if unresolved:
            unresolved_disagreements += 1
        if label is None:
            continue
        delivered = observation.get("delivered") is True
        side = "delivered" if delivered else "pass"
        answer = "true" if label else "false"
        cells[f"{side}_should_{answer}"] += 1
        qualified.append((observation, delivered))

    delivered_count = sum(delivered for _row, delivered in qualified)
    pass_count = len(qualified) - delivered_count
    feedback_covered = sum(
        delivered and str(row.get("turn_id") or "") in feedback_turns
        for row, delivered in qualified
    )
    blockers: list[str] = []
    if errors:
        blockers.append("manifest_invalid")
    if primary_handled < eligible_structural:
        blockers.append("primary_review_incomplete")
    if second_handled < second_required:
        blockers.append("blind_second_review_incomplete")
    if unresolved_disagreements:
        blockers.append("adjudication_incomplete")
    if delivered_count < MIN_DELIVERY_SIDE_COUNT:
        blockers.append("qualified_delivered_below_20")
    if pass_count < MIN_DELIVERY_SIDE_COUNT:
        blockers.append("qualified_pass_below_20")
    for cell in (
        "delivered_should_true", "delivered_should_false",
        "pass_should_true", "pass_should_false",
    ):
        if cells[cell] < MIN_STRATUM_COUNT:
            blockers.append(f"{cell}_below_{MIN_STRATUM_COUNT}")
    return {
        "status": "ready_for_f2_rerun" if not blockers else "hold",
        "blockers": blockers,
        "validation_errors": errors,
        "requirements": {
            "minimum_qualified_delivered": MIN_DELIVERY_SIDE_COUNT,
            "minimum_qualified_pass": MIN_DELIVERY_SIDE_COUNT,
            "minimum_each_delivery_label_cell": MIN_STRATUM_COUNT,
            "all_primary_reviews_handled": True,
            "all_required_second_reviews_handled": True,
            "all_disagreements_adjudicated": True,
        },
        "counts": {
            "freeze_observation_count": len(observations),
            "manifest_item_count": len(manifest.get("items") or []),
            "structurally_eligible_count": eligible_structural,
            "primary_handled_count": primary_handled,
            "primary_abstained_count": primary_abstained,
            "second_review_required_count": second_required,
            "second_review_handled_count": second_handled,
            "unresolved_disagreement_count": unresolved_disagreements,
            "qualified_count": len(qualified),
            "qualified_delivered_count": delivered_count,
            "qualified_pass_count": pass_count,
            "explicit_feedback_covered_delivered_count": feedback_covered,
            "explicit_feedback_coverage": _rate(feedback_covered, delivered_count),
            "cells": dict(cells),
        },
        "denominator_contract": {
            "false_interruption": "qualified delivered and human should_recommend=false / qualified delivered non-abstention",
            "missed_opportunity": "qualified non-delivered and human should_recommend=true / qualified non-delivered non-abstention",
            "explicit_feedback_coverage": "valid-turn explicit feedback / qualified delivered non-abstention",
            "excluded": "privacy hard blocks, technical failures, abstentions, and inferred ignored",
        },
        "production_config_modified": False,
        "tuning_modified": False,
    }


def _blind_item(observation: Mapping[str, Any], *, index: int) -> tuple[dict[str, Any], bool]:
    turn_id = str(observation.get("turn_id") or "")
    context = observation.get("review_context")
    reasons: list[str] = []
    if not turn_id:
        reasons.append("missing_turn_id")
    if not isinstance(context, Mapping):
        reasons.append("missing_review_context")
        context = {}
    candidates = []
    for candidate in context.get("candidate_labels") or []:
        candidate_id = str(candidate.get("id") or "")
        source_type = str(candidate.get("source_type") or "")
        title = str(candidate.get("safe_title") or "")
        summary = str(candidate.get("safe_summary") or "")
        if not candidate_id or not source_type or not (title or summary):
            reasons.append("incomplete_candidate_review_context")
            continue
        candidates.append({
            "id": candidate_id,
            "source_type": source_type,
            "safe_title": title,
            "safe_summary": summary,
        })
    if not candidates:
        reasons.append("no_reviewable_candidates")
    item = {
        "turn_id": turn_id,
        "review_eligible": not reasons,
        "exclusion_reasons": sorted(set(reasons)),
        "context_for_blind_review": {
            "activity_state": str(context.get("activity_state") or "unknown"),
            "candidates": candidates,
            "redaction_notes": [str(note) for note in context.get("redaction_notes") or []],
        },
        "primary_review": _blank_review(),
        "second_review": {"required": False, **_blank_review()},
        "adjudication": {
            "status": "not_required",
            "adjudicator_id": "",
            "adjudicated_at": "",
            "should_recommend": None,
            "reason_code": "",
            "comment": "",
        },
        "source_index": index,
    }
    # source_index is internal identity only; remove it from the reviewer bundle.
    item.pop("source_index")
    return item, not reasons


def _blank_review() -> dict[str, Any]:
    return {
        "status": "pending",
        "reviewer_id": "",
        "reviewed_at": "",
        "should_recommend": None,
        "confidence": "",
        "reason_code": "",
        "comment": "",
        "abstain_reason": "",
    }


def _second_review_selection(
    eligible_by_delivery: Mapping[bool, list[str]], *, rate: float,
) -> set[str]:
    selected: set[str] = set()
    for delivered in (True, False):
        turn_ids = sorted(eligible_by_delivery[delivered])
        if not turn_ids:
            continue
        count = math.ceil(len(turn_ids) * rate)
        selected.update(sorted(turn_ids, key=lambda turn_id: _stable_key(delivered, turn_id))[:count])
    return selected


def _stable_key(delivered: bool, turn_id: str) -> str:
    return hashlib.sha256(f"p44-f2-r0:{int(delivered)}:{turn_id}".encode("utf-8")).hexdigest()


def _validate_review(value: Any, path: str, *, required: bool) -> list[dict[str, str]]:
    review = value if isinstance(value, Mapping) else {}
    errors: list[dict[str, str]] = []
    status = str(review.get("status") or "")
    if status not in REVIEW_STATUSES:
        return [_error(f"{path}.status", "must be pending, completed, or abstained")]
    handled = status in {"completed", "abstained"}
    if required and status == "pending":
        pass
    if handled and not str(review.get("reviewer_id") or "").strip():
        errors.append(_error(f"{path}.reviewer_id", "required after review"))
    if handled and not _aware_iso8601(str(review.get("reviewed_at") or "")):
        errors.append(_error(f"{path}.reviewed_at", "timezone-aware ISO-8601 required after review"))
    if status == "completed":
        if not isinstance(review.get("should_recommend"), bool):
            errors.append(_error(f"{path}.should_recommend", "must be boolean when completed"))
        if review.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(_error(f"{path}.confidence", "must be low, medium, or high when completed"))
        if review.get("reason_code") not in REASON_CODES:
            errors.append(_error(f"{path}.reason_code", "invalid reason code"))
    if status == "abstained" and review.get("abstain_reason") not in ABSTAIN_REASONS:
        errors.append(_error(f"{path}.abstain_reason", "invalid abstain reason"))
    return errors


def _resolved_label(item: Mapping[str, Any]) -> tuple[bool | None, bool]:
    primary = item.get("primary_review") or {}
    if primary.get("status") != "completed":
        return None, False
    primary_label = primary.get("should_recommend")
    second = item.get("second_review") or {}
    if not second.get("required"):
        return primary_label, False
    if second.get("status") != "completed":
        return None, False
    second_label = second.get("should_recommend")
    if primary_label == second_label:
        return primary_label, False
    adjudication = item.get("adjudication") or {}
    if adjudication.get("status") == "completed":
        return adjudication.get("should_recommend"), False
    return None, True


def _excluded_outcome(observation: Mapping[str, Any]) -> bool:
    reason = str(observation.get("actual_reason_code") or "")
    return reason in TECHNICAL_REASON_CODES or "PRIVACY" in reason


def _blindness_errors(value: Any, path: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in FORBIDDEN_BLIND_KEYS:
                errors.append(_error(f"{path}.{key}", "forbidden in blind manifest"))
            errors.extend(_blindness_errors(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_blindness_errors(child, f"{path}[{index}]"))
    return errors


def _error(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)


def _aware_iso8601(value: str) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


__all__ = [
    "build_timing_annotation_manifest", "validate_timing_annotation_manifest",
    "timing_annotation_readiness",
]
