"""Local observation sink for proactive recommendation shadow decisions.

This module deliberately handles only sanitized diagnostics. It does not fetch
sources, deliver messages, call LLMs, or alter proactive chat behavior.
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
import json
import logging
import os
from pathlib import Path
import math
import re
import time
from typing import Any


logger = logging.getLogger("N.E.K.O.Main.proactive_recommendation_observer")

OBSERVATION_LOG_FILENAME = "proactive_recommendation_observations.jsonl"
DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024
DEFAULT_HIGH_SCORE_THRESHOLD = 0.75
DEFAULT_EXAMPLE_LIMIT = 10
MAX_EXAMPLE_LIMIT = 20
CALIBRATION_WINDOW_SECONDS = 3600
CALIBRATION_SAMPLE_LIMIT = 50
ACTIVE_READY_MIN_SAMPLE_COUNT = 30
ACTIVE_READY_SOURCE_MATCH_RATE = 0.75
ACTIVE_READY_MATERIAL_MATCH_RATE = 0.65
ACTIVE_READY_AVERAGE_RANK = 1.8
ACTIVE_READY_PASS_HIGH_SCORE_RATE = 0.15
VALIDATION_SOURCE_OVERUSE_RATE = 0.6
VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT = 5
VALIDATION_CANDIDATE_OVERUSE_RATE = 0.35
VALIDATION_CANDIDATE_OVERUSE_MIN_SAMPLE_COUNT = 5
VALIDATION_EXAMPLE_LIMIT_PER_ISSUE = 3
REVIEW_CONTEXT_MAX_CANDIDATES = 3
REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH = 96
REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH = 240
REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH = 160
REVIEW_CONTEXT_REDACTION_NOTES_MAX = 8

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
_EXAMPLE_KEYS = {
    "turn_id",
    "ts",
    "decision_stage",
    "shadow_selected_source_type",
    "actual_primary_channel",
    "actual_rank",
    "top_candidates",
    "review_context",
}
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
_REVIEW_FORBIDDEN_KEYS = {
    "payload",
    "source_links",
    "raw_data",
    "screenshot",
    "screenshot_b64",
    "screen_text",
    "window_title",
    "chat_text",
    "raw_text",
    "messages",
    "prompt",
    "token",
    "cookie",
    "authorization",
    "url",
    "uri",
}
_REVIEW_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_REVIEW_SECRET_RE = re.compile(
    r"\b(token|cookie|authorization|api[_-]?key|session[_-]?id)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)


def sanitize_recommendation_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact, file-safe observation shape used by JSONL logging."""
    safe: dict[str, Any] = {}
    for key in _TOP_LEVEL_KEYS:
        if key not in observation:
            continue
        if key == "top_candidates":
            safe[key] = _sanitize_top_candidates(observation.get(key))
        elif key == "review_context":
            review_context = sanitize_recommendation_review_context(observation.get(key))
            if review_context:
                safe[key] = review_context
        elif key == "decision_context":
            decision_context = sanitize_recommendation_decision_context(observation.get(key))
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
    if not isinstance(conversation, Mapping) or not isinstance(source_affinity, Mapping):
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
                    "baseline_rank": _bounded_nonnegative_int(
                        row.get("baseline_rank")
                    ),
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
        ).strip().lower()[:32]
        or None,
        "personalized_selected_candidate_id": str(
            value.get("personalized_selected_candidate_id") or ""
        ).strip()[:160]
        or None,
        "personalized_selected_source_type": str(
            value.get("personalized_selected_source_type") or ""
        ).strip().lower()[:32]
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
    prior = value.get("beta_prior") if isinstance(value.get("beta_prior"), Mapping) else {}
    return {
        "version": "recommendation_preference_state_v1",
        "half_life_seconds": _bounded_nonnegative_int(value.get("half_life_seconds")),
        "beta_prior": {
            "alpha": _bounded_optional_number(prior.get("alpha"), lower=0.0, upper=100.0),
            "beta": _bounded_optional_number(prior.get("beta"), lower=0.0, upper=100.0),
        },
        "min_evidence": _bounded_optional_number(value.get("min_evidence"), lower=0.0, upper=1000.0),
        "saturation_evidence": _bounded_optional_number(value.get("saturation_evidence"), lower=0.0, upper=1000.0),
        "max_abs_delta": _bounded_optional_number(value.get("max_abs_delta"), lower=0.0, upper=0.03),
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
    context_version = str(
        value.get("context_version") or "source-context-v1"
    ).strip()
    is_v2 = context_version in {"source-context-v2", "source-context-v3"}
    if context_version not in {
        "source-context-v1",
        "source-context-v2",
        "source-context-v3",
    }:
        return {}
    eligible = [
        item for item in _clean_string_list(value.get("eligible_arms"))
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
    if eligible and abs(
        sum(float(number) for number in safe_target_probabilities.values()) - 1.0
    ) > 1e-9:
        return {}
    proposed = str(
        (
            value.get("proposed_arm")
            if is_v2
            else value.get("chosen_arm")
        )
        or ""
    ).strip().lower() or None
    if proposed is not None and proposed not in eligible:
        return {}
    proposed_probability = (
        safe_target_probabilities.get(proposed) if proposed is not None else None
    )
    actual = (
        str(value.get("actual_arm") or "").strip().lower() or None
        if is_v2
        else None
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
            arm: _bounded_optional_number(
                raw_behavior.get(arm), lower=0.0, upper=1.0
            )
            for arm in eligible
        }
        if behavior_present
        else {}
    )
    if behavior_present and any(
        number is None for number in safe_behavior.values()
    ):
        return {}
    if behavior_present and abs(
        sum(float(number) for number in safe_behavior.values()) - 1.0
    ) > 1e-9:
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
    proposed_candidate_id = str(
        (
            value.get("proposed_candidate_id")
            if is_v2
            else value.get("chosen_candidate_id")
        )
        or ""
    )[:160] or None
    actual_candidate_id = (
        str(value.get("actual_candidate_id") or "")[:160] or None
        if is_v2
        else None
    )
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
    arm_posteriors = _sanitize_policy_posteriors(
        value.get("arm_posteriors"), eligible
    )
    arm_bandit_posteriors = _sanitize_policy_posteriors(
        value.get("arm_bandit_posteriors"), eligible
    ) if context_version == "source-context-v3" else arm_posteriors
    arm_preference_posteriors = _sanitize_policy_posteriors(
        value.get("arm_preference_posteriors"), eligible
    ) if context_version == "source-context-v3" else arm_posteriors
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
            arm for arm in _clean_string_list(value.get("near_tie_arms"))
            if arm in eligible
        ],
        "proposed_arm": proposed,
        "proposed_candidate_id": proposed_candidate_id,
        "actual_arm": actual,
        "actual_candidate_id": actual_candidate_id,
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
            "mean": _bounded_optional_number(
                bucket.get("mean"), lower=0.0, upper=1.0
            ),
            "evidence": _bounded_optional_number(
                bucket.get("evidence"), lower=0.0, upper=1_000_000.0
            ),
        }
    return result


