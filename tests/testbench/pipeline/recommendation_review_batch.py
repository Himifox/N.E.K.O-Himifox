"""Apply a structured candidate-first review batch by stable turn ID."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Any


SEMANTIC_FIELDS = {
    "should_recommend",
    "acceptable_top1_sources",
    "relevance",
    "must_filter_candidate_ids",
    "expected_filter_reasons",
    "interruption_level",
    "privacy_risk",
    "score_diagnosis",
    "issue_layer",
    "comment",
}


def expand_review_seed(
    workbook: dict[str, Any],
    seed: dict[str, Any],
) -> dict[str, Any]:
    """Expand source-keyed review decisions into candidate-ID keyed proposals."""
    if int(seed.get("schema_version") or 0) != 1:
        raise ValueError("review seed schema_version must be 1")
    annotations = {
        str(annotation.get("turn_id") or ""): annotation
        for annotation in workbook.get("annotations") or []
    }
    expanded_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in seed.get("items") or []:
        turn_id = str(item.get("turn_id") or "")
        if not turn_id or turn_id in seen:
            raise ValueError(f"invalid or duplicate turn_id: {turn_id!r}")
        seen.add(turn_id)
        annotation = annotations.get(turn_id)
        if annotation is None:
            raise ValueError(f"unknown turn_id: {turn_id}")
        candidates = list(
            (annotation.get("context_for_review") or {}).get("candidates") or []
        )
        by_source: dict[str, dict[str, Any]] = {}
        by_id: dict[str, dict[str, Any]] = {}
        duplicate_sources: set[str] = set()
        for candidate in candidates:
            source = str(candidate.get("source_type") or "")
            candidate_id = str(candidate.get("id") or "")
            if not source or not candidate_id:
                raise ValueError(f"{turn_id}: candidate must have source_type and id")
            if source in by_source:
                duplicate_sources.add(source)
            else:
                by_source[source] = candidate
            by_id[candidate_id] = candidate
        relevance_by_candidate_id = item.get("relevance_by_candidate_id")
        if relevance_by_candidate_id is not None:
            relevance = dict(relevance_by_candidate_id)
            if set(relevance) != set(by_id):
                raise ValueError(
                    f"{turn_id}: relevance_by_candidate_id must cover candidates exactly"
                )
        else:
            if duplicate_sources:
                raise ValueError(
                    f"{turn_id}: source-keyed seed is ambiguous for sources "
                    f"{sorted(duplicate_sources)}"
                )
            relevance_by_source = dict(item.get("relevance_by_source") or {})
            if set(relevance_by_source) != set(by_source):
                raise ValueError(
                    f"{turn_id}: relevance_by_source must cover candidate sources exactly"
                )
            relevance = {
                str(by_source[source]["id"]): value
                for source, value in relevance_by_source.items()
            }
        must_filter_sources = set(item.get("must_filter_source_types") or [])
        reason_by_source = dict(
            item.get("expected_filter_reasons_by_source") or {}
        )
        if not must_filter_sources <= set(by_source):
            raise ValueError(f"{turn_id}: must-filter source is not a candidate")
        if set(reason_by_source) != must_filter_sources:
            raise ValueError(
                f"{turn_id}: every must-filter source needs exactly one reason"
            )
        fields = {
            "should_recommend": item.get("should_recommend"),
            "acceptable_top1_sources": list(
                item.get("acceptable_top1_sources") or []
            ),
            "relevance": relevance,
            "must_filter_candidate_ids": [
                str(by_source[source]["id"])
                for source in sorted(must_filter_sources)
            ],
            "expected_filter_reasons": {
                str(by_source[source]["id"]): reason_by_source[source]
                for source in sorted(must_filter_sources)
            },
            "interruption_level": str(
                item.get("interruption_level") or "borderline"
            ),
            "privacy_risk": str(item.get("privacy_risk") or "none"),
            "score_diagnosis": str(
                item.get("score_diagnosis") or "not_enough_context"
            ),
            "issue_layer": str(item.get("issue_layer") or "data"),
            "comment": str(item.get("comment") or ""),
        }
        expanded_items.append({
            "turn_id": turn_id,
            "confidence": str(item.get("confidence") or ""),
            "primary_review_status": str(
                item.get("primary_review_status") or "corrected"
            ),
            "fields": fields,
        })
    if not expanded_items:
        raise ValueError("review seed contains no items")
    return {
        "schema_version": 1,
        "batch_id": str(seed.get("batch_id") or ""),
        "assistant_reviewer_id": str(seed.get("assistant_reviewer_id") or ""),
        "assistant_reviewed_at": str(seed.get("assistant_reviewed_at") or ""),
        "items": expanded_items,
    }


def build_context_recovered_blind_bundle(
    workbook: dict[str, Any],
    fixed_turn_ids: list[str],
) -> dict[str, Any]:
    """Build a label-free second-review bundle with only causal context."""
    if not fixed_turn_ids or len(fixed_turn_ids) != len(set(fixed_turn_ids)):
        raise ValueError("fixed_turn_ids must be a non-empty unique list")
    annotations = {
        str(annotation.get("turn_id") or ""): annotation
        for annotation in workbook.get("annotations") or []
    }
    reviews: list[dict[str, Any]] = []
    for turn_id in fixed_turn_ids:
        annotation = annotations.get(turn_id)
        if annotation is None:
            raise ValueError(f"blind-review turn_id is missing: {turn_id}")
        context = deepcopy(annotation.get("context_for_review") or {})
        for downstream_key in ("delivered", "reason", "delivered_excerpt"):
            context.pop(downstream_key, None)
        pre_decision = context.get("pre_decision_context")
        if not isinstance(pre_decision, dict):
            raise ValueError(f"{turn_id}: missing pre_decision_context")
        messages = list(pre_decision.get("messages") or [])
        observation_ts = pre_decision.get("observation_ts")
        if not isinstance(observation_ts, (int, float)):
            raise ValueError(f"{turn_id}: invalid observation_ts")
        if any(
            not isinstance(message.get("ts_epoch"), (int, float))
            or float(message["ts_epoch"]) > float(observation_ts)
            for message in messages
        ):
            raise ValueError(f"{turn_id}: pre-decision context crosses causal boundary")
        candidates = list(context.get("candidates") or [])
        if not candidates or any(
            not str(candidate.get("id") or "")
            or not str(candidate.get("source_type") or "")
            for candidate in candidates
        ):
            raise ValueError(f"{turn_id}: invalid candidates")
        reviews.append({
            "turn_id": turn_id,
            "context_for_review": context,
            "second_review": {
                "required": True,
                "status": "pending",
                "reviewer_id": "",
                "reviewed_at": "",
                "should_recommend": None,
                "relevance": {},
                "comment": "",
                "abstain_reason": "",
            },
        })
    sample_sha256 = hashlib.sha256(
        json.dumps(fixed_turn_ids, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "schema_version": 2,
        "kind": "recommendation_blind_second_review_context_recovered",
        "source_dataset": workbook.get("source_dataset"),
        "source_sha256": workbook.get("source_sha256"),
        "selection": {
            "method": "preserve_fixed_v1_turn_ids_in_original_order",
            "sample_count": len(fixed_turn_ids),
            "turn_ids_sha256": sample_sha256,
        },
        "instructions": {
            "causal_order": (
                "judge pre-decision context first, then candidate relevance; "
                "delivery realization is excluded"
            ),
            "should_recommend": "boolean",
            "relevance": "integer 0-3 for every candidate",
            "reviewer_id": "required and independent from primary reviewer",
            "reviewed_at": "required ISO-8601 timestamp with timezone",
            "status": "completed after filling labels, or abstained when evidence is insufficient",
            "abstain_reason": "required when status is abstained",
            "blindness": (
                "primary labels, Codex proposals, realization text, feedback outcome, "
                "and diagnosis fields are intentionally omitted"
            ),
        },
        "reviews": reviews,
    }


def normalize_blind_second_reviews(
    bundle: dict[str, Any],
    *,
    default_reviewer_id: str,
    completed_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Conservatively normalize shorthand without inventing semantic labels."""
    if not default_reviewer_id.strip():
        raise ValueError("default_reviewer_id is required")
    if not _timezone_aware(completed_at):
        raise ValueError("completed_at must be ISO-8601 with timezone")
    output = deepcopy(bundle)
    audits: list[dict[str, Any]] = []
    normalized_count = 0
    abstained_count = 0
    for index, review in enumerate(output.get("reviews") or [], 1):
        turn_id = str(review.get("turn_id") or "")
        candidates = list(
            (review.get("context_for_review") or {}).get("candidates") or []
        )
        candidate_ids = [str(candidate.get("id") or "") for candidate in candidates]
        by_source: dict[str, list[str]] = {}
        for candidate in candidates:
            by_source.setdefault(
                str(candidate.get("source_type") or ""), []
            ).append(str(candidate.get("id") or ""))
        second = dict(review.get("second_review") or {})
        raw_second = deepcopy(second)
        raw_should = second.get("should_recommend")
        should: bool | None
        abstained = False
        if isinstance(raw_should, bool):
            should = raw_should
        elif isinstance(raw_should, str) and raw_should.lower() in {"true", "false"}:
            should = raw_should.lower() == "true"
        elif (
            isinstance(raw_should, str)
            and raw_should.lower() == "none"
        ) or (
            str(second.get("status") or "") == "abstained"
            and raw_should is None
            and not second.get("relevance")
            and bool(str(second.get("abstain_reason") or "").strip())
        ):
            should = None
            abstained = True
        else:
            should = None

        relevance_raw = (
            second.get("relevance")
            if isinstance(second.get("relevance"), dict)
            else {}
        )
        mapped: dict[str, int] = {}
        issues: list[str] = []
        dropped_unknown_keys: list[str] = []
        if "all" in relevance_raw:
            if len(relevance_raw) != 1:
                issues.append("all_cannot_be_combined_with_other_keys")
            else:
                try:
                    score = int(relevance_raw["all"])
                except (TypeError, ValueError):
                    issues.append("all_score_invalid")
                else:
                    if 0 <= score <= 3:
                        mapped = {candidate_id: score for candidate_id in candidate_ids}
                    else:
                        issues.append("all_score_invalid")
        else:
            by_compact_id = {
                candidate_id.replace(":", ""): candidate_id
                for candidate_id in candidate_ids
            }
            for raw_key, raw_score in relevance_raw.items():
                key = str(raw_key)
                if key in candidate_ids:
                    candidate_id = key
                elif key.replace(":", "") in by_compact_id:
                    candidate_id = by_compact_id[key.replace(":", "")]
                elif len(by_source.get(key, [])) == 1:
                    candidate_id = by_source[key][0]
                else:
                    dropped_unknown_keys.append(key)
                    continue
                try:
                    score = int(raw_score)
                except (TypeError, ValueError):
                    issues.append(f"invalid_score:{key}")
                    continue
                if isinstance(raw_score, bool) or not 0 <= score <= 3:
                    issues.append(f"invalid_score:{key}")
                    continue
                mapped[candidate_id] = score

        missing = sorted(set(candidate_ids) - set(mapped))
        if abstained:
            second.update({
                "required": True,
                "status": "abstained",
                "reviewer_id": str(second.get("reviewer_id") or default_reviewer_id),
                "reviewed_at": completed_at,
                "should_recommend": None,
                "relevance": {},
                "abstain_reason": str(
                    second.get("abstain_reason") or "other"
                ),
            })
            review["second_review"] = second
            normalized_count += 1
            abstained_count += 1
            state = "normalized_abstained"
        elif should is not None and not missing and not issues:
            second.update({
                "required": True,
                "status": "completed",
                "reviewer_id": str(second.get("reviewer_id") or default_reviewer_id),
                "reviewed_at": completed_at,
                "should_recommend": should,
                "relevance": mapped,
                "abstain_reason": "",
            })
            review["second_review"] = second
            normalized_count += 1
            state = "normalized_completed"
        else:
            state = "needs_human_correction"
            if should is None:
                issues.append("should_recommend_missing")
            if missing:
                issues.append("missing_candidate_scores")
            review["second_review"] = raw_second
        audits.append({
            "index": index,
            "turn_id": turn_id,
            "state": state,
            "candidates": [
                {
                    "id": str(candidate.get("id") or ""),
                    "source_type": str(candidate.get("source_type") or ""),
                    "safe_title": str(candidate.get("safe_title") or ""),
                }
                for candidate in candidates
            ],
            "candidate_ids": candidate_ids,
            "raw_should_recommend": raw_should,
            "raw_relevance": relevance_raw,
            "mapped_relevance": mapped,
            "missing_candidate_ids": missing,
            "dropped_unknown_keys": dropped_unknown_keys,
            "issues": issues,
        })
    output["normalization"] = {
        "schema_version": 1,
        "method": "conservative_source_alias_and_scalar_normalization",
        "default_reviewer_id": default_reviewer_id,
        "completed_at": completed_at,
        "normalized_count": normalized_count,
        "completed_count": normalized_count - abstained_count,
        "abstained_count": abstained_count,
        "unresolved_count": len(audits) - normalized_count,
    }
    return output, {
        **output["normalization"],
        "rows": audits,
    }


