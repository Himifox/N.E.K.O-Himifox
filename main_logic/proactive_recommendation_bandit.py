"""Constrained source-level bandit for safe proactive material candidates."""
from __future__ import annotations

from collections.abc import Mapping
import math
import random
from typing import Any


BANDIT_POLICY_ID = "source_epsilon_greedy_v1"
BANDIT_CONTEXT_VERSION = "source-context-v4"
BANDIT_BASELINE_SCORE_CONTRACT = "baseline-score-v1"
BANDIT_PERSONALIZED_SCORE_CONTRACT = "personalized-policy-score-v1"
BANDIT_ARMS = ("news", "music", "meme")
BANDIT_EPSILON = 0.05
BANDIT_NEAR_TIE_GAP = 0.03
VALID_BANDIT_MODES = frozenset({"off", "shadow", "canary"})


def build_source_bandit_decision(
    recommendation_decision: Any,
    *,
    mode: Any,
    preference_state: Mapping[str, Any] | None = None,
    bandit_state: Mapping[str, Any] | None = None,
    score_contract: Any = BANDIT_BASELINE_SCORE_CONTRACT,
    random_value: float | None = None,
    random_arm_value: float | None = None,
) -> dict[str, Any]:
    """Choose one safe source arm without adding or unfiltering candidates."""
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in VALID_BANDIT_MODES:
        normalized_mode = "off"
    normalized_score_contract = _score_contract(score_contract)
    ranked = tuple(
        candidate
        for candidate in getattr(recommendation_decision, "ranked_candidates", ())
        if _finite(getattr(candidate, "score", None)) is not None
    )
    if not ranked or _arm_name(getattr(ranked[0], "source_type", None)) not in BANDIT_ARMS:
        return _empty_decision(
            normalized_mode,
            score_contract=normalized_score_contract,
            fallback_reason="top_source_not_bandit_arm" if ranked else "no_candidate",
        )
    arm_candidates = _best_candidate_per_arm(
        recommendation_decision,
        score_contract=normalized_score_contract,
    )
    eligible = sorted(
        arm_candidates,
        key=lambda arm: (-float(arm_candidates[arm]["policy_score"]), arm),
    )
    if not eligible:
        return _empty_decision(
            normalized_mode,
            score_contract=normalized_score_contract,
            fallback_reason="no_bandit_arm",
        )

    score_leader = eligible[0]
    top_score = float(arm_candidates[score_leader]["policy_score"])
    near_tie = [
        arm
        for arm in eligible
        if top_score - float(arm_candidates[arm]["policy_score"])
        <= BANDIT_NEAR_TIE_GAP + 1e-12
    ]
    bandit_posteriors = _arm_posteriors(eligible, bandit_state, bucket_key="arms")
    preference_posteriors = _arm_posteriors(
        eligible, preference_state, bucket_key="sources"
    )
    exploit_arm = score_leader
    if normalized_mode in {"shadow", "canary"}:
        exploit_arm = sorted(
            near_tie,
            key=lambda arm: (
                -float(bandit_posteriors[arm]["mean"]),
                -float(arm_candidates[arm]["policy_score"]),
                arm,
            ),
        )[0]
    exploration_eligible = normalized_mode in {"shadow", "canary"} and len(near_tie) >= 2
    probabilities = {arm: 0.0 for arm in eligible}
    if exploration_eligible:
        share = BANDIT_EPSILON / len(near_tie)
        for arm in near_tie:
            probabilities[arm] = share
        probabilities[exploit_arm] += 1.0 - BANDIT_EPSILON
    else:
        probabilities[exploit_arm] = 1.0

    chosen_arm = exploit_arm
    explored = False
    if exploration_eligible:
        draw = random.SystemRandom().random() if random_value is None else _unit(random_value)
        if draw < BANDIT_EPSILON:
            arm_draw = (
                random.SystemRandom().random()
                if random_arm_value is None
                else _unit(random_arm_value)
            )
            chosen_arm = near_tie[min(int(arm_draw * len(near_tie)), len(near_tie) - 1)]
            explored = chosen_arm != exploit_arm

    return {
        "policy_id": BANDIT_POLICY_ID,
        "mode": normalized_mode,
        "score_contract": normalized_score_contract,
        "eligible_arms": eligible,
        "near_tie_arms": near_tie,
        "proposed_arm": chosen_arm,
        "proposed_candidate_id": arm_candidates[chosen_arm]["candidate_id"],
        "actual_arm": None,
        "actual_candidate_id": None,
        "policy_applied": False,
        "chosen_arm": chosen_arm,
        "chosen_candidate_id": arm_candidates[chosen_arm]["candidate_id"],
        "exploit_arm": exploit_arm,
        "target_action_probabilities": {
            arm: round(probabilities[arm], 12) for arm in eligible
        },
        "behavior_action_probabilities": {},
        "actual_action_probability": None,
        "action_probabilities": {
            arm: round(probabilities[arm], 12) for arm in eligible
        },
        "chosen_action_probability": round(probabilities[chosen_arm], 12),
        "exploration_eligible": exploration_eligible,
        "explored": explored,
        "context_version": BANDIT_CONTEXT_VERSION,
        "arm_baseline_scores": {
            arm: round(float(arm_candidates[arm]["baseline_score"]), 6)
            for arm in eligible
        },
        "arm_policy_scores": {
            arm: round(float(arm_candidates[arm]["policy_score"]), 6)
            for arm in eligible
        },
        "arm_scores": {
            arm: round(float(arm_candidates[arm]["policy_score"]), 6)
            for arm in eligible
        },
        "arm_posteriors": bandit_posteriors,
        "arm_bandit_posteriors": bandit_posteriors,
        "arm_preference_posteriors": preference_posteriors,
        "fallback_reason": None,
    }