def summarize_recommendation_policy(
    observations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize policy exposure and propensity integrity for runtime monitoring."""
    modes: Counter[str] = Counter()
    proposals: Counter[str] = Counter()
    actuals: Counter[str] = Counter()
    policy_count = 0
    applied_count = 0
    explored_count = 0
    exploration_eligible_count = 0
    probability_violation_count = 0
    for observation in observations:
        raw_policy = observation.get("policy_decision")
        if not isinstance(raw_policy, Mapping):
            continue
        policy_count += 1
        policy = sanitize_recommendation_policy_decision(raw_policy)
        if not policy:
            probability_violation_count += 1
            continue
        modes[str(policy["mode"])] += 1
        proposed = str(policy.get("proposed_arm") or policy.get("chosen_arm") or "")
        actual = str(policy.get("actual_arm") or "")
        if proposed:
            proposals[proposed] += 1
        if actual:
            actuals[actual] += 1
        elif policy.get("context_version") == "source-context-v1" and proposed:
            actuals[proposed] += 1
        applied_count += int(policy.get("policy_applied") is True)
        exploration_eligible_count += int(
            policy.get("exploration_eligible") is True
        )
        explored_count += int(policy.get("explored") is True)
    total_choices = sum(actuals.values())
    distribution = {
        source: round(count / total_choices, 6) if total_choices else 0.0
        for source, count in sorted(actuals.items())
    }
    return {
        "policy_observation_count": policy_count,
        "valid_policy_observation_count": policy_count - probability_violation_count,
        "mode_distribution": dict(sorted(modes.items())),
        "proposed_arm_count": dict(sorted(proposals.items())),
        "actual_arm_count": dict(sorted(actuals.items())),
        "policy_applied_count": applied_count,
        "chosen_arm_count": dict(sorted(proposals.items())),
        "chosen_arm_distribution": distribution,
        "exploration_eligible_count": exploration_eligible_count,
        "explored_count": explored_count,
        "observed_exploration_rate": round(
            explored_count / exploration_eligible_count, 6
        )
        if exploration_eligible_count
        else 0.0,
        "max_source_exposure_rate": max(distribution.values(), default=0.0),
        "hhi": round(sum(value * value for value in distribution.values()), 6),
        "probability_violation_count": probability_violation_count,
        "hard_gate_pass": probability_violation_count == 0,
    }


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
            candidate_id, id_notes = _sanitize_review_text(raw.get("id"), max_length=128)
            source_type, source_notes = _sanitize_review_text(raw.get("source_type"), max_length=32)
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


def validate_recommendation_review_context(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an export before Testbench enables human relevance labels."""
    value = observation.get("review_context")
    issues: list[str] = []
    if not isinstance(value, Mapping):
        return {
            "valid": False,
            "annotation_ready": False,
            "issues": ["missing_review_context"],
        }
    if _contains_review_forbidden_fields(value):
        issues.append("review_context_forbidden_fields")
    if _contains_review_url(value):
        issues.append("review_context_url_present")

    labels = value.get("candidate_labels")
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)) or not labels:
        issues.append("review_context_candidate_labels_missing")
        labels = []
    expected = _review_candidate_identity(observation.get("top_candidates"))
    actual = _review_candidate_identity(labels)
    if actual != expected[:REVIEW_CONTEXT_MAX_CANDIDATES]:
        issues.append("review_context_candidate_alignment_mismatch")

    for raw in labels:
        if not isinstance(raw, Mapping):
            issues.append("review_context_candidate_label_invalid")
            continue
        if set(raw) - _REVIEW_CANDIDATE_LABEL_KEYS:
            issues.append("review_context_candidate_label_extra_fields")
        if len(str(raw.get("safe_title") or "")) > REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH:
            issues.append("review_context_safe_title_too_long")
        if len(str(raw.get("safe_summary") or "")) > REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH:
            issues.append("review_context_safe_summary_too_long")
    if len(str(value.get("delivered_excerpt") or "")) > REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH:
        issues.append("review_context_delivered_excerpt_too_long")
    if set(value) - _REVIEW_CONTEXT_KEYS:
        issues.append("review_context_extra_fields")

    unique_issues = sorted(set(issues))
    return {
        "valid": not unique_issues,
        "annotation_ready": bool(labels) and not unique_issues,
        "issues": unique_issues,
    }


