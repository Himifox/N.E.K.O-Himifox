"""Sanitizers for persisted recommendation observations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any

_TOP_LEVEL_KEYS = {
    "ts",
    "lanlan_name",
    "turn_id",
    "activity_state",
    "activity_propensity",
    "algorithm_version",
    "git_revision",
    "review_context",
    "decision_context",
    "feedback_state_preview",
    "preference_state",
    "personalization",
    "policy_decision",
    "recommendation_mode",
    "decision_stage",
    "candidate_count",
    "shadow_selected_source_type",
    "shadow_selected_candidate_id",
    "shadow_selected_score",
    "top_candidates",
    "actual_primary_channel",
    "actual_source_tag",
    "actual_reason_code",
    "actual_stage",
    "active_channels",
    "delivered",
    "actual_rank",
    "actual_candidate_id",
    "actual_candidate_score",
    "matched_actual_material",
    "matched_actual_source",
    "active_bias_applied",
    "active_preferred_source_type",
    "active_preferred_source_tag",
    "active_preferred_candidate_id",
    "active_bias_fallback_reason",
    "active_model_followed_preference",
}
_TOP_CANDIDATE_KEYS = {"rank", "id", "source_type", "family", "topic_usable", "score"}
_REVIEW_CONTEXT_KEYS = {
    "schema_version",
    "candidate_labels",
    "activity_state",
    "delivered_excerpt",
    "redaction_notes",
}
_REVIEW_CANDIDATE_LABEL_KEYS = {
    "id",
    "source_type",
    "safe_title",
    "safe_summary",
    "score",
}
REVIEW_CONTEXT_MAX_CANDIDATES = 3
REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH = 96
REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH = 240
REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH = 160
REVIEW_CONTEXT_REDACTION_NOTES_MAX = 8
_REVIEW_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_REVIEW_SECRET_RE = re.compile(
    r"\b(token|cookie|authorization|api[_-]?key|session[_-]?id)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)


def sanitize_recommendation_observation(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the compact, file-safe observation shape used by JSONL logging."""
    safe: dict[str, Any] = {}
    for key in _TOP_LEVEL_KEYS:
        if key not in observation:
            continue
        if key == "top_candidates":
            safe[key] = _sanitize_top_candidates(observation.get(key))
        elif key == "review_context":
            review_context = sanitize_recommendation_review_context(
                observation.get(key)
            )
            if review_context:
                safe[key] = review_context
        elif key == "decision_context":
            decision_context = sanitize_recommendation_decision_context(
                observation.get(key)
            )
            if decision_context:
                safe[key] = decision_context
        elif key == "feedback_state_preview":
            state_preview = sanitize_recommendation_feedback_state_preview(
                observation.get(key)
            )
            if state_preview:
                safe[key] = state_preview
        elif key == "preference_state":
            preference_state = sanitize_recommendation_preference_state(
                observation.get(key)
            )
            if preference_state:
                safe[key] = preference_state
        elif key == "personalization":
            personalization = sanitize_recommendation_personalization(
                observation.get(key)
            )
            if personalization:
                safe[key] = personalization
        elif key == "policy_decision":
            policy = sanitize_recommendation_policy_decision(observation.get(key))
            if policy:
                safe[key] = policy
        elif key == "active_channels":
            safe[key] = _clean_string_list(observation.get(key))
        else:
            safe[key] = _json_safe_scalar(observation.get(key))
    return safe


def sanitize_recommendation_decision_context(value: Any) -> dict[str, Any]:
    """Return the bounded, observation-only context allowed for offline gates."""
    if not isinstance(value, Mapping):
        return {}
    timing = value.get("timing")
    if not isinstance(timing, Mapping):
        return {}

    safe_timing = {
        "configured_interval_seconds": _bounded_optional_number(
            timing.get("configured_interval_seconds"),
            lower=0.0,
            upper=86_400.0,
        ),
        "elapsed_since_last_delivery_seconds": _bounded_optional_number(
            timing.get("elapsed_since_last_delivery_seconds"),
            lower=0.0,
            upper=31_536_000.0,
        ),
        "recent_delivery_count_30m": _bounded_nonnegative_int(
            timing.get("recent_delivery_count_30m")
        ),
        "recent_delivery_count_2h": _bounded_nonnegative_int(
            timing.get("recent_delivery_count_2h")
        ),
        "consecutive_unanswered_deliveries": _bounded_nonnegative_int(
            timing.get("consecutive_unanswered_deliveries")
        ),
    }
    return {"timing": safe_timing}


