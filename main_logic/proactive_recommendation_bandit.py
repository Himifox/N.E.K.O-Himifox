"""Constrained source-level bandit for safe proactive material candidates."""
from __future__ import annotations

from collections.abc import Mapping
import math
import random
from typing import Any


BANDIT_POLICY_ID = "source_epsilon_greedy_v1"
BANDIT_CONTEXT_VERSION = "source-context-v1"
BANDIT_ARMS = ("news", "music", "meme")
BANDIT_EPSILON = 0.05
BANDIT_NEAR_TIE_GAP = 0.03
VALID_BANDIT_MODES = frozenset({"off", "shadow", "canary"})


def build_source_bandit_decision(
    recommendation_decision: Any,
    *,
    mode: Any,
    preference_state: Mapping[str, Any] | None = None,
    random_value: float | None = None,
    random_arm_value: float | None = None,
) -> dict[str, Any]:
    """Choose one safe source arm without adding or unfiltering candidates."""
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in VALID_BANDIT_MODES:
        normalized_mode = "off"
    arm_candidates = _best_candidate_per_arm(recommendation_decision)
    eligible = sorted(
        arm_candidates,
        key=lambda arm: (-float(arm_candidates[arm]["score"]), arm),
    )
    if not eligible:
        return _empty_decision(normalized_mode)

    exploit_arm = eligible[0]
    top_score = float(arm_candidates[exploit_arm]["score"])
    near_tie = [
        arm
        for arm in eligible
        if top_score - float(arm_candidates[arm]["score"]) <= BANDIT_NEAR_TIE_GAP + 1e-12
    ]
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
        "eligible_arms": eligible,
        "near_tie_arms": near_tie,
        "chosen_arm": chosen_arm,
        "chosen_candidate_id": arm_candidates[chosen_arm]["candidate_id"],
        "exploit_arm": exploit_arm,
        "action_probabilities": {
            arm: round(probabilities[arm], 12) for arm in eligible
        },
        "chosen_action_probability": round(probabilities[chosen_arm], 12),
        "exploration_eligible": exploration_eligible,
        "explored": explored,
        "context_version": BANDIT_CONTEXT_VERSION,
        "arm_scores": {
            arm: round(float(arm_candidates[arm]["score"]), 6) for arm in eligible
        },
        "arm_posteriors": _arm_posteriors(eligible, preference_state),
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


def _best_candidate_per_arm(decision: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for candidate in getattr(decision, "ranked_candidates", ()):
        arm = _arm_name(getattr(candidate, "source_type", None))
        score = _finite(getattr(candidate, "score", None))
        if arm not in BANDIT_ARMS or score is None:
            continue
        if arm not in result or score > float(result[arm]["score"]):
            result[arm] = {
                "candidate_id": str(getattr(candidate, "id", "")),
                "score": score,
            }
    return result


def _arm_posteriors(
    arms: list[str], state: Mapping[str, Any] | None
) -> dict[str, dict[str, float]]:
    sources = state.get("sources") if isinstance(state, Mapping) else None
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


def _empty_decision(mode: str) -> dict[str, Any]:
    return {
        "policy_id": BANDIT_POLICY_ID,
        "mode": mode,
        "eligible_arms": [],
        "near_tie_arms": [],
        "chosen_arm": None,
        "chosen_candidate_id": None,
        "exploit_arm": None,
        "action_probabilities": {},
        "chosen_action_probability": None,
        "exploration_eligible": False,
        "explored": False,
        "context_version": BANDIT_CONTEXT_VERSION,
        "arm_scores": {},
        "arm_posteriors": {},
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


__all__ = [
    "BANDIT_ARMS",
    "BANDIT_CONTEXT_VERSION",
    "BANDIT_EPSILON",
    "BANDIT_NEAR_TIE_GAP",
    "BANDIT_POLICY_ID",
    "VALID_BANDIT_MODES",
    "bandit_preferred_candidate",
    "build_source_bandit_decision",
]