def summarize_recommendation_review_context(
    observations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize the Testbench gate without exposing review text."""
    total = 0
    present = 0
    ready = 0
    issue_counts: Counter[str] = Counter()
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        total += 1
        if isinstance(observation.get("review_context"), Mapping):
            present += 1
        result = validate_recommendation_review_context(observation)
        if result.get("annotation_ready") is True:
            ready += 1
        for issue in result.get("issues") or ():
            issue_counts[str(issue)] += 1
    return {
        "sample_count": total,
        "review_context_present_count": present,
        "annotation_ready_count": ready,
        "annotation_blocked_count": total - ready,
        "issue_distribution": dict(sorted(issue_counts.items())),
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


def _contains_review_forbidden_fields(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _REVIEW_FORBIDDEN_KEYS:
                return True
            if _contains_review_forbidden_fields(child):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_review_forbidden_fields(child) for child in value)
    return False


def _contains_review_url(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_review_url(child) for child in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_review_url(child) for child in value)
    return bool(_REVIEW_URL_RE.search(str(value or "")))


def _review_candidate_identity(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out: list[tuple[str, str]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        candidate_id = str(row.get("id") or "").strip()
        source_type = str(row.get("source_type") or "").strip()
        if candidate_id and source_type:
            out.append((candidate_id, source_type))
    return out


def append_recommendation_observation_jsonl(
    observation: Mapping[str, Any],
    *,
    log_mode: str = "off",
    path: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    rotate_bytes: int = DEFAULT_ROTATE_BYTES,
) -> bool:
    """Append one sanitized observation to a local JSONL file when enabled."""
    if log_mode != "jsonl":
        return False
    target = _resolve_observation_path(path=path, config_dir=config_dir)
    if target is None:
        return False
    try:
        safe = sanitize_recommendation_observation(observation)
        if not str(safe.get("turn_id") or "").strip():
            logger.debug("proactive recommendation observation rejected: missing turn_id")
            return False
        if not str(safe.get("algorithm_version") or "").strip():
            logger.debug("proactive recommendation observation rejected: missing algorithm_version")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target, rotate_bytes=rotate_bytes)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        logger.debug("proactive recommendation observation append failed: %s", exc)
        return False


def load_recommendation_observations_jsonl(
    path: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read observations from JSONL, returning the newest ``limit`` rows."""
    target = Path(path)
    if not target.exists():
        return []
    rows: deque[dict[str, Any]] | list[dict[str, Any]]
    rows = deque(maxlen=limit) if limit and limit > 0 else []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, Mapping):
                    rows.append(sanitize_recommendation_observation(item))
    except Exception as exc:
        logger.debug("proactive recommendation observation read failed: %s", exc)
        return []
    return list(rows)


