"""Build privacy-safe recommendation observations and review context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from typing import Any

from config.application import APP_VERSION
from main_logic.proactive_recommendation.contracts import (
    ProactiveActiveBias,
    ProactiveCandidate,
    ProactiveRecommendationDecision,
)
from main_logic.proactive_recommendation.policy.bandit import (
    finalize_source_bandit_decision,
)

PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION = (
    f"{APP_VERSION}:proactive-recommendation-observation-v5"
)
PROACTIVE_RECOMMENDATION_GIT_REVISION = str(
    os.getenv("NEKO_GIT_REVISION") or os.getenv("GIT_REVISION") or ""
).strip()
_REVIEW_CONTEXT_MODES = {"shadow_review", "testbench"}
_REVIEW_CONTEXT_MAX_CANDIDATES = 3
_REVIEW_SENSITIVE_SOURCE_TYPES = {"personal", "topic_hook", "vision", "window"}


def build_recommendation_observation(
    decision: ProactiveRecommendationDecision,
    *,
    recommendation_mode: Any = None,
    active_bias: ProactiveActiveBias | Mapping[str, Any] | None = None,
    action: Any = None,
    reason_code: Any = None,
    stage: Any = None,
    source_mode: Any = None,
    channel: Any = None,
    source_tag: Any = None,
    active_channels: Any = None,
    source_links: Any = None,
    ts: Any = None,
    lanlan_name: Any = None,
    turn_id: Any = None,
    activity_state: Any = None,
    activity_propensity: Any = None,
    algorithm_version: Any = None,
    git_revision: Any = None,
    review_context: Mapping[str, Any] | None = None,
    decision_context: Mapping[str, Any] | None = None,
    policy_decision: Mapping[str, Any] | None = None,
    top_n: int = 3,
) -> dict[str, Any]:
    shadow_source = decision.shadow_selected_source_type
    actual_primary_channel = _text(source_mode) or _text(channel)
    normalized_reason = _text(reason_code)
    active_bias_info = _active_bias_info(active_bias)
    active_preferred_tag = _text(active_bias_info.get("preferred_source_tag")).upper()
    actual_source_tag = _text(source_tag).upper()
    delivered = _text(action) == "chat" and normalized_reason == "CHAT_DELIVERED"
    active = _clean_string_list(active_channels)
    actual_rank = None
    actual_candidate = None
    actual_candidate_score = None
    actual_candidate_id = None
    if delivered:
        rank, actual_candidate = _find_actual_candidate_match(
            decision.ranked_candidates,
            source_links=source_links,
            source_mode=actual_primary_channel,
            source_tag=source_tag,
            active_channels=active,
        )
        if rank is not None and actual_candidate is not None:
            actual_rank = rank
            actual_candidate_score = round(actual_candidate.score, 3)
            actual_candidate_id = actual_candidate.id
    policy_actual_candidate = actual_candidate
    arm_attribution_basis = (
        "confirmed_material" if actual_candidate is not None else None
    )
    if (
        delivered
        and policy_actual_candidate is None
        and active_bias_info.get("applied") is True
    ):
        preferred_candidate_id = _text(active_bias_info.get("preferred_candidate_id"))
        policy_actual_candidate = next(
            (
                candidate
                for candidate in decision.ranked_candidates
                if candidate.id == preferred_candidate_id
            ),
            None,
        )
        if policy_actual_candidate is not None:
            policy_mode = (
                _text(policy_decision.get("mode"))
                if isinstance(policy_decision, Mapping)
                else ""
            )
            arm_attribution_basis = (
                "applied_canary_policy"
                if policy_mode == "canary"
                else "applied_active_bias"
            )
    finalized_policy_decision = finalize_source_bandit_decision(
        policy_decision,
        actual_candidate=policy_actual_candidate,
        attribution_basis=arm_attribution_basis,
        delivered=delivered,
    )
    actual_aliases = _actual_source_aliases(actual_primary_channel, source_tag, active)
    policy_mode = (
        _text(finalized_policy_decision.get("mode"))
        if isinstance(finalized_policy_decision, Mapping)
        else ""
    )
    expected_source = (
        _text(active_bias_info.get("preferred_source_type"))
        if policy_mode == "canary" and active_bias_info.get("applied") is True
        else shadow_source
    )
    matched_actual_source = bool(
        delivered
        and expected_source
        and _source_type_matches(expected_source, actual_aliases)
    )
    shadow_candidate_id = (
        decision.selected_candidate.id if decision.selected_candidate else None
    )
    expected_candidate_id = (
        _text(active_bias_info.get("preferred_candidate_id"))
        if policy_mode == "canary" and active_bias_info.get("applied") is True
        else shadow_candidate_id
    )
    matched_actual_material = bool(
        delivered
        and expected_candidate_id
        and actual_candidate_id
        and expected_candidate_id == actual_candidate_id
    )
    active_bias_applied = active_bias_info.get("applied") is True
    active_model_followed_preference = bool(
        active_bias_applied
        and delivered
        and active_preferred_tag
        and actual_source_tag == active_preferred_tag
    )
    observation = {
        "ts": _number(ts, 0.0) if ts is not None else None,
        "lanlan_name": _text(lanlan_name) or None,
        "turn_id": _text(turn_id) or None,
        "activity_state": _text(activity_state) or "unknown",
        "activity_propensity": _text(activity_propensity) or "unknown",
        "algorithm_version": (
            _text(algorithm_version) or PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION
        ),
        "git_revision": (
            _text(git_revision) or PROACTIVE_RECOMMENDATION_GIT_REVISION or None
        ),
        "review_context": dict(review_context)
        if isinstance(review_context, Mapping)
        else None,
        "decision_context": (
            dict(decision_context) if isinstance(decision_context, Mapping) else None
        ),
        "policy_decision": (
            finalized_policy_decision
            if isinstance(finalized_policy_decision, Mapping)
            else None
        ),
        "recommendation_mode": _text(recommendation_mode) or None,
        "decision_stage": decision.decision_stage,
        "candidate_count": decision.candidate_count,
        "shadow_selected_source_type": shadow_source,
        "shadow_selected_candidate_id": shadow_candidate_id,
        "shadow_selected_score": (
            round(decision.selected_candidate.score, 3)
            if decision.selected_candidate
            else None
        ),
        "top_candidates": _top_candidate_logs(decision.ranked_candidates, limit=top_n),
        "actual_primary_channel": actual_primary_channel or None,
        "actual_source_tag": _text(source_tag) or None,
        "actual_reason_code": normalized_reason or None,
        "actual_stage": _text(stage) or None,
        "active_channels": active,
        "delivered": delivered,
        "actual_rank": actual_rank,
        "actual_candidate_id": actual_candidate_id,
        "actual_candidate_score": actual_candidate_score,
        "matched_actual_material": matched_actual_material,
        "matched_actual_source": matched_actual_source,
        "active_bias_applied": active_bias_applied,
        "active_preferred_source_type": active_bias_info.get("preferred_source_type"),
        "active_preferred_source_tag": active_bias_info.get("preferred_source_tag"),
        "active_preferred_candidate_id": active_bias_info.get("preferred_candidate_id"),
        "active_bias_fallback_reason": active_bias_info.get("fallback_reason"),
        "active_model_followed_preference": active_model_followed_preference,
    }
    if decision.personalization:
        observation["personalization"] = dict(decision.personalization)
    return observation


def build_recommendation_review_context(
    decision: ProactiveRecommendationDecision,
    *,
    mode: Any = "off",
    activity_state: Any = None,
    delivered_text: Any = None,
) -> dict[str, Any] | None:
    """Build transient review input; the observer sanitizer is authoritative.

    Sensitive screen/personal candidates never expose their raw topic or
    summary. Other candidate text is still treated as untrusted and is passed
    through the review-context sanitizer before persistence.
    """
    normalized_mode = _text(mode)
    if normalized_mode not in _REVIEW_CONTEXT_MODES:
        return None

    labels: list[dict[str, Any]] = []
    redaction_notes: list[str] = []
    for candidate in decision.ranked_candidates[:_REVIEW_CONTEXT_MAX_CANDIDATES]:
        sensitive = candidate.source_type in _REVIEW_SENSITIVE_SOURCE_TYPES
        if sensitive:
            safe_title = candidate.family or candidate.source_type
            safe_summary = ""
            note = (
                "vision_text_omitted"
                if candidate.source_type in {"vision", "window"}
                else "personal_text_omitted"
            )
            if note not in redaction_notes:
                redaction_notes.append(note)
        else:
            safe_title = candidate.topic
            safe_summary = candidate.summary
        labels.append(
            {
                "id": candidate.id,
                "source_type": candidate.source_type,
                "safe_title": safe_title,
                "safe_summary": safe_summary,
                "score": round(float(candidate.score), 3),
            }
        )

    return {
        "schema_version": 1,
        "candidate_labels": labels,
        "activity_state": _text(activity_state) or "unknown",
        "delivered_excerpt": _text(delivered_text),
        "redaction_notes": redaction_notes,
    }


def _top_candidate_logs(
    candidates: Sequence[ProactiveCandidate],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    out = []
    for rank, candidate in enumerate(candidates[: max(0, limit)], start=1):
        out.append(
            {
                "rank": rank,
                "id": candidate.id,
                "source_type": candidate.source_type,
                "family": candidate.family,
                "topic": candidate.topic,
                "score": round(float(candidate.score), 3),
            }
        )
    return out


def _find_actual_candidate_match(
    candidates: Sequence[ProactiveCandidate],
    *,
    source_links: Any,
    source_mode: Any,
    source_tag: Any,
    active_channels: Sequence[str],
) -> tuple[int | None, ProactiveCandidate | None]:
    actual_urls = _source_link_urls(source_links)
    if actual_urls:
        for rank, candidate in enumerate(candidates, start=1):
            if _candidate_url(candidate) in actual_urls:
                return rank, candidate

    aliases = _actual_source_aliases(source_mode, source_tag, active_channels)
    if aliases:
        for rank, candidate in enumerate(candidates, start=1):
            if _source_type_matches(candidate.source_type, aliases):
                return rank, candidate

    return None, None


def _source_link_urls(source_links: Any) -> set[str]:
    if isinstance(source_links, Mapping):
        source_links = [source_links]
    if not isinstance(source_links, Sequence) or isinstance(source_links, (str, bytes)):
        return set()
    urls = set()
    for link in source_links:
        if isinstance(link, Mapping):
            url = _text(link.get("url"))
            if url:
                urls.add(url)
    return urls


def _candidate_url(candidate: ProactiveCandidate) -> str:
    link = candidate.payload.get("link")
    if isinstance(link, Mapping):
        return _text(link.get("url"))
    return ""


def _active_bias_info(
    active_bias: ProactiveActiveBias | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(active_bias, ProactiveActiveBias):
        return active_bias.to_log_dict()
    if isinstance(active_bias, Mapping):
        return {
            "applied": active_bias.get("applied") is True,
            "preferred_source_type": _text(active_bias.get("preferred_source_type"))
            or None,
            "preferred_source_tag": _text(
                active_bias.get("preferred_source_tag")
            ).upper()
            or None,
            "preferred_candidate_id": _text(active_bias.get("preferred_candidate_id"))
            or None,
            "score_gap": (
                _number(active_bias.get("score_gap"), 0.0)
                if active_bias.get("score_gap") is not None
                else None
            ),
            "fallback_reason": _text(active_bias.get("fallback_reason")) or None,
        }
    return {
        "applied": False,
        "preferred_source_type": None,
        "preferred_source_tag": None,
        "preferred_candidate_id": None,
        "score_gap": None,
        "fallback_reason": None,
    }


def _actual_source_aliases(
    source_mode: Any, source_tag: Any, active_channels: Sequence[str]
) -> set[str]:
    aliases = set(_clean_string_list([source_mode]))
    tag = _text(source_tag).upper()
    if tag == "WEB":
        aliases.add("web")
    elif tag == "MUSIC":
        aliases.add("music")
    elif tag == "MEME":
        aliases.add("meme")
    elif tag == "CHAT":
        aliases.add("chat")
    return {alias for alias in aliases if alias}


def _source_type_matches(source_type: Any, aliases: set[str]) -> bool:
    source = _text(source_type)
    if not source or not aliases:
        return False
    if source in aliases:
        return True
    web_sources = {"web", "news", "video", "home", "personal"}
    return source in web_sources and bool(aliases & web_sources)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence):
        return []
    return [_text(item) for item in value if _text(item)]