def sanitize_recommendation_feedback_state_preview(value: Any) -> dict[str, Any]:
    """Keep only bounded v2 conversation and source aggregates."""
    if not isinstance(value, Mapping):
        return {}
    if value.get("version") == "feedback_state_preview_v1":
        return _sanitize_legacy_feedback_state_preview(value)
    if value.get("version") != "feedback_state_preview_v2":
        return {}
    conversation = value.get("conversation_acceptance")
    source_affinity = value.get("source_affinity")
    if not isinstance(conversation, Mapping) or not isinstance(
        source_affinity, Mapping
    ):
        return {}
    conversation_temporary = conversation.get("temporary")
    conversation_persistent = conversation.get("persistent")
    source_temporary = source_affinity.get("temporary")
    source_persistent = source_affinity.get("persistent")
    if not all(
        isinstance(item, Mapping)
        for item in (
            conversation_temporary,
            conversation_persistent,
            source_temporary,
            source_persistent,
        )
    ):
        return {}
    return {
        "version": "feedback_state_preview_v2",
        "preview_only": value.get("preview_only") is not False,
        "ranking_consumed": value.get("ranking_consumed") is True,
        "tuning_consumed": False,
        "conversation_acceptance": {
            "temporary": {
                "ttl_seconds": _bounded_nonnegative_int(
                    conversation_temporary.get("ttl_seconds")
                ),
                **_sanitize_feedback_state_bucket(
                    conversation_temporary,
                    persistent=False,
                ),
            },
            "persistent": {
                "min_explicit_evidence": _bounded_nonnegative_int(
                    conversation_persistent.get("min_explicit_evidence")
                ),
                **_sanitize_feedback_state_bucket(
                    conversation_persistent,
                    persistent=True,
                    score_key="acceptance_preview",
                ),
            },
        },
        "source_affinity": {
            "temporary": {
                "ttl_seconds": _bounded_nonnegative_int(
                    source_temporary.get("ttl_seconds")
                ),
                "sources": _sanitize_feedback_state_sources(
                    source_temporary.get("sources"),
                    persistent=False,
                ),
            },
            "persistent": {
                "min_explicit_evidence": _bounded_nonnegative_int(
                    source_persistent.get("min_explicit_evidence")
                ),
                "sources": _sanitize_feedback_state_sources(
                    source_persistent.get("sources"),
                    persistent=True,
                ),
            },
        },
    }


def sanitize_recommendation_personalization(value: Any) -> dict[str, Any]:
    """Keep only bounded baseline-vs-personalized ranking diagnostics."""

    if not isinstance(value, Mapping):
        return {}
    mode = str(value.get("mode") or "").strip().lower()
    if mode not in {"shadow_compare", "active"}:
        return {}
    candidates = value.get("candidates")
    safe_candidates: list[dict[str, Any]] = []
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for row in candidates[:20]:
            if not isinstance(row, Mapping):
                continue
            candidate_id = str(row.get("id") or "").strip()[:160]
            source_type = str(row.get("source_type") or "").strip().lower()[:32]
            if not candidate_id or not source_type:
                continue
            safe_candidates.append(
                {
                    "id": candidate_id,
                    "source_type": source_type,
                    "baseline_rank": _bounded_nonnegative_int(row.get("baseline_rank")),
                    "personalized_rank": _bounded_nonnegative_int(
                        row.get("personalized_rank")
                    ),
                    "baseline_score": _bounded_optional_number(
                        row.get("baseline_score"), lower=0.0, upper=1.0
                    ),
                    "delta": _bounded_optional_number(
                        row.get("delta"), lower=-0.03, upper=0.03
                    ),
                    "personalized_score": _bounded_optional_number(
                        row.get("personalized_score"), lower=0.0, upper=1.0
                    ),
                }
            )
    ranking_consumed = mode == "active" and value.get("ranking_consumed") is True
    return {
        "mode": mode,
        "ranking_consumed": ranking_consumed,
        "baseline_selected_candidate_id": str(
            value.get("baseline_selected_candidate_id") or ""
        ).strip()[:160]
        or None,
        "baseline_selected_source_type": str(
            value.get("baseline_selected_source_type") or ""
        )
        .strip()
        .lower()[:32]
        or None,
        "personalized_selected_candidate_id": str(
            value.get("personalized_selected_candidate_id") or ""
        ).strip()[:160]
        or None,
        "personalized_selected_source_type": str(
            value.get("personalized_selected_source_type") or ""
        )
        .strip()
        .lower()[:32]
        or None,
        "top1_changed": value.get("top1_changed") is True,
        "candidates": safe_candidates,
    }