def bandit_preferred_candidate(
    recommendation_decision: Any,
    policy_decision: Mapping[str, Any] | None,
) -> Any | None:
    candidate_id = str((policy_decision or {}).get("chosen_candidate_id") or "")
    for candidate in getattr(recommendation_decision, "ranked_candidates", ()):
        if str(getattr(candidate, "id", "")) == candidate_id:
            return candidate
    return None


def finalize_source_bandit_decision(
    policy_decision: Mapping[str, Any] | None,
    *,
    actual_candidate: Any = None,
    attribution_basis: str | None = None,
    delivered: bool,
) -> dict[str, Any] | None:
    """Bind a proposed policy action to the material that was actually delivered."""
    if not isinstance(policy_decision, Mapping):
        return None
    result = dict(policy_decision)
    eligible = [str(arm) for arm in result.get("eligible_arms", ()) if str(arm)]
    actual_arm = (
        _arm_name(getattr(actual_candidate, "source_type", None))
        if delivered and actual_candidate is not None
        else None
    )
    actual_candidate_id = (
        str(getattr(actual_candidate, "id", "") or "")
        if actual_arm is not None
        else ""
    )
    actual_attribution_basis = (
        str(attribution_basis or "confirmed_material").strip().lower()
        if actual_arm is not None
        else None
    )
    proposed_arm = str(
        result.get("proposed_arm") or result.get("chosen_arm") or ""
    ) or None
    proposed_candidate_id = str(
        result.get("proposed_candidate_id")
        or result.get("chosen_candidate_id")
        or ""
    ) or None
    target = {
        arm: float(probability)
        for arm, probability in (
            result.get("target_action_probabilities")
            or result.get("action_probabilities")
            or {}
        ).items()
        if arm in eligible and _finite(probability) is not None
    }
    policy_applied = bool(
        result.get("mode") == "canary"
        and actual_arm == proposed_arm
        and actual_candidate_id
        and actual_candidate_id == proposed_candidate_id
    )
    if result.get("mode") == "shadow" and actual_arm in eligible:
        behavior = {arm: 1.0 if arm == actual_arm else 0.0 for arm in eligible}
        actual_probability = 1.0
    elif policy_applied:
        behavior = dict(target)
        actual_probability = target.get(actual_arm)
    else:
        behavior = {}
        actual_probability = None
    result.update(
        {
            "proposed_arm": proposed_arm,
            "proposed_candidate_id": proposed_candidate_id,
            "actual_arm": actual_arm,
            "actual_candidate_id": actual_candidate_id or None,
            "arm_attribution_basis": actual_attribution_basis,
            "policy_applied": policy_applied,
            "target_action_probabilities": target,
            "behavior_action_probabilities": behavior,
            "actual_action_probability": actual_probability,
        }
    )
    return result