def reposition_blind_second_reviews(
    bundle: dict[str, Any],
    *,
    moves: list[tuple[int, int]] | None = None,
    swaps: list[tuple[int, int]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move or swap complete second-review payloads using 1-based row indexes."""
    output = deepcopy(bundle)
    reviews = list(output.get("reviews") or [])
    moves = list(moves or [])
    swaps = list(swaps or [])
    touched: set[int] = set()
    operations: list[dict[str, Any]] = []

    def _row(index: int) -> dict[str, Any]:
        if index < 1 or index > len(reviews):
            raise ValueError(f"review index out of range: {index}")
        return reviews[index - 1]

    def _claim(*indexes: int) -> None:
        overlap = touched.intersection(indexes)
        if overlap:
            raise ValueError(
                f"review indexes used by multiple operations: {sorted(overlap)}"
            )
        touched.update(indexes)

    def _blank_second_review() -> dict[str, Any]:
        return {
            "required": True,
            "status": "pending",
            "reviewer_id": "",
            "reviewed_at": "",
            "should_recommend": None,
            "relevance": {},
            "comment": "",
            "abstain_reason": "",
        }

    def _has_input(second: dict[str, Any]) -> bool:
        return any([
            second.get("should_recommend") is not None,
            bool(second.get("relevance")),
            bool(str(second.get("comment") or "").strip()),
            bool(str(second.get("abstain_reason") or "").strip()),
        ])

    for source_index, target_index in moves:
        _claim(source_index, target_index)
        source = _row(source_index)
        target = _row(target_index)
        source_second = dict(source.get("second_review") or {})
        target_second = dict(target.get("second_review") or {})
        if not _has_input(source_second):
            raise ValueError(f"move source row has no review input: {source_index}")
        if _has_input(target_second):
            raise ValueError(f"move target row already has review input: {target_index}")
        target["second_review"] = deepcopy(source_second)
        source["second_review"] = _blank_second_review()
        operations.append({
            "operation": "move",
            "source_index": source_index,
            "source_turn_id": str(source.get("turn_id") or ""),
            "target_index": target_index,
            "target_turn_id": str(target.get("turn_id") or ""),
        })

    for left_index, right_index in swaps:
        _claim(left_index, right_index)
        left = _row(left_index)
        right = _row(right_index)
        left_second = deepcopy(dict(left.get("second_review") or {}))
        right_second = deepcopy(dict(right.get("second_review") or {}))
        if not _has_input(left_second) or not _has_input(right_second):
            raise ValueError(
                f"swap requires review input on both rows: {left_index}, {right_index}"
            )
        left["second_review"] = right_second
        right["second_review"] = left_second
        operations.append({
            "operation": "swap",
            "left_index": left_index,
            "left_turn_id": str(left.get("turn_id") or ""),
            "right_index": right_index,
            "right_turn_id": str(right.get("turn_id") or ""),
        })

    output["position_repair"] = {
        "schema_version": 1,
        "method": "explicit_human_confirmed_row_reposition",
        "operations": operations,
    }
    return output, output["position_repair"]


def apply_blind_second_review_corrections(
    bundle: dict[str, Any],
    corrections: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply explicit human corrections by turn ID before final normalization."""
    output = deepcopy(bundle)
    reviews = list(output.get("reviews") or [])
    by_turn = {
        str(review.get("turn_id") or ""): review
        for review in reviews
    }
    seen: set[str] = set()
    applied: list[dict[str, Any]] = []
    for correction in corrections:
        turn_id = str(correction.get("turn_id") or "")
        if not turn_id or turn_id in seen:
            raise ValueError(f"invalid or duplicate correction turn_id: {turn_id!r}")
        seen.add(turn_id)
        review = by_turn.get(turn_id)
        if review is None:
            raise ValueError(f"unknown correction turn_id: {turn_id}")
        second = dict(review.get("second_review") or {})
        if str(second.get("status") or "pending") not in {"pending", ""}:
            raise ValueError(f"correction target is not pending: {turn_id}")

        abstain = bool(correction.get("abstain"))
        should = correction.get("should_recommend")
        relevance = correction.get("relevance")
        if abstain:
            if should is not None:
                raise ValueError(f"{turn_id}: abstain cannot include should_recommend")
            second["should_recommend"] = "none"
            second["relevance"] = {}
            second["abstain_reason"] = str(
                correction.get("abstain_reason") or "insufficient_context"
            )
        else:
            if should is not None:
                if not isinstance(should, bool):
                    raise ValueError(f"{turn_id}: should_recommend must be boolean")
                second["should_recommend"] = should
            if relevance is not None:
                if not isinstance(relevance, dict):
                    raise ValueError(f"{turn_id}: relevance must be an object")
                merged = dict(
                    second.get("relevance")
                    if isinstance(second.get("relevance"), dict)
                    else {}
                )
                merged.update(relevance)
                second["relevance"] = merged

        comment_append = str(correction.get("comment_append") or "").strip()
        if comment_append:
            existing = str(second.get("comment") or "").strip()
            second["comment"] = (
                f"{existing}\n{comment_append}".strip()
                if existing
                else comment_append
            )
        review["second_review"] = second
        applied.append({
            "turn_id": turn_id,
            "abstained": abstain,
            "updated_should_recommend": should is not None,
            "updated_relevance_keys": sorted(
                relevance.keys()
                if isinstance(relevance, dict)
                else []
            ),
        })
    output["correction_application"] = {
        "schema_version": 1,
        "method": "explicit_human_missing_field_corrections",
        "applied_count": len(applied),
        "rows": applied,
    }
    return output, output["correction_application"]


def merge_blind_second_reviews(
    workbook: dict[str, Any],
    blind_bundle: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge a completed blind sample and calculate descriptive agreement."""
    output = deepcopy(workbook)
    annotations = list(output.get("annotations") or [])
    required = {
        str(annotation.get("turn_id") or ""): annotation
        for annotation in annotations
        if (annotation.get("second_review") or {}).get("required")
    }
    reviews = list(blind_bundle.get("reviews") or [])
    blind_by_turn = {
        str(review.get("turn_id") or ""): review
        for review in reviews
    }
    if len(blind_by_turn) != len(reviews):
        raise ValueError("blind review contains missing or duplicate turn IDs")
    if set(blind_by_turn) != set(required):
        raise ValueError("blind review turn IDs must exactly match required sample")

    gate_pairs: list[tuple[bool, bool]] = []
    candidate_pairs: list[tuple[int, int]] = []
    top_set_matches = 0
    joint_count = 0
    abstained_count = 0
    disagreements: list[dict[str, Any]] = []
    reviewer_ids: set[str] = set()
    for turn_id, annotation in required.items():
        blind_row = blind_by_turn[turn_id]
        second = deepcopy(dict(blind_row.get("second_review") or {}))
        status = str(second.get("status") or "")
        if status not in {"completed", "abstained"}:
            raise ValueError(f"{turn_id}: blind review is not handled")
        reviewer_id = str(second.get("reviewer_id") or "")
        if not reviewer_id or not str(second.get("reviewed_at") or ""):
            raise ValueError(f"{turn_id}: blind review identity/time is missing")
        if reviewer_id == str(annotation.get("primary_reviewer_id") or ""):
            raise ValueError(f"{turn_id}: second reviewer must differ from primary")
        reviewer_ids.add(reviewer_id)
        workbook_ids = {
            str(candidate.get("id") or "")
            for candidate in (
                (annotation.get("context_for_review") or {}).get("candidates") or []
            )
        }
        blind_ids = {
            str(candidate.get("id") or "")
            for candidate in (
                (blind_row.get("context_for_review") or {}).get("candidates") or []
            )
        }
        if workbook_ids != blind_ids:
            raise ValueError(f"{turn_id}: candidate IDs differ from primary workbook")
        if status == "completed":
            if not isinstance(second.get("should_recommend"), bool):
                raise ValueError(f"{turn_id}: completed should_recommend must be boolean")
            relevance = second.get("relevance")
            if not isinstance(relevance, dict) or set(relevance) != workbook_ids:
                raise ValueError(f"{turn_id}: completed relevance coverage is invalid")
            if any(
                not isinstance(score, int)
                or isinstance(score, bool)
                or not 0 <= score <= 3
                for score in relevance.values()
            ):
                raise ValueError(f"{turn_id}: completed relevance score is invalid")
        else:
            abstained_count += 1
            if (
                second.get("should_recommend") is not None
                or second.get("relevance")
                or not str(second.get("abstain_reason") or "")
            ):
                raise ValueError(f"{turn_id}: abstained payload is invalid")
        annotation["second_review"] = second

        primary_eligible = (
            annotation.get("primary_review_status") in {"accepted", "corrected"}
            and isinstance(annotation.get("should_recommend"), bool)
        )
        if not primary_eligible or status != "completed":
            continue
        primary_relevance = dict(annotation.get("relevance") or {})
        second_relevance = dict(second.get("relevance") or {})
        if set(primary_relevance) != workbook_ids:
            raise ValueError(f"{turn_id}: primary relevance coverage is invalid")
        joint_count += 1
        gate_pairs.append((
            bool(annotation["should_recommend"]),
            bool(second["should_recommend"]),
        ))
        for candidate_id in sorted(workbook_ids):
            candidate_pairs.append((
                int(primary_relevance[candidate_id]),
                int(second_relevance[candidate_id]),
            ))
        primary_best = max(primary_relevance.values(), default=0)
        second_best = max(second_relevance.values(), default=0)
        primary_top = {
            candidate_id
            for candidate_id, score in primary_relevance.items()
            if score == primary_best
        }
        second_top = {
            candidate_id
            for candidate_id, score in second_relevance.items()
            if score == second_best
        }
        top_match = primary_top == second_top
        top_set_matches += int(top_match)
        gate_match = (
            annotation["should_recommend"] == second["should_recommend"]
        )
        relevance_match = primary_relevance == second_relevance
        if not gate_match or not relevance_match:
            disagreements.append({
                "turn_id": turn_id,
                "gate_match": gate_match,
                "top_relevance_set_match": top_match,
                "primary_should_recommend": annotation["should_recommend"],
                "second_should_recommend": second["should_recommend"],
                "primary_relevance": primary_relevance,
                "second_relevance": second_relevance,
            })

    gate_matches = sum(left == right for left, right in gate_pairs)
    exact_candidate_matches = sum(left == right for left, right in candidate_pairs)
    within_one_matches = sum(abs(left - right) <= 1 for left, right in candidate_pairs)
    absolute_error = sum(abs(left - right) for left, right in candidate_pairs)
    gate_count = len(gate_pairs)
    candidate_count = len(candidate_pairs)
    primary_positive = (
        sum(left for left, _ in gate_pairs) / gate_count if gate_count else 0.0
    )
    second_positive = (
        sum(right for _, right in gate_pairs) / gate_count if gate_count else 0.0
    )
    expected_agreement = (
        primary_positive * second_positive
        + (1 - primary_positive) * (1 - second_positive)
    )
    observed_agreement = gate_matches / gate_count if gate_count else None
    kappa = (
        (observed_agreement - expected_agreement) / (1 - expected_agreement)
        if observed_agreement is not None and expected_agreement < 1
        else None
    )
    agreement = {
        "schema_version": 1,
        "required_count": len(required),
        "handled_count": len(reviews),
        "completed_count": len(reviews) - abstained_count,
        "abstained_count": abstained_count,
        "jointly_eligible_count": joint_count,
        "second_reviewer_ids": sorted(reviewer_ids),
        "gate_exact_agreement": {
            "numerator": gate_matches,
            "denominator": gate_count,
            "value": round(observed_agreement, 4)
            if observed_agreement is not None
            else None,
        },
        "gate_cohen_kappa": round(kappa, 4) if kappa is not None else None,
        "candidate_score_exact_agreement": {
            "numerator": exact_candidate_matches,
            "denominator": candidate_count,
            "value": round(exact_candidate_matches / candidate_count, 4)
            if candidate_count
            else None,
        },
        "candidate_score_within_one": {
            "numerator": within_one_matches,
            "denominator": candidate_count,
            "value": round(within_one_matches / candidate_count, 4)
            if candidate_count
            else None,
        },
        "candidate_score_mae": (
            round(absolute_error / candidate_count, 4)
            if candidate_count
            else None
        ),
        "top_relevance_set_agreement": {
            "numerator": top_set_matches,
            "denominator": joint_count,
            "value": round(top_set_matches / joint_count, 4)
            if joint_count
            else None,
        },
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
    }
    output["second_review_merge"] = {
        "schema_version": 1,
        "source_kind": blind_bundle.get("kind"),
        "required_count": len(required),
        "handled_count": len(reviews),
        "agreement": agreement,
    }
    return output, agreement


def build_lightweight_adjudication_bundle(
    workbook: dict[str, Any],
) -> dict[str, Any]:
    """Classify the fixed blind sample and prepare only gate conflicts for review."""
    annotations = list(workbook.get("annotations") or [])
    items: list[dict[str, Any]] = []
    counts = {"A": 0, "B": 0, "C": 0, "excluded": 0}
    for annotation in annotations:
        second = dict(annotation.get("second_review") or {})
        if not second.get("required"):
            continue
        primary_status = str(annotation.get("primary_review_status") or "")
        second_status = str(second.get("status") or "")
        if primary_status == "abstained" or second_status == "abstained":
            grade = "excluded"
            resolution_policy = "excluded_any_reviewer_abstained"
        else:
            primary_relevance = dict(annotation.get("relevance") or {})
            second_relevance = dict(second.get("relevance") or {})
            if annotation.get("should_recommend") != second.get("should_recommend"):
                grade = "A"
                resolution_policy = "manual_gate_conflict_adjudication"
            else:
                primary_best = max(primary_relevance.values(), default=0)
                second_best = max(second_relevance.values(), default=0)
                primary_top = {
                    candidate_id
                    for candidate_id, score in primary_relevance.items()
                    if score == primary_best
                }
                second_top = {
                    candidate_id
                    for candidate_id, score in second_relevance.items()
                    if score == second_best
                }
                max_difference = max(
                    (
                        abs(
                            int(primary_relevance[candidate_id])
                            - int(second_relevance[candidate_id])
                        )
                        for candidate_id in primary_relevance
                    ),
                    default=0,
                )
                if primary_top == second_top and max_difference <= 1:
                    grade = "C"
                    resolution_policy = "retain_primary_minor_difference"
                else:
                    grade = "B"
                    resolution_policy = "retain_primary_low_confidence"
        counts[grade] += 1
        context = deepcopy(dict(annotation.get("context_for_review") or {}))
        context.pop("delivered", None)
        context.pop("reason", None)
        context.pop("delivered_excerpt", None)
        item = {
            "turn_id": str(annotation.get("turn_id") or ""),
            "grade": grade,
            "resolution_policy": resolution_policy,
            "context_for_review": context,
            "single_candidate_recovery": deepcopy(
                (annotation.get("codex_evidence") or {}).get(
                    "single_candidate_recovery"
                )
                or {}
            ),
            "primary_review": {
                "status": primary_status,
                "should_recommend": annotation.get("should_recommend"),
                "relevance": deepcopy(dict(annotation.get("relevance") or {})),
                "comment": str(annotation.get("comment") or ""),
            },
            "second_review": {
                "status": second_status,
                "should_recommend": second.get("should_recommend"),
                "relevance": deepcopy(dict(second.get("relevance") or {})),
                "comment": str(second.get("comment") or ""),
                "abstain_reason": str(second.get("abstain_reason") or ""),
            },
            "adjudication": {
                "status": "pending" if grade == "A" else resolution_policy,
                "adjudicator_id": "",
                "adjudicated_at": "",
                "candidate_relevance": {},
                "timing_ok": None,
                "fatigue_suppressed": None,
                "should_recommend": None,
                "reason_code": "",
                "comment": "",
            },
        }
        items.append(item)
    return {
        "schema_version": 1,
        "kind": "recommendation_p44e2_lightweight_adjudication",
        "source_dataset": workbook.get("source_dataset"),
        "mode": "lightweight_gate_conflicts_only",
        "counts": counts,
        "instructions": {
            "manual_scope": "grade A only",
            "candidate_relevance": "integer 0-3 for every candidate",
            "timing_ok": "boolean",
            "fatigue_suppressed": "boolean",
            "should_recommend": "boolean",
            "reason_code": (
                "required; all-zero relevance with should_recommend=true is allowed "
                "only for single_candidate_recovery with matching evidence"
            ),
            "history": "primary and second reviews are immutable evidence",
        },
        "items": items,
    }


def validate_lightweight_adjudication_bundle(
    bundle: dict[str, Any],
    *,
    require_complete: bool,
) -> list[dict[str, str]]:
    """Validate A-grade adjudications without changing historical reviews."""
    errors: list[dict[str, str]] = []
    for index, item in enumerate(bundle.get("items") or []):
        if item.get("grade") != "A":
            continue
        path = f"items[{index}].adjudication"
        adjudication = dict(item.get("adjudication") or {})
        status = str(adjudication.get("status") or "pending")
        if status == "pending" and not require_complete:
            continue
        if status != "completed":
            errors.append({
                "path": f"{path}.status",
                "message": "must be completed",
            })
            continue
        if not str(adjudication.get("adjudicator_id") or "").strip():
            errors.append({
                "path": f"{path}.adjudicator_id",
                "message": "is required",
            })
        if not _timezone_aware(str(adjudication.get("adjudicated_at") or "")):
            errors.append({
                "path": f"{path}.adjudicated_at",
                "message": "must be ISO-8601 with timezone",
            })
        candidates = list(
            (item.get("context_for_review") or {}).get("candidates") or []
        )
        candidate_ids = {
            str(candidate.get("id") or "")
            for candidate in candidates
        }
        relevance = adjudication.get("candidate_relevance")
        if not isinstance(relevance, dict) or set(relevance) != candidate_ids:
            errors.append({
                "path": f"{path}.candidate_relevance",
                "message": "must contain every and only candidate id",
            })
            relevance = {}
        elif any(
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= 3
            for score in relevance.values()
        ):
            errors.append({
                "path": f"{path}.candidate_relevance",
                "message": "scores must be integer 0-3",
            })
        for key in ("timing_ok", "fatigue_suppressed", "should_recommend"):
            if not isinstance(adjudication.get(key), bool):
                errors.append({
                    "path": f"{path}.{key}",
                    "message": "must be boolean",
                })
        reason_code = str(adjudication.get("reason_code") or "")
        if not reason_code:
            errors.append({
                "path": f"{path}.reason_code",
                "message": "is required",
            })
        all_zero = bool(relevance) and not any(
            isinstance(score, int) and score > 0
            for score in relevance.values()
        )
        if adjudication.get("should_recommend") is True and all_zero:
            recovery = dict(item.get("single_candidate_recovery") or {})
            if (
                reason_code != "single_candidate_recovery"
                or recovery.get("active") is not True
            ):
                errors.append({
                    "path": f"{path}.should_recommend",
                    "message": (
                        "all-zero recommendation requires active "
                        "single_candidate_recovery evidence and reason code"
                    ),
                })
    return errors


def finalize_lightweight_adjudication(
    workbook: dict[str, Any],
    bundle: dict[str, Any],
    decisions: list[dict[str, Any]],
    *,
    adjudicator_id: str,
    adjudicated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Apply confirmed A decisions and materialize non-destructive final labels."""
    if not adjudicator_id.strip():
        raise ValueError("adjudicator_id is required")
    if not _timezone_aware(adjudicated_at):
        raise ValueError("adjudicated_at must be ISO-8601 with timezone")
    finalized_bundle = deepcopy(bundle)
    items = list(finalized_bundle.get("items") or [])
    by_turn = {
        str(item.get("turn_id") or ""): item
        for item in items
    }
    grade_a_ids = {
        turn_id
        for turn_id, item in by_turn.items()
        if item.get("grade") == "A"
    }
    decision_by_turn = {
        str(decision.get("turn_id") or ""): decision
        for decision in decisions
    }
    if len(decision_by_turn) != len(decisions):
        raise ValueError("decisions contain missing or duplicate turn IDs")
    if set(decision_by_turn) != grade_a_ids:
        raise ValueError("decisions must exactly cover every grade A turn ID")

    for turn_id in sorted(grade_a_ids):
        decision = decision_by_turn[turn_id]
        item = by_turn[turn_id]
        item["adjudication"] = {
            "status": "completed",
            "adjudicator_id": adjudicator_id,
            "adjudicated_at": adjudicated_at,
            "candidate_relevance": deepcopy(
                dict(decision.get("candidate_relevance") or {})
            ),
            "timing_ok": decision.get("timing_ok"),
            "fatigue_suppressed": decision.get("fatigue_suppressed"),
            "should_recommend": decision.get("should_recommend"),
            "reason_code": str(decision.get("reason_code") or ""),
            "comment": str(decision.get("comment") or ""),
        }
    validation_errors = validate_lightweight_adjudication_bundle(
        finalized_bundle,
        require_complete=True,
    )
    if validation_errors:
        raise ValueError(
            "adjudication validation failed: "
            + json.dumps(validation_errors, ensure_ascii=False)
        )

    finalized_workbook = deepcopy(workbook)
    annotations = {
        str(annotation.get("turn_id") or ""): annotation
        for annotation in finalized_workbook.get("annotations") or []
    }
    status_counts: dict[str, int] = {}
    for item in items:
        turn_id = str(item.get("turn_id") or "")
        annotation = annotations.get(turn_id)
        if annotation is None:
            raise ValueError(f"adjudication references unknown turn ID: {turn_id}")
        grade = str(item.get("grade") or "")
        if grade == "A":
            adjudication = dict(item["adjudication"])
            status = "completed"
            reviewer = adjudicator_id
            reviewed_at = adjudicated_at
            relevance = deepcopy(adjudication["candidate_relevance"])
            timing_ok = adjudication["timing_ok"]
            fatigue_suppressed = adjudication["fatigue_suppressed"]
            should_recommend = adjudication["should_recommend"]
            reason_code = adjudication["reason_code"]
            comment = adjudication["comment"]
        elif grade in {"B", "C"}:
            status = str(item.get("resolution_policy") or "")
            reviewer = "policy:retain_primary"
            reviewed_at = adjudicated_at
            relevance = deepcopy(dict(annotation.get("relevance") or {}))
            timing_ok = None
            fatigue_suppressed = None
            should_recommend = annotation.get("should_recommend")
            reason_code = status
            comment = (
                "Lightweight P44-E2 policy retained the primary label; "
                "the original blind review remains available."
            )
        else:
            status = "excluded_abstention"
            reviewer = "policy:exclude_abstention"
            reviewed_at = adjudicated_at
            relevance = {}
            timing_ok = None
            fatigue_suppressed = None
            should_recommend = None
            reason_code = "at_least_one_reviewer_abstained"
            comment = "Excluded from adjudicated metrics; no labels were invented."
        annotation.update({
            "adjudication_grade": grade,
            "adjudication_status": status,
            "adjudicator_id": reviewer,
            "adjudicated_at": reviewed_at,
            "adjudicated_relevance": relevance,
            "adjudicated_timing_ok": timing_ok,
            "adjudicated_fatigue_suppressed": fatigue_suppressed,
            "adjudicated_should_recommend": should_recommend,
            "adjudication_reason_code": reason_code,
            "adjudication_comment": comment,
        })
        status_counts[status] = status_counts.get(status, 0) + 1

    audit = {
        "schema_version": 1,
        "mode": "lightweight_gate_conflicts_only",
        "adjudicator_id": adjudicator_id,
        "adjudicated_at": adjudicated_at,
        "status_distribution": dict(sorted(status_counts.items())),
        "grade_distribution": deepcopy(finalized_bundle.get("counts") or {}),
        "validation_error_count": 0,
        "historical_reviews_modified": False,
        "production_config_modified": False,
    }
    finalized_workbook["adjudication_summary"] = audit
    finalized_bundle["finalization"] = audit
    return finalized_workbook, finalized_bundle, audit


def _timezone_aware(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def apply_review_batch(
    workbook: dict[str, Any],
    batch: dict[str, Any],
    *,
    confirm: bool = False,
    primary_reviewer_id: str = "",
    primary_reviewed_at: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply proposals without relying on JSON text position."""
    if int(batch.get("schema_version") or 0) != 1:
        raise ValueError("review batch schema_version must be 1")
    items = list(batch.get("items") or [])
    if not items:
        raise ValueError("review batch contains no items")
    if confirm:
        if not primary_reviewer_id.strip():
            raise ValueError("primary_reviewer_id is required with confirm")
        if not _timezone_aware(primary_reviewed_at):
            raise ValueError("primary_reviewed_at must be ISO-8601 with timezone")

    output = deepcopy(workbook)
    annotations = list(output.get("annotations") or [])
    by_turn = {
        str(annotation.get("turn_id") or ""): annotation
        for annotation in annotations
    }
    seen: set[str] = set()
    applied: list[str] = []
    for item in items:
        turn_id = str(item.get("turn_id") or "")
        if not turn_id or turn_id in seen:
            raise ValueError(f"invalid or duplicate turn_id: {turn_id!r}")
        seen.add(turn_id)
        annotation = by_turn.get(turn_id)
        if annotation is None:
            raise ValueError(f"unknown turn_id: {turn_id}")
        fields = dict(item.get("fields") or {})
        unknown_fields = sorted(set(fields) - SEMANTIC_FIELDS)
        if unknown_fields:
            raise ValueError(f"{turn_id}: unknown fields {unknown_fields}")
        candidates = list(
            (annotation.get("context_for_review") or {}).get("candidates") or []
        )
        candidate_ids = {
            str(candidate.get("id") or "")
            for candidate in candidates
            if str(candidate.get("id") or "")
        }
        relevance = fields.get("relevance")
        if not isinstance(relevance, dict) or set(relevance) != candidate_ids:
            raise ValueError(f"{turn_id}: relevance must cover every candidate exactly")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 3
            for value in relevance.values()
        ):
            raise ValueError(f"{turn_id}: relevance values must be integers 0-3")
        filter_ids = set(fields.get("must_filter_candidate_ids") or [])
        filter_reasons = set((fields.get("expected_filter_reasons") or {}).keys())
        if not filter_ids <= candidate_ids or not filter_reasons <= candidate_ids:
            raise ValueError(f"{turn_id}: filter references unknown candidate")
        for key, value in fields.items():
            annotation[key] = deepcopy(value)
        proposal = {
            "status": "human_confirmed" if confirm else "proposed",
            "batch_id": str(batch.get("batch_id") or ""),
            "reviewer_id": str(batch.get("assistant_reviewer_id") or ""),
            "reviewed_at": str(batch.get("assistant_reviewed_at") or ""),
            "evidence_scope": "pre_decision_context_only",
            "requires_human_confirmation": not confirm,
            "confidence": str(item.get("confidence") or ""),
        }
        if confirm:
            proposal["confirmed_by"] = primary_reviewer_id
            proposal["confirmed_at"] = primary_reviewed_at
            annotation["primary_review_status"] = str(
                item.get("primary_review_status") or "corrected"
            )
            annotation["primary_reviewer_id"] = primary_reviewer_id
            annotation["primary_reviewed_at"] = primary_reviewed_at
            annotation["primary_abstain_reason"] = ""
        annotation["assistant_review_proposal"] = proposal
        applied.append(turn_id)

    history = list(output.get("review_batch_history") or [])
    history.append({
        "batch_id": str(batch.get("batch_id") or ""),
        "turn_ids": applied,
        "confirmed": confirm,
        "primary_reviewer_id": primary_reviewer_id if confirm else "",
        "primary_reviewed_at": primary_reviewed_at if confirm else "",
    })
    output["review_batch_history"] = history
    return output, {
        "batch_id": str(batch.get("batch_id") or ""),
        "applied_count": len(applied),
        "confirmed_count": len(applied) if confirm else 0,
        "turn_ids": applied,
    }