def sanitize_recommendation_preference_state(value: Any) -> dict[str, Any]:
    """Keep only the public, bounded preference snapshot."""
    if not isinstance(value, Mapping):
        return {}
    if value.get("version") != "recommendation_preference_state_v1":
        return {}
    raw_sources = value.get("sources")
    if not isinstance(raw_sources, Mapping):
        return {}
    sources: dict[str, dict[str, Any]] = {}
    for raw_source, raw_bucket in raw_sources.items():
        source = str(raw_source or "").strip().lower()[:32]
        if not source or not isinstance(raw_bucket, Mapping):
            continue
        sources[source] = {
            "effective_success": _bounded_optional_number(
                raw_bucket.get("effective_success"), lower=0.0, upper=1_000_000.0
            ),
            "effective_failure": _bounded_optional_number(
                raw_bucket.get("effective_failure"), lower=0.0, upper=1_000_000.0
            ),
            "effective_evidence": _bounded_optional_number(
                raw_bucket.get("effective_evidence"), lower=0.0, upper=1_000_000.0
            ),
            "posterior_alpha": _bounded_optional_number(
                raw_bucket.get("posterior_alpha"), lower=0.0, upper=1_000_002.0
            ),
            "posterior_beta": _bounded_optional_number(
                raw_bucket.get("posterior_beta"), lower=0.0, upper=1_000_002.0
            ),
            "posterior_mean": _bounded_optional_number(
                raw_bucket.get("posterior_mean"), lower=0.0, upper=1.0
            ),
            "direction": _bounded_optional_number(
                raw_bucket.get("direction"), lower=-1.0, upper=1.0
            ),
            "confidence": _bounded_optional_number(
                raw_bucket.get("confidence"), lower=0.0, upper=1.0
            ),
            "personalization_delta": _bounded_optional_number(
                raw_bucket.get("personalization_delta"), lower=-0.03, upper=0.03
            ),
            "updated_at": _bounded_optional_number(
                raw_bucket.get("updated_at"), lower=0.0, upper=4_102_444_800.0
            ),
        }
    prior = (
        value.get("beta_prior") if isinstance(value.get("beta_prior"), Mapping) else {}
    )
    return {
        "version": "recommendation_preference_state_v1",
        "half_life_seconds": _bounded_nonnegative_int(value.get("half_life_seconds")),
        "beta_prior": {
            "alpha": _bounded_optional_number(
                prior.get("alpha"), lower=0.0, upper=100.0
            ),
            "beta": _bounded_optional_number(prior.get("beta"), lower=0.0, upper=100.0),
        },
        "min_evidence": _bounded_optional_number(
            value.get("min_evidence"), lower=0.0, upper=1000.0
        ),
        "saturation_evidence": _bounded_optional_number(
            value.get("saturation_evidence"), lower=0.0, upper=1000.0
        ),
        "max_abs_delta": _bounded_optional_number(
            value.get("max_abs_delta"), lower=0.0, upper=0.03
        ),
        "legacy_replacement_approximation_count": _bounded_nonnegative_int(
            value.get("legacy_replacement_approximation_count")
        ),
        "sources": sources,
    }