def summarize_recommendation_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    limit: int | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
) -> dict[str, Any]:
    """Aggregate shadow-observation quality metrics for local diagnostics."""
    rows = [
        sanitize_recommendation_observation(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    if limit and limit > 0:
        rows = rows[-limit:]

    total = len(rows)
    delivered = [row for row in rows if row.get("delivered") is True]
    passes = [row for row in rows if row.get("delivered") is not True]
    ranks = [
        int(row["actual_rank"])
        for row in delivered
        if isinstance(row.get("actual_rank"), int)
    ]
    top1_sources = Counter(
        _top1_source_type(row)
        for row in rows
        if _top1_source_type(row)
    )
    stage_counts = Counter(
        str(row.get("decision_stage") or "unknown")
        for row in rows
    )
    pass_high_score_count = sum(
        1
        for row in passes
        if _number(row.get("shadow_selected_score"), 0.0) >= high_score_threshold
    )

    return {
        "total": total,
        "delivered_count": len(delivered),
        "pass_count": len(passes),
        "source_match_rate": _rate(
            sum(1 for row in delivered if row.get("matched_actual_source") is True),
            len(delivered),
        ),
        "material_match_rate": _rate(
            sum(1 for row in delivered if row.get("matched_actual_material") is True),
            len(delivered),
        ),
        "average_actual_rank": round(sum(ranks) / len(ranks), 3) if ranks else None,
        "shadow_top1_by_source_type": dict(sorted(top1_sources.items())),
        "decision_stage_counts": dict(sorted(stage_counts.items())),
        "pass_high_score_count": pass_high_score_count,
        "high_score_threshold": float(high_score_threshold),
    }


def get_recommendation_calibration_samples(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    window_seconds: int = CALIBRATION_WINDOW_SECONDS,
    sample_limit: int = CALIBRATION_SAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Return sanitized observations in the current calibration window."""
    current = time.time() if now is None else float(now)
    window = max(0, int(window_seconds))
    limit = max(0, int(sample_limit))
    rows = [
        sanitize_recommendation_observation(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    recent = [
        row
        for row in rows
        if _is_recent_observation(row, now=current, window_seconds=window)
    ]
    if limit <= 0:
        return []
    return recent[-limit:]


def summarize_recommendation_calibration(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    window_seconds: int = CALIBRATION_WINDOW_SECONDS,
    sample_limit: int = CALIBRATION_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Summarize whether shadow ranking is stable enough to discuss active mode."""
    samples = get_recommendation_calibration_samples(
        observations,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    summary = summarize_recommendation_observations(
        samples,
        high_score_threshold=high_score_threshold,
    )
    sample_count = summary["total"]
    pass_high_score_rate = _rate(summary["pass_high_score_count"], sample_count)

    issues: list[str] = []
    reasons: list[str] = []
    if sample_count < ACTIVE_READY_MIN_SAMPLE_COUNT:
        issues.append("low_sample_count")
        reasons.append("sample_count_below_threshold")

    source_match_rate = summary["source_match_rate"]
    if source_match_rate is None or source_match_rate < ACTIVE_READY_SOURCE_MATCH_RATE:
        issues.append("source_selection_drift")
        reasons.append("source_match_rate_below_threshold")

    material_match_rate = summary["material_match_rate"]
    if material_match_rate is None or material_match_rate < ACTIVE_READY_MATERIAL_MATCH_RATE:
        issues.append("material_ranking_drift")
        reasons.append("material_match_rate_below_threshold")

    average_rank = summary["average_actual_rank"]
    if average_rank is None or average_rank > ACTIVE_READY_AVERAGE_RANK:
        issues.append("ranking_order_drift")
        reasons.append("average_actual_rank_above_threshold")

    if pass_high_score_rate is not None and pass_high_score_rate >= ACTIVE_READY_PASS_HIGH_SCORE_RATE:
        issues.append("pass_gate_conflict")
        reasons.append("pass_high_score_rate_above_threshold")

    return {
        "sample_count": sample_count,
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "source_match_rate": source_match_rate,
        "material_match_rate": material_match_rate,
        "average_actual_rank": average_rank,
        "pass_high_score_count": summary["pass_high_score_count"],
        "pass_high_score_rate": pass_high_score_rate,
        "active_ready": not issues,
        "active_ready_reasons": reasons,
        "calibration_issues": issues,
        "thresholds": {
            "min_sample_count": ACTIVE_READY_MIN_SAMPLE_COUNT,
            "source_match_rate": ACTIVE_READY_SOURCE_MATCH_RATE,
            "material_match_rate": ACTIVE_READY_MATERIAL_MATCH_RATE,
            "average_actual_rank": ACTIVE_READY_AVERAGE_RANK,
            "pass_high_score_rate": ACTIVE_READY_PASS_HIGH_SCORE_RATE,
            "high_score_threshold": float(high_score_threshold),
        },
        "summary": summary,
    }


def summarize_recommendation_validation(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    window_seconds: int = CALIBRATION_WINDOW_SECONDS,
    sample_limit: int = CALIBRATION_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Classify shadow recommendation mismatches for manual rule calibration."""
    samples = get_recommendation_calibration_samples(
        observations,
        now=now,
        window_seconds=window_seconds,
        sample_limit=sample_limit,
    )
    summary = summarize_recommendation_observations(
        samples,
        high_score_threshold=high_score_threshold,
    )
    delivered = [row for row in samples if row.get("delivered") is True]
    source_drift = [
        row
        for row in delivered
        if row.get("matched_actual_source") is False
    ]
    material_drift = [
        row
        for row in delivered
        if (
            row.get("matched_actual_source") is True
            and (
                row.get("matched_actual_material") is False
                or _actual_rank(row) > 1
            )
        )
    ]
    pass_conflict = [
        row
        for row in samples
        if (
            row.get("delivered") is not True
            and _number(row.get("shadow_selected_score"), 0.0) >= high_score_threshold
        )
    ]
    low_quality_top1 = [
        row
        for row in samples
        if _is_low_quality_top1(row)
    ]
    top1_counts = Counter(
        _top1_source_type(row)
        for row in samples
        if _top1_source_type(row)
    )
    top1_candidate_counts = Counter(
        _top1_candidate_id(row)
        for row in samples
        if _top1_candidate_id(row)
    )
    dominant_source_type = ""
    dominant_source_count = 0
    if top1_counts:
        dominant_source_type, dominant_source_count = top1_counts.most_common(1)[0]
    dominant_source_rate = _rate(dominant_source_count, len(samples))
    source_overuse = bool(
        len(samples) >= VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT
        and dominant_source_type
        and dominant_source_rate is not None
        and dominant_source_rate >= VALIDATION_SOURCE_OVERUSE_RATE
    )
    dominant_candidate_id = ""
    dominant_candidate_count = 0
    if top1_candidate_counts:
        dominant_candidate_id, dominant_candidate_count = top1_candidate_counts.most_common(1)[0]
    dominant_candidate_rate = _rate(dominant_candidate_count, len(samples))
    candidate_overuse = bool(
        len(samples) >= VALIDATION_CANDIDATE_OVERUSE_MIN_SAMPLE_COUNT
        and dominant_candidate_id
        and dominant_candidate_rate is not None
        and dominant_candidate_rate >= VALIDATION_CANDIDATE_OVERUSE_RATE
    )

    issue_counts = {
        "source_drift": len(source_drift),
        "material_drift": len(material_drift),
        "pass_conflict": len(pass_conflict),
        "source_overuse": 1 if source_overuse else 0,
        "candidate_overuse": 1 if candidate_overuse else 0,
        "low_quality_top1": len(low_quality_top1),
    }
    issues = [
        issue
        for issue, count in issue_counts.items()
        if count > 0
    ]

    return {
        "sample_count": len(samples),
        "sample_window_seconds": int(window_seconds),
        "sample_limit": int(sample_limit),
        "issues": issues,
        "issue_counts": issue_counts,
        "rates": {
            "source_drift": _rate(len(source_drift), len(delivered)),
            "material_drift": _rate(len(material_drift), len(delivered)),
            "pass_conflict": _rate(len(pass_conflict), len(samples)),
            "source_overuse": dominant_source_rate if source_overuse else 0.0,
            "candidate_overuse": dominant_candidate_rate if candidate_overuse else 0.0,
            "low_quality_top1": _rate(len(low_quality_top1), len(samples)),
        },
        "dominant_source_type": dominant_source_type or None,
        "dominant_source_rate": dominant_source_rate,
        "dominant_candidate_id": dominant_candidate_id or None,
        "dominant_candidate_rate": dominant_candidate_rate,
        "suggested_weight_adjustments": _suggested_weight_adjustments(
            issues,
            dominant_source_type=dominant_source_type,
        ),
        "examples": {
            "source_drift": _validation_examples(source_drift),
            "material_drift": _validation_examples(material_drift),
            "pass_conflict": _validation_examples(pass_conflict),
            "source_overuse": _validation_examples(
                [
                    row
                    for row in samples
                    if source_overuse and _top1_source_type(row) == dominant_source_type
                ]
            ),
            "candidate_overuse": _validation_examples(
                [
                    row
                    for row in samples
                    if candidate_overuse and _top1_candidate_id(row) == dominant_candidate_id
                ]
            ),
            "low_quality_top1": _validation_examples(low_quality_top1),
        },
        "summary": summary,
        "thresholds": {
            "high_score_threshold": float(high_score_threshold),
            "source_overuse_rate": VALIDATION_SOURCE_OVERUSE_RATE,
            "source_overuse_min_sample_count": VALIDATION_SOURCE_OVERUSE_MIN_SAMPLE_COUNT,
            "candidate_overuse_rate": VALIDATION_CANDIDATE_OVERUSE_RATE,
            "candidate_overuse_min_sample_count": VALIDATION_CANDIDATE_OVERUSE_MIN_SAMPLE_COUNT,
        },
    }


def select_recommendation_observation_examples(
    observations: Iterable[Mapping[str, Any]],
    *,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Return compact diagnostic examples, prioritizing mismatches and high-score passes."""
    rows = [
        sanitize_recommendation_observation(row)
        for row in observations
        if isinstance(row, Mapping)
    ]
    example_limit = max(0, min(int(limit), MAX_EXAMPLE_LIMIT))
    if example_limit <= 0:
        return []

    def priority(row: Mapping[str, Any]) -> tuple[int, float]:
        mismatch = row.get("delivered") is True and row.get("matched_actual_material") is False
        pass_high_score = (
            row.get("delivered") is not True
            and _number(row.get("shadow_selected_score"), 0.0) >= high_score_threshold
        )
        if mismatch:
            group = 0
        elif pass_high_score:
            group = 1
        else:
            group = 2
        return (group, -_number(row.get("ts"), 0.0))

    selected = sorted(rows, key=priority)[:example_limit]
    return [_example_from_observation(row) for row in selected]


def _resolve_observation_path(
    *,
    path: str | os.PathLike[str] | None,
    config_dir: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        return Path(path)
    if config_dir is None:
        return None
    return Path(config_dir) / OBSERVATION_LOG_FILENAME


def _rotate_if_needed(path: Path, *, rotate_bytes: int) -> None:
    if rotate_bytes <= 0:
        return
    try:
        if path.exists() and path.stat().st_size > rotate_bytes:
            os.replace(path, path.parent / (path.name + ".1"))
    except OSError as exc:
        logger.debug("proactive recommendation observation rotate failed: %s", exc)


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


def _top1_source_type(row: Mapping[str, Any]) -> str:
    candidates = row.get("top_candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return ""
    if not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, Mapping):
        return ""
    return str(first.get("source_type") or "").strip()


def _top1_candidate(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidates = row.get("top_candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return None
    if not candidates:
        return None
    first = candidates[0]
    return first if isinstance(first, Mapping) else None


def _top1_candidate_id(row: Mapping[str, Any]) -> str:
    top = _top1_candidate(row)
    if top is None:
        return ""
    return str(top.get("id") or "").strip()


def _actual_rank(row: Mapping[str, Any]) -> int:
    value = row.get("actual_rank")
    return int(value) if isinstance(value, int) else 0


def _is_low_quality_top1(row: Mapping[str, Any]) -> bool:
    if row.get("candidate_count") == 0:
        return False
    top = _top1_candidate(row)
    if top is None:
        return True
    source_type = str(top.get("source_type") or "").strip()
    candidate_id = str(top.get("id") or "").strip()
    score = _number(top.get("score"), -1.0)
    topic_usable = top.get("topic_usable") is True
    return not source_type or not candidate_id or not topic_usable or score < 0.2


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _is_recent_observation(
    row: Mapping[str, Any],
    *,
    now: float,
    window_seconds: int,
) -> bool:
    ts = _number(row.get("ts"), -1.0)
    if ts < 0:
        return False
    return 0 <= now - ts <= window_seconds


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


def _example_from_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    example = {
        key: row.get(key)
        for key in _EXAMPLE_KEYS
        if key in row
    }
    if "actual_reason_code" in row:
        example["reason_code"] = row.get("actual_reason_code")
    return example


def _validation_examples(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        _example_from_observation(row)
        for row in rows[:VALIDATION_EXAMPLE_LIMIT_PER_ISSUE]
    ]


def _suggested_weight_adjustments(
    issues: Sequence[str],
    *,
    dominant_source_type: str,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    issue_set = set(issues)
    if "pass_conflict" in issue_set:
        suggestions.append(
            {
                "target": "interruption_cost",
                "adjustment": "+0.05",
                "reason": "pass_conflict",
            }
        )
    if "material_drift" in issue_set:
        suggestions.append(
            {
                "target": "source_quality",
                "adjustment": "+0.05",
                "reason": "material_drift",
            }
        )
    if "source_drift" in issue_set:
        suggestions.append(
            {
                "target": "context_match",
                "adjustment": "+0.05",
                "reason": "source_drift",
            }
        )
    if "source_overuse" in issue_set or "candidate_overuse" in issue_set:
        suggestions.append(
            {
                "target": "diversity_penalty",
                "adjustment": "+0.05",
                "reason": (
                    "candidate_overuse"
                    if "candidate_overuse" in issue_set
                    else "source_overuse"
                ),
            }
        )
    if "source_overuse" in issue_set and dominant_source_type:
        suggestions.append(
            {
                "target": f"source_type.{dominant_source_type}",
                "adjustment": "-0.05",
                "reason": "source_overuse",
            }
        )
        if dominant_source_type == "music":
            suggestions.append(
                {
                    "target": "music.novelty",
                    "adjustment": "+0.05",
                    "reason": "source_overuse",
                }
            )
    if "low_quality_top1" in issue_set and "material_drift" not in issue_set:
        suggestions.append(
            {
                "target": "source_quality",
                "adjustment": "+0.05",
                "reason": "low_quality_top1",
            }
        )
    return suggestions