def _best_candidate_per_arm(
    decision: Any,
    *,
    score_contract: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    breakdowns = getattr(decision, "score_breakdown", None)
    for candidate in getattr(decision, "ranked_candidates", ()):
        arm = _arm_name(getattr(candidate, "source_type", None))
        actual_score = _finite(getattr(candidate, "score", None))
        if arm not in BANDIT_ARMS or actual_score is None:
            continue
        candidate_id = str(getattr(candidate, "id", ""))
        breakdown = (
            breakdowns.get(candidate_id)
            if isinstance(breakdowns, Mapping)
            and isinstance(breakdowns.get(candidate_id), Mapping)
            else {}
        )
        baseline_score = _finite(breakdown.get("baseline_score"))
        if baseline_score is None:
            baseline_score = actual_score
        personalized_score = _finite(breakdown.get("personalized_score"))
        if personalized_score is None:
            personalized_score = baseline_score
        policy_score = (
            personalized_score
            if score_contract == BANDIT_PERSONALIZED_SCORE_CONTRACT
            else baseline_score
        )
        if (
            arm not in result
            or policy_score > float(result[arm]["policy_score"])
        ):
            result[arm] = {
                "candidate_id": candidate_id,
                "baseline_score": baseline_score,
                "policy_score": policy_score,
            }
    return result


def _arm_posteriors(
    arms: list[str],
    state: Mapping[str, Any] | None,
    *,
    bucket_key: str,
) -> dict[str, dict[str, float]]:
    sources = state.get(bucket_key) if isinstance(state, Mapping) else None
    result: dict[str, dict[str, float]] = {}
    for arm in arms:
        bucket = sources.get(arm) if isinstance(sources, Mapping) else None
        if not isinstance(bucket, Mapping):
            bucket = {}
        result[arm] = {
            "alpha": round(_number(bucket.get("posterior_alpha"), 2.0), 6),
            "beta": round(_number(bucket.get("posterior_beta"), 2.0), 6),
            "mean": round(_number(bucket.get("posterior_mean"), 0.5), 6),
            "evidence": round(_number(bucket.get("effective_evidence"), 0.0), 6),
        }
    return result


def _empty_decision(
    mode: str,
    *,
    score_contract: str,
    fallback_reason: str,
) -> dict[str, Any]:
    return {
        "policy_id": BANDIT_POLICY_ID,
        "mode": mode,
        "score_contract": score_contract,
        "eligible_arms": [],
        "near_tie_arms": [],
        "proposed_arm": None,
        "proposed_candidate_id": None,
        "actual_arm": None,
        "actual_candidate_id": None,
        "policy_applied": False,
        "chosen_arm": None,
        "chosen_candidate_id": None,
        "exploit_arm": None,
        "target_action_probabilities": {},
        "behavior_action_probabilities": {},
        "actual_action_probability": None,
        "action_probabilities": {},
        "chosen_action_probability": None,
        "exploration_eligible": False,
        "explored": False,
        "context_version": BANDIT_CONTEXT_VERSION,
        "arm_baseline_scores": {},
        "arm_policy_scores": {},
        "arm_scores": {},
        "arm_posteriors": {},
        "arm_bandit_posteriors": {},
        "arm_preference_posteriors": {},
        "fallback_reason": fallback_reason,
    }


def _arm_name(value: Any) -> str:
    source = str(value or "").strip().lower()
    return "news" if source in {"web", "home"} else source


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: Any, default: float) -> float:
    number = _finite(value)
    return default if number is None else number


def _unit(value: Any) -> float:
    return min(max(_number(value, 0.0), 0.0), math.nextafter(1.0, 0.0))


def _score_contract(value: Any) -> str:
    return (
        BANDIT_PERSONALIZED_SCORE_CONTRACT
        if str(value or "").strip().lower() == BANDIT_PERSONALIZED_SCORE_CONTRACT
        else BANDIT_BASELINE_SCORE_CONTRACT
    )


__all__ = [
    "BANDIT_ARMS",
    "BANDIT_BASELINE_SCORE_CONTRACT",
    "BANDIT_CONTEXT_VERSION",
    "BANDIT_EPSILON",
    "BANDIT_NEAR_TIE_GAP",
    "BANDIT_PERSONALIZED_SCORE_CONTRACT",
    "BANDIT_POLICY_ID",
    "VALID_BANDIT_MODES",
    "bandit_preferred_candidate",
    "build_source_bandit_decision",
    "finalize_source_bandit_decision",
]