def sanitize_recommendation_policy_decision(value: Any) -> dict[str, Any]:
    """Validate source-bandit action support and propensity logging."""
    if not isinstance(value, Mapping):
        return {}
    mode = str(value.get("mode") or "").strip().lower()
    if mode not in {"shadow", "canary"}:
        return {}
    context_version = str(value.get("context_version") or "source-context-v1").strip()
    is_v2 = context_version in {
        "source-context-v2",
        "source-context-v3",
        "source-context-v4",
    }
    if context_version not in {
        "source-context-v1",
        "source-context-v2",
        "source-context-v3",
        "source-context-v4",
    }:
        return {}
    eligible = [
        item
        for item in _clean_string_list(value.get("eligible_arms"))
        if item in {"news", "music", "meme"}
    ]
    target_probabilities = value.get(
        "target_action_probabilities" if is_v2 else "action_probabilities"
    )
    if not isinstance(target_probabilities, Mapping):
        return {}
    safe_target_probabilities = {
        arm: _bounded_optional_number(
            target_probabilities.get(arm), lower=0.0, upper=1.0
        )
        for arm in eligible
    }
    if any(number is None for number in safe_target_probabilities.values()):
        return {}
    if (
        eligible
        and abs(
            sum(float(number) for number in safe_target_probabilities.values()) - 1.0
        )
        > 1e-9
    ):
        return {}
    proposed = (
        str((value.get("proposed_arm") if is_v2 else value.get("chosen_arm")) or "")
        .strip()
        .lower()
        or None
    )
    if proposed is not None and proposed not in eligible:
        return {}
    proposed_probability = (
        safe_target_probabilities.get(proposed) if proposed is not None else None
    )
    actual = (
        str(value.get("actual_arm") or "").strip().lower() or None if is_v2 else None
    )
    if actual is not None and actual not in {
        "news",
        "music",
        "meme",
        "vision",
        "video",
        "chat",
        "mini_game",
    }:
        return {}
    raw_behavior = value.get("behavior_action_probabilities") if is_v2 else {}
    if not isinstance(raw_behavior, Mapping):
        return {}
    behavior_present = bool(raw_behavior)
    safe_behavior = (
        {
            arm: _bounded_optional_number(raw_behavior.get(arm), lower=0.0, upper=1.0)
            for arm in eligible
        }
        if behavior_present
        else {}
    )
    if behavior_present and any(number is None for number in safe_behavior.values()):
        return {}
    if (
        behavior_present
        and abs(sum(float(number) for number in safe_behavior.values()) - 1.0) > 1e-9
    ):
        return {}
    actual_probability = (
        safe_behavior.get(actual)
        if actual is not None and actual in safe_behavior and behavior_present
        else None
    )
    if is_v2 and value.get("actual_action_probability") is not None:
        logged_actual_probability = _bounded_optional_number(
            value.get("actual_action_probability"), lower=0.0, upper=1.0
        )
        if (
            logged_actual_probability is None
            or actual_probability is None
            or abs(float(logged_actual_probability) - float(actual_probability)) > 1e-9
        ):
            return {}
    policy_applied = is_v2 and value.get("policy_applied") is True
    proposed_candidate_id = (
        str(
            (
                value.get("proposed_candidate_id")
                if is_v2
                else value.get("chosen_candidate_id")
            )
            or ""
        )[:160]
        or None
    )
    actual_candidate_id = (
        str(value.get("actual_candidate_id") or "")[:160] or None if is_v2 else None
    )
    attribution_basis = (
        str(value.get("arm_attribution_basis") or "").strip().lower() or None
        if context_version == "source-context-v4"
        else None
    )
    if context_version == "source-context-v4":
        if attribution_basis not in {
            None,
            "confirmed_material",
            "applied_active_bias",
            "applied_canary_policy",
        }:
            return {}
        if bool(actual) != bool(attribution_basis):
            return {}
    if policy_applied and not (
        mode == "canary"
        and actual == proposed
        and actual_candidate_id
        and actual_candidate_id == proposed_candidate_id
        and behavior_present
    ):
        return {}
    raw_baseline_scores = (
        value.get("arm_baseline_scores") if is_v2 else value.get("arm_scores")
    )
    raw_policy_scores = (
        value.get("arm_policy_scores") if is_v2 else value.get("arm_scores")
    )
    arm_baseline_scores = {
        arm: _bounded_optional_number(
            raw_baseline_scores.get(arm)
            if isinstance(raw_baseline_scores, Mapping)
            else None,
            lower=0.0,
            upper=1.0,
        )
        for arm in eligible
    }
    arm_policy_scores = {
        arm: _bounded_optional_number(
            raw_policy_scores.get(arm)
            if isinstance(raw_policy_scores, Mapping)
            else None,
            lower=0.0,
            upper=1.0,
        )
        for arm in eligible
    }
    if any(number is None for number in arm_policy_scores.values()) or (
        is_v2 and any(number is None for number in arm_baseline_scores.values())
    ):
        return {}
    arm_posteriors = _sanitize_policy_posteriors(value.get("arm_posteriors"), eligible)
    arm_bandit_posteriors = (
        _sanitize_policy_posteriors(value.get("arm_bandit_posteriors"), eligible)
        if context_version in {"source-context-v3", "source-context-v4"}
        else arm_posteriors
    )
    arm_preference_posteriors = (
        _sanitize_policy_posteriors(value.get("arm_preference_posteriors"), eligible)
        if context_version in {"source-context-v3", "source-context-v4"}
        else arm_posteriors
    )
    return {
        "policy_id": "source_epsilon_greedy_v1",
        "mode": mode,
        "score_contract": (
            str(value.get("score_contract") or "")[:80]
            if is_v2
            else "legacy-candidate-score-v1"
        ),
        "eligible_arms": eligible,
        "near_tie_arms": [
            arm
            for arm in _clean_string_list(value.get("near_tie_arms"))
            if arm in eligible
        ],
        "proposed_arm": proposed,
        "proposed_candidate_id": proposed_candidate_id,
        "actual_arm": actual,
        "actual_candidate_id": actual_candidate_id,
        "arm_attribution_basis": attribution_basis,
        "policy_applied": policy_applied,
        "chosen_arm": proposed,
        "chosen_candidate_id": proposed_candidate_id,
        "exploit_arm": str(value.get("exploit_arm") or "").strip().lower() or None,
        "target_action_probabilities": safe_target_probabilities,
        "behavior_action_probabilities": safe_behavior if behavior_present else {},
        "actual_action_probability": actual_probability,
        "action_probabilities": safe_target_probabilities,
        "chosen_action_probability": proposed_probability,
        "exploration_eligible": value.get("exploration_eligible") is True,
        "explored": value.get("explored") is True,
        "context_version": context_version,
        "arm_baseline_scores": arm_baseline_scores,
        "arm_policy_scores": arm_policy_scores,
        "arm_scores": arm_policy_scores,
        "arm_posteriors": arm_posteriors,
        "arm_bandit_posteriors": arm_bandit_posteriors,
        "arm_preference_posteriors": arm_preference_posteriors,
        "fallback_reason": str(value.get("fallback_reason") or "")[:120] or None,
    }


def _sanitize_policy_posteriors(
    value: Any,
    eligible: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for arm in eligible:
        bucket = value.get(arm) if isinstance(value, Mapping) else None
        if not isinstance(bucket, Mapping):
            bucket = {}
        result[arm] = {
            "alpha": _bounded_optional_number(
                bucket.get("alpha"), lower=0.0, upper=1_000_002.0
            ),
            "beta": _bounded_optional_number(
                bucket.get("beta"), lower=0.0, upper=1_000_002.0
            ),
            "mean": _bounded_optional_number(bucket.get("mean"), lower=0.0, upper=1.0),
            "evidence": _bounded_optional_number(
                bucket.get("evidence"), lower=0.0, upper=1_000_000.0
            ),
        }
    return result


def _sanitize_legacy_feedback_state_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve safe historical v1 snapshots without migrating their semantics."""
    temporary = value.get("temporary")
    persistent = value.get("persistent")
    if not isinstance(temporary, Mapping) or not isinstance(persistent, Mapping):
        return {}
    return {
        "version": "feedback_state_preview_v1",
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "temporary": {
            "ttl_seconds": _bounded_nonnegative_int(temporary.get("ttl_seconds")),
            "sources": _sanitize_feedback_state_sources(
                temporary.get("sources"),
                persistent=False,
            ),
        },
        "persistent": {
            "min_explicit_evidence": _bounded_nonnegative_int(
                persistent.get("min_explicit_evidence")
            ),
            "sources": _sanitize_feedback_state_sources(
                persistent.get("sources"),
                persistent=True,
            ),
        },
    }


def _sanitize_feedback_state_bucket(
    value: Mapping[str, Any],
    *,
    persistent: bool,
    score_key: str = "affinity_preview",
) -> dict[str, Any]:
    bucket = {
        "positive_evidence_count": _bounded_nonnegative_int(
            value.get("positive_evidence_count")
        ),
        "negative_evidence_count": _bounded_nonnegative_int(
            value.get("negative_evidence_count")
        ),
    }
    if persistent:
        bucket["updated_at"] = _bounded_optional_number(
            value.get("updated_at"),
            lower=0.0,
            upper=32_503_680_000.0,
        )
        bucket[score_key] = _bounded_optional_number(
            value.get(score_key),
            lower=-1.0,
            upper=1.0,
        )
    else:
        bucket["interest_preview"] = _bounded_optional_number(
            value.get("interest_preview"),
            lower=-1.0,
            upper=1.0,
        )
        bucket["expires_in_seconds"] = _bounded_optional_number(
            value.get("expires_in_seconds"),
            lower=0.0,
            upper=86_400.0,
        )
    return bucket


def _sanitize_feedback_state_sources(
    value: Any,
    *,
    persistent: bool,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, dict[str, Any]] = {}
    for raw_source, raw_bucket in list(value.items())[:16]:
        source = str(raw_source or "").strip().lower()
        if not source.replace("_", "").isalnum() or not isinstance(raw_bucket, Mapping):
            continue
        safe[source] = _sanitize_feedback_state_bucket(
            raw_bucket,
            persistent=persistent,
        )
    return safe


def sanitize_recommendation_review_context(value: Any) -> dict[str, Any]:
    """Return the bounded, URL-free context allowed for human review."""
    if not isinstance(value, Mapping):
        return {}

    notes = set(_clean_string_list(value.get("redaction_notes")))
    labels: list[dict[str, Any]] = []
    raw_labels = value.get("candidate_labels")
    if isinstance(raw_labels, Sequence) and not isinstance(raw_labels, (str, bytes)):
        for raw in raw_labels[:REVIEW_CONTEXT_MAX_CANDIDATES]:
            if not isinstance(raw, Mapping):
                continue
            candidate_id, id_notes = _sanitize_review_text(
                raw.get("id"), max_length=128
            )
            source_type, source_notes = _sanitize_review_text(
                raw.get("source_type"), max_length=32
            )
            safe_title, title_notes = _sanitize_review_text(
                raw.get("safe_title"),
                max_length=REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH,
            )
            safe_summary, summary_notes = _sanitize_review_text(
                raw.get("safe_summary"),
                max_length=REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH,
            )
            notes.update(id_notes + source_notes + title_notes + summary_notes)
            if not candidate_id or not source_type:
                continue
            labels.append(
                {
                    "id": candidate_id,
                    "source_type": source_type,
                    "safe_title": safe_title,
                    "safe_summary": safe_summary,
                    "score": round(_number(raw.get("score"), 0.0), 3),
                }
            )

    activity_state, activity_notes = _sanitize_review_text(
        value.get("activity_state"),
        max_length=48,
    )
    delivered_excerpt, excerpt_notes = _sanitize_review_text(
        value.get("delivered_excerpt"),
        max_length=REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH,
    )
    notes.update(activity_notes + excerpt_notes)

    return {
        "schema_version": 1,
        "candidate_labels": labels,
        "activity_state": activity_state or "unknown",
        "delivered_excerpt": delivered_excerpt,
        "redaction_notes": sorted(notes)[:REVIEW_CONTEXT_REDACTION_NOTES_MAX],
    }


def _sanitize_review_text(value: Any, *, max_length: int) -> tuple[str, list[str]]:
    text = " ".join(str(value or "").split())
    notes: list[str] = []
    if _REVIEW_URL_RE.search(text):
        text = _REVIEW_URL_RE.sub("", text)
        notes.append("url_removed")
    if _REVIEW_SECRET_RE.search(text):
        text = _REVIEW_SECRET_RE.sub("[redacted]", text)
        notes.append("secret_redacted")
    text = " ".join(text.split())
    if len(text) > max_length:
        text = text[:max_length].rstrip()
        notes.append("text_truncated")
    return text, notes


def _sanitize_top_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        clean = {
            key: _json_safe_scalar(item.get(key))
            for key in _TOP_CANDIDATE_KEYS
            if key in item
        }
        # Candidate topics may contain personal dynamics, window titles, or
        # other user-derived context. Preserve only the low-quality diagnostic
        # signal, never the topic text itself.
        if "topic_usable" not in clean and "topic" in item:
            clean["topic_usable"] = len(str(item.get("topic") or "").strip()) >= 4
        if clean:
            out.append(clean)
    return out


def _json_safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe_scalar(item) for item in value]
    return str(value)


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bounded_optional_number(
    value: Any,
    *,
    lower: float,
    upper: float,
) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(max(lower, min(upper, number)), 3)


def _bounded_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(1_000_000, number))
