"""Lightweight candidate ranking for proactive chat source selection.

The module is intentionally pure: it does not fetch sources, call LLMs, deliver
messages, or write history. The proactive endpoint can run it in shadow mode to
observe which source a rule-based recommender would prefer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import os
from typing import Any

from main_logic.proactive_recommendation_personalization import (
    PERSONALIZATION_MAX_ABS_DELTA,
)

from config.application import APP_VERSION


_PRIVACY_CLOSED_STATES = {"closed", "private", "privacy", "privacy_closed"}
_BUSY_STATES = {"busy", "voice_busy", "delivery_busy", "active", "away"}
_RESTRICTED_STATES = {"restricted_screen_only", "focused_work", "gaming"}
_PRIVACY_SENSITIVE_SOURCES = {"vision", "window", "personal"}
_CONTEXT_MATCH_WEIGHT = 0.25
_DIVERSITY_RECENT_SOURCE_LIMIT = 8
_DIVERSITY_SOURCE_REPEAT_STEP = 0.04
_DIVERSITY_SOURCE_REPEAT_MAX = 0.16
_DIVERSITY_SOURCE_STREAK_STEP = 0.06
_DIVERSITY_SOURCE_STREAK_MAX = 0.12
_DIVERSITY_CANDIDATE_REPEAT_PENALTY = 0.12
_DIVERSITY_PENALTY_MAX = 0.30
_ACTIVE_DIVERSITY_OVERUSE_THRESHOLD = 0.12
_SOURCE_TYPE_SCORE_ADJUSTMENTS = {
    # Calibrated from a 50-observation shadow run: news was top1 in 64% of
    # samples while actual delivery often chose chat/music/meme instead.
    "news": -0.05,
}

PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION = (
    f"{APP_VERSION}:proactive-recommendation-observation-v5"
)
# Build environments may inject a revision once at process start. Never shell
# out per observation: logging must remain cheap and work in packaged builds.
PROACTIVE_RECOMMENDATION_GIT_REVISION = str(
    os.getenv("NEKO_GIT_REVISION") or os.getenv("GIT_REVISION") or ""
).strip()
_REVIEW_CONTEXT_MODES = {"shadow_review", "testbench"}
_REVIEW_CONTEXT_MAX_CANDIDATES = 3
_REVIEW_SENSITIVE_SOURCE_TYPES = {"personal", "topic_hook", "vision", "window"}


@dataclass(slots=True)
class ProactiveCandidate:
    id: str
    source_type: str
    family: str
    topic: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    freshness: float = 0.5
    risk_flags: tuple[str, ...] = ()
    quality: float = 0.5
    score: float = 0.0

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "family": self.family,
            "topic": self.topic,
            "summary": self.summary,
            "freshness": round(_clamp01(self.freshness), 3),
            "risk_flags": list(self.risk_flags),
            "quality": round(_clamp01(self.quality), 3),
            "score": round(float(self.score), 3),
        }


@dataclass(slots=True)
class ProactiveRecommendationContext:
    lanlan_name: str
    enabled_modes: Sequence[str] = ()
    source_weights: Mapping[str, float] = field(default_factory=dict)
    source_type_adjustments: Mapping[str, float] = field(default_factory=dict)
    recent_sources: Sequence[str] = ()
    recent_shadow_sources: Sequence[str] = ()
    recent_candidate_ids: Sequence[str] = ()
    privacy_state: str = "open"
    activity_state: str = "unknown"
    topic_materials: Sequence[Mapping[str, Any]] = ()
    mini_game_available: bool = False
    personalization_mode: str = "off"
    personalization_adjustments: Mapping[str, float] = field(default_factory=dict)


def resolve_recommendation_activity_state(activity_snapshot: Any) -> str:
    """Return the inferred activity state, never its collapsed propensity.

    Ranking owns state-sensitive costs (for example ``away`` and
    ``focused_work``).  ``propensity`` belongs to the upstream router and
    deliberately collapses several states, so substituting it here silently
    disables those ranking branches.
    """
    if activity_snapshot is None:
        return "unknown"
    return _text(getattr(activity_snapshot, "state", None)) or "unknown"


@dataclass(slots=True)
class ProactiveRecommendationDecision:
    candidate_count: int
    selected_candidate: ProactiveCandidate | None
    decision_stage: str = "pre_phase1_source"
    ranked_candidates: tuple[ProactiveCandidate, ...] = ()
    filtered_reasons: dict[str, str] = field(default_factory=dict)
    score_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    shadow_selected_source_type: str | None = None
    personalization: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        selected = self.selected_candidate.to_log_dict() if self.selected_candidate else None
        result = {
            "decision_stage": self.decision_stage,
            "candidate_count": self.candidate_count,
            "filtered_reasons": dict(self.filtered_reasons),
            "shadow_selected_source_type": self.shadow_selected_source_type,
            "shadow_selected_candidate_id": self.selected_candidate.id if self.selected_candidate else None,
            "shadow_selected_score": round(self.selected_candidate.score, 3) if self.selected_candidate else None,
            "selected_candidate": selected,
            "top_candidates": _top_candidate_logs(self.ranked_candidates),
            "score_breakdown": {
                key: {name: round(value, 3) for name, value in values.items()}
                for key, values in self.score_breakdown.items()
            },
        }
        if self.personalization:
            result["personalization"] = dict(self.personalization)
        return result


@dataclass(slots=True)
class ProactiveActiveBias:
    applied: bool
    preferred_source_type: str | None = None
    preferred_source_tag: str | None = None
    preferred_candidate_id: str | None = None
    score_gap: float | None = None
    fallback_reason: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "preferred_source_type": self.preferred_source_type,
            "preferred_source_tag": self.preferred_source_tag,
            "preferred_candidate_id": self.preferred_candidate_id,
            "score_gap": round(float(self.score_gap), 3) if self.score_gap is not None else None,
            "fallback_reason": self.fallback_reason,
        }


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
    actual_candidate_score = None
    actual_candidate_id = None
    if delivered:
        rank, candidate = _find_actual_candidate_match(
            decision.ranked_candidates,
            source_links=source_links,
            source_mode=actual_primary_channel,
            source_tag=source_tag,
            active_channels=active,
        )
        if rank is not None and candidate is not None:
            actual_rank = rank
            actual_candidate_score = round(candidate.score, 3)
            actual_candidate_id = candidate.id
    actual_aliases = _actual_source_aliases(actual_primary_channel, source_tag, active)
    policy_mode = (
        _text(policy_decision.get("mode"))
        if isinstance(policy_decision, Mapping)
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
    shadow_candidate_id = decision.selected_candidate.id if decision.selected_candidate else None
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
        "review_context": dict(review_context) if isinstance(review_context, Mapping) else None,
        "decision_context": (
            dict(decision_context) if isinstance(decision_context, Mapping) else None
        ),
        "policy_decision": (
            dict(policy_decision) if isinstance(policy_decision, Mapping) else None
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
            note = "vision_text_omitted" if candidate.source_type in {"vision", "window"} else "personal_text_omitted"
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


def build_shadow_recommendation_decision(
    ctx: ProactiveRecommendationContext,
    sources: Mapping[str, Mapping[str, Any]] | None,
) -> ProactiveRecommendationDecision:
    candidates = build_candidates(ctx, sources or {})
    return _rank_candidates(ctx, candidates, decision_stage="pre_phase1_source")


def build_phase1_material_shadow_decision(
    ctx: ProactiveRecommendationContext,
    *,
    phase1_topics: Sequence[Any] = (),
    selected_web_link: Mapping[str, Any] | None = None,
    selected_music_link: Mapping[str, Any] | None = None,
    selected_meme_link: Mapping[str, Any] | None = None,
    vision_content: Mapping[str, Any] | None = None,
    active_channels: Sequence[Any] = (),
) -> ProactiveRecommendationDecision:
    """Rank concrete Phase-1 materials without changing the live pipeline."""
    candidates = _phase1_material_candidates(
        ctx,
        phase1_topics=phase1_topics,
        selected_web_link=selected_web_link,
        selected_music_link=selected_music_link,
        selected_meme_link=selected_meme_link,
        vision_content=vision_content,
        active_channels=active_channels,
    )
    return _rank_candidates(ctx, candidates, decision_stage="phase1_material")


def source_type_to_phase2_tag(source_type: Any) -> str | None:
    """Map active-safe material source types to existing Phase-2 tags."""
    normalized = _text(source_type)
    if normalized in {"web", "news", "video", "home"}:
        return "WEB"
    if normalized == "music":
        return "MUSIC"
    if normalized == "meme":
        return "MEME"
    return None


def build_active_source_bias(
    decision: ProactiveRecommendationDecision | None,
    *,
    min_score_gap: float = 0.05,
) -> ProactiveActiveBias:
    """Return the minimal active-source pilot bias for a material decision."""
    if decision is None:
        return _active_bias_fallback("no_decision")
    if decision.decision_stage != "phase1_material":
        return _active_bias_fallback("not_phase1_material")
    ranked = list(decision.ranked_candidates or ())
    if not ranked:
        return _active_bias_fallback("no_candidate")

    top = ranked[0]
    preferred_tag = source_type_to_phase2_tag(top.source_type)
    if preferred_tag is None:
        return _active_bias_fallback(
            "unsupported_source",
            candidate=top,
            score_gap=_score_gap(ranked),
        )
    if not _candidate_has_material_link(top):
        return _active_bias_fallback(
            "missing_material_link",
            candidate=top,
            preferred_tag=preferred_tag,
            score_gap=_score_gap(ranked),
        )
    top_breakdown = decision.score_breakdown.get(top.id, {})
    diversity_penalty = _number(top_breakdown.get("diversity_penalty"), 0.0)
    if diversity_penalty >= _ACTIVE_DIVERSITY_OVERUSE_THRESHOLD:
        return _active_bias_fallback(
            "diversity_overuse",
            candidate=top,
            preferred_tag=preferred_tag,
            score_gap=_score_gap(ranked),
        )

    effective_min_score_gap = max(0.0, float(min_score_gap))
    if (
        decision.personalization.get("ranking_consumed") is True
        and decision.personalization.get("top1_changed") is True
    ):
        effective_min_score_gap = 0.0
    gap = _score_gap(ranked)
    if gap is not None and gap < effective_min_score_gap:
        return _active_bias_fallback(
            "score_gap_too_small",
            candidate=top,
            preferred_tag=preferred_tag,
            score_gap=gap,
        )

    return ProactiveActiveBias(
        applied=True,
        preferred_source_type=top.source_type,
        preferred_source_tag=preferred_tag,
        preferred_candidate_id=top.id,
        score_gap=gap,
    )


def reorder_phase1_topics_for_bias(
    phase1_topics: Sequence[Any],
    bias: ProactiveActiveBias | Mapping[str, Any] | None,
) -> list[Any]:
    """Move the preferred material channel first without dropping candidates."""
    info = _active_bias_info(bias)
    if info.get("applied") is not True:
        return list(phase1_topics or ())
    target = {
        "WEB": "web",
        "MUSIC": "music",
        "MEME": "meme",
    }.get(_text(info.get("preferred_source_tag")).upper())
    if not target:
        return list(phase1_topics or ())

    preferred: list[Any] = []
    rest: list[Any] = []
    for item in phase1_topics or ():
        channel = ""
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 1:
            channel = _text(item[0])
        if channel == target:
            preferred.append(item)
        else:
            rest.append(item)
    return preferred + rest


def _rank_candidates(
    ctx: ProactiveRecommendationContext,
    candidates: Sequence[ProactiveCandidate],
    *,
    decision_stage: str,
) -> ProactiveRecommendationDecision:
    filtered: dict[str, str] = {}
    score_breakdown: dict[str, dict[str, float]] = {}
    ranked: list[ProactiveCandidate] = []

    for candidate in candidates:
        filter_reason = _filter_reason(ctx, candidate)
        if filter_reason:
            filtered[candidate.id] = filter_reason
            continue
        score, breakdown = _score_candidate(ctx, candidate)
        candidate.score = score
        score_breakdown[candidate.id] = breakdown
        ranked.append(candidate)

    ranked.sort(key=lambda item: item.score, reverse=True)
    selected = ranked[0] if ranked else None
    personalization = _personalization_diagnostics(
        ctx,
        ranked,
        score_breakdown,
    )
    return ProactiveRecommendationDecision(
        candidate_count=len(candidates),
        selected_candidate=selected,
        decision_stage=decision_stage,
        ranked_candidates=tuple(ranked),
        filtered_reasons=filtered,
        score_breakdown=score_breakdown,
        shadow_selected_source_type=selected.source_type if selected else None,
        personalization=personalization,
    )


def build_candidates(
    ctx: ProactiveRecommendationContext,
    sources: Mapping[str, Mapping[str, Any]],
) -> list[ProactiveCandidate]:
    candidates: list[ProactiveCandidate] = []
    enabled = set(ctx.enabled_modes or ())

    for source_type, content in sources.items():
        if not isinstance(content, Mapping):
            continue
        if enabled and source_type not in enabled:
            continue
        candidates.extend(_source_candidates(source_type, content))

    for material in ctx.topic_materials or ():
        if isinstance(material, Mapping):
            candidate = _topic_material_candidate(material)
            if candidate is not None:
                candidates.append(candidate)

    if ctx.mini_game_available:
        candidates.append(
            _candidate(
                "mini_game",
                "mini_game_invite",
                "mini-game invite",
                "mini-game invite",
                payload={"available": True},
                freshness=0.6,
                quality=0.45,
            )
        )

    return candidates


def _phase1_material_candidates(
    ctx: ProactiveRecommendationContext,
    *,
    phase1_topics: Sequence[Any],
    selected_web_link: Mapping[str, Any] | None,
    selected_music_link: Mapping[str, Any] | None,
    selected_meme_link: Mapping[str, Any] | None,
    vision_content: Mapping[str, Any] | None,
    active_channels: Sequence[Any],
) -> list[ProactiveCandidate]:
    candidates: list[ProactiveCandidate] = []
    topic_by_channel = _phase1_topic_by_channel(phase1_topics)
    active = set(_clean_string_list(active_channels))

    if isinstance(selected_web_link, Mapping):
        source_type = _web_material_source_type(ctx, selected_web_link)
        title = _text(selected_web_link.get("title")) or _text(topic_by_channel.get("web")) or "web material"
        candidates.append(
            _candidate(
                source_type,
                _family_for_source(source_type),
                title,
                _text(topic_by_channel.get("web")) or title,
                payload={
                    "link": _safe_link_payload(selected_web_link),
                    "material_stage": "phase1",
                },
                freshness=0.85,
                quality=_link_quality(selected_web_link),
            )
        )
    elif "web" in active and topic_by_channel.get("web"):
        source_type = _fallback_web_source_type(ctx)
        topic = _text(topic_by_channel.get("web"))
        candidates.append(
            _candidate(
                source_type,
                _family_for_source(source_type),
                topic,
                topic,
                payload={"material_stage": "phase1"},
                freshness=0.65,
                quality=0.45,
            )
        )

    if isinstance(selected_music_link, Mapping):
        title = _text(selected_music_link.get("title")) or "music material"
        artist = _text(selected_music_link.get("artist"))
        topic = f"{title} - {artist}".strip(" -") if artist else title
        candidates.append(
            _candidate(
                "music",
                "music",
                topic,
                _text(topic_by_channel.get("music")) or topic,
                payload={
                    "link": _safe_link_payload(selected_music_link),
                    "material_stage": "phase1",
                },
                freshness=0.8,
                quality=_link_quality(selected_music_link),
            )
        )

    if isinstance(selected_meme_link, Mapping):
        title = _text(selected_meme_link.get("title")) or "meme material"
        candidates.append(
            _candidate(
                "meme",
                "meme",
                title,
                _text(topic_by_channel.get("meme")) or title,
                payload={
                    "link": _safe_link_payload(selected_meme_link),
                    "material_stage": "phase1",
                },
                freshness=0.8,
                quality=_link_quality(selected_meme_link),
            )
        )

    if isinstance(vision_content, Mapping) and ("vision" in active or vision_content):
        title = _text(vision_content.get("window_title")) or "screen context"
        candidates.append(
            _candidate(
                "vision",
                "screen_context",
                title,
                title,
                payload={
                    "window_title": title,
                    "material_stage": "phase1",
                },
                freshness=0.75,
                quality=0.65 if title != "screen context" else 0.45,
                risk_flags=("screen",),
            )
        )

    for material in ctx.topic_materials or ():
        if isinstance(material, Mapping):
            candidate = _topic_material_candidate(material)
            if candidate is not None:
                candidates.append(candidate)

    return candidates


def _source_candidates(source_type: str, content: Mapping[str, Any]) -> list[ProactiveCandidate]:
    if source_type == "vision":
        title = _text(content.get("window_title")) or "screen context"
        quality = 0.75 if _text(content.get("screenshot_b64")) else 0.35
        return [
            _candidate(
                source_type,
                "screen_context",
                title,
                title,
                payload=dict(content),
                freshness=0.8,
                quality=quality,
                risk_flags=("screen",),
            )
        ]

    if content.get("placeholder"):
        note = _text(content.get("note")) or f"{source_type} placeholder"
        return [
            _candidate(
                source_type,
                _family_for_source(source_type),
                source_type,
                note,
                payload=dict(content),
                freshness=0.35,
                quality=0.25,
                risk_flags=("placeholder",),
            )
        ]

    links = content.get("links")
    if isinstance(links, Sequence) and not isinstance(links, (str, bytes)):
        out = []
        for link in links:
            if not isinstance(link, Mapping):
                continue
            title = _text(link.get("title"))
            if not title:
                continue
            out.append(
                _candidate(
                    source_type,
                    _family_for_source(source_type),
                    title,
                    _text(link.get("summary")) or title,
                    payload={"link": dict(link), "raw_source": _raw_source_hint(content)},
                    freshness=0.75,
                    quality=0.75 if _text(link.get("url")) else 0.55,
                )
            )
            if len(out) >= 5:
                break
        if out:
            return out

    formatted = _text(content.get("formatted_content"))
    raw = content.get("raw_data") if isinstance(content.get("raw_data"), Mapping) else {}
    fallback_topic = _first_content_line(formatted) or _text(raw.get("window_title"))
    if not fallback_topic:
        return []
    return [
        _candidate(
            source_type,
            _family_for_source(source_type),
            fallback_topic,
            fallback_topic,
            payload={"raw_source": _raw_source_hint(content)},
            freshness=0.55,
            quality=0.45,
        )
    ]


def _topic_material_candidate(material: Mapping[str, Any]) -> ProactiveCandidate | None:
    topic = _text(material.get("interest"))
    if not topic:
        return None
    relevance = _number(material.get("relevance"), 70.0) / 100.0
    risk = _number(material.get("risk"), 20.0) / 100.0
    risk_flags = ("topic_risk",) if risk >= 0.65 else ()
    hint = material.get("material_hint")
    summary = ""
    if isinstance(hint, Mapping):
        summary = _text(hint.get("summary"))
    return _candidate(
        "topic_hook",
        "topic_hook",
        topic,
        summary or topic,
        payload=dict(material),
        freshness=0.8,
        quality=max(0.45, min(1.0, relevance)),
        risk_flags=risk_flags,
    )


def _filter_reason(ctx: ProactiveRecommendationContext, candidate: ProactiveCandidate) -> str | None:
    if candidate.source_type not in ("topic_hook", "mini_game"):
        enabled = set(ctx.enabled_modes or ())
        if enabled and candidate.source_type not in enabled:
            return "source_disabled"

    if ctx.privacy_state in _PRIVACY_CLOSED_STATES and (
        candidate.source_type in _PRIVACY_SENSITIVE_SOURCES
        or "screen" in candidate.risk_flags
        or "privacy" in candidate.risk_flags
    ):
        return "privacy_sensitive"

    if "duplicate" in candidate.risk_flags:
        return "duplicate"

    if ctx.activity_state in _BUSY_STATES and candidate.source_type not in ("topic_hook", "vision"):
        return "activity_busy"

    return None


def _score_candidate(
    ctx: ProactiveRecommendationContext,
    candidate: ProactiveCandidate,
) -> tuple[float, dict[str, float]]:
    source_weight = _clamp01(float(ctx.source_weights.get(candidate.source_type, 0.5)))
    freshness = _clamp01(candidate.freshness)
    context_match = _context_match(ctx, candidate)
    user_interest_match = _user_interest_match(candidate)
    novelty = _novelty(ctx, candidate)
    source_quality = _clamp01(candidate.quality)
    interaction_value = _interaction_value(candidate)
    interruption_cost = _interruption_cost(ctx, candidate)
    risk_penalty = _risk_penalty(ctx, candidate)
    source_type_adjustment = _source_type_score_adjustment(candidate)
    tuning_adjustment = _tuning_score_adjustment(ctx, candidate)
    diversity_penalty, diversity_stats = _diversity_penalty(ctx, candidate)

    base_score = (
        0.20 * source_weight
        + 0.15 * freshness
        + _CONTEXT_MATCH_WEIGHT * context_match
        + 0.15 * user_interest_match
        + 0.15 * novelty
        + 0.10 * source_quality
        + 0.05 * interaction_value
        - 0.25 * interruption_cost
        - 0.30 * risk_penalty
    )
    baseline_score = _clamp01(
        base_score + source_type_adjustment + tuning_adjustment - diversity_penalty
    )
    personalization_mode = _personalization_mode(ctx.personalization_mode)
    personalization_adjustment = 0.0
    if personalization_mode in {"shadow_compare", "active"}:
        personalization_adjustment = max(
            -PERSONALIZATION_MAX_ABS_DELTA,
            min(
                PERSONALIZATION_MAX_ABS_DELTA,
                _number(
                    ctx.personalization_adjustments.get(candidate.source_type),
                    0.0,
                ),
            ),
        )
    personalized_score = _clamp01(baseline_score + personalization_adjustment)
    score = personalized_score if personalization_mode == "active" else baseline_score
    breakdown = {
        "source_weight": source_weight,
        "freshness": freshness,
        "context_match": context_match,
        "user_interest_match": user_interest_match,
        "novelty": novelty,
        "source_quality": source_quality,
        "interaction_value": interaction_value,
        "interruption_cost": interruption_cost,
        "risk_penalty": risk_penalty,
        "base_score": base_score,
        "source_type_adjustment": source_type_adjustment,
        "tuning_adjustment": tuning_adjustment,
        "diversity_penalty": diversity_penalty,
        "shadow_source_repeat_count": float(diversity_stats["shadow_source_repeat_count"]),
        "shadow_source_streak": float(diversity_stats["shadow_source_streak"]),
        "candidate_repeat_count": float(diversity_stats["candidate_repeat_count"]),
        "score": score,
    }
    if personalization_mode != "off":
        breakdown.update(
            {
                "baseline_score": baseline_score,
                "personalization_adjustment": personalization_adjustment,
                "personalized_score": personalized_score,
            }
        )
    return score, breakdown


def _personalization_diagnostics(
    ctx: ProactiveRecommendationContext,
    ranked: Sequence[ProactiveCandidate],
    score_breakdown: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    mode = _personalization_mode(ctx.personalization_mode)
    if mode == "off" or not ranked:
        return {}

    baseline_ranked = sorted(
        ranked,
        key=lambda candidate: _number(
            score_breakdown.get(candidate.id, {}).get("baseline_score"),
            candidate.score,
        ),
        reverse=True,
    )
    personalized_ranked = sorted(
        ranked,
        key=lambda candidate: _number(
            score_breakdown.get(candidate.id, {}).get("personalized_score"),
            candidate.score,
        ),
        reverse=True,
    )
    baseline_top = baseline_ranked[0]
    personalized_top = personalized_ranked[0]
    baseline_positions = {
        candidate.id: index for index, candidate in enumerate(baseline_ranked, start=1)
    }
    personalized_positions = {
        candidate.id: index
        for index, candidate in enumerate(personalized_ranked, start=1)
    }
    rows = []
    for candidate in baseline_ranked:
        breakdown = score_breakdown.get(candidate.id, {})
        rows.append(
            {
                "id": candidate.id,
                "source_type": candidate.source_type,
                "baseline_rank": baseline_positions[candidate.id],
                "personalized_rank": personalized_positions[candidate.id],
                "baseline_score": round(
                    _number(breakdown.get("baseline_score"), candidate.score), 6
                ),
                "delta": round(
                    _number(breakdown.get("personalization_adjustment"), 0.0), 6
                ),
                "personalized_score": round(
                    _number(breakdown.get("personalized_score"), candidate.score), 6
                ),
            }
        )
    return {
        "mode": mode,
        "ranking_consumed": mode == "active",
        "baseline_selected_candidate_id": baseline_top.id,
        "baseline_selected_source_type": baseline_top.source_type,
        "personalized_selected_candidate_id": personalized_top.id,
        "personalized_selected_source_type": personalized_top.source_type,
        "top1_changed": baseline_top.id != personalized_top.id,
        "candidates": rows,
    }


def _personalization_mode(value: Any) -> str:
    mode = _text(value)
    return mode if mode in {"off", "shadow_compare", "active"} else "off"


def _context_match(ctx: ProactiveRecommendationContext, candidate: ProactiveCandidate) -> float:
    if ctx.activity_state == "restricted_screen_only":
        return 1.0 if candidate.source_type == "vision" else 0.2
    if ctx.activity_state in _RESTRICTED_STATES:
        if candidate.source_type in ("vision", "window", "topic_hook"):
            return 0.8
        return 0.35
    if ctx.activity_state == "stale_returning":
        return 0.9 if candidate.source_type in ("topic_hook", "personal") else 0.65
    return 0.7 if candidate.source_type != "mini_game" else 0.55


def _user_interest_match(candidate: ProactiveCandidate) -> float:
    if candidate.source_type == "topic_hook":
        return _clamp01(_number(candidate.payload.get("relevance"), 70.0) / 100.0)
    if candidate.source_type == "personal":
        return 0.75
    if candidate.source_type in ("music", "meme"):
        return 0.55
    return 0.5


def _novelty(ctx: ProactiveRecommendationContext, candidate: ProactiveCandidate) -> float:
    recent = [str(item or "") for item in (ctx.recent_sources or ())]
    if candidate.source_type not in recent:
        return 1.0
    repeats = sum(1 for item in recent if item == candidate.source_type)
    return max(0.15, 1.0 - 0.35 * repeats)


def _interaction_value(candidate: ProactiveCandidate) -> float:
    if candidate.source_type == "topic_hook":
        return 0.85
    if candidate.source_type == "meme":
        return 0.7
    if candidate.source_type == "mini_game":
        return 0.65
    if candidate.source_type == "music":
        return 0.6
    if candidate.source_type in ("news", "video", "home"):
        return 0.55
    return 0.5


def _interruption_cost(ctx: ProactiveRecommendationContext, candidate: ProactiveCandidate) -> float:
    if ctx.activity_state in _BUSY_STATES:
        return 0.9
    if ctx.activity_state in _RESTRICTED_STATES:
        return 0.65 if candidate.source_type not in ("vision", "topic_hook") else 0.25
    if ctx.activity_state == "stale_returning":
        return 0.15
    return 0.25 if candidate.source_type in ("meme", "mini_game") else 0.2


def _risk_penalty(ctx: ProactiveRecommendationContext, candidate: ProactiveCandidate) -> float:
    penalty = 0.0
    flags = set(candidate.risk_flags)
    if "privacy" in flags:
        penalty += 0.8
    if "duplicate" in flags:
        penalty += 0.8
    if "screen" in flags:
        penalty += 0.35 if ctx.privacy_state in _PRIVACY_CLOSED_STATES else 0.1
    if "placeholder" in flags:
        penalty += 0.15
    if "topic_risk" in flags:
        penalty += 0.3
    return _clamp01(penalty)


def _source_type_score_adjustment(candidate: ProactiveCandidate) -> float:
    return float(_SOURCE_TYPE_SCORE_ADJUSTMENTS.get(candidate.source_type, 0.0))


def _tuning_score_adjustment(
    ctx: ProactiveRecommendationContext,
    candidate: ProactiveCandidate,
) -> float:
    try:
        value = float(ctx.source_type_adjustments.get(candidate.source_type, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(-0.15, min(0.15, value))


def _diversity_penalty(
    ctx: ProactiveRecommendationContext,
    candidate: ProactiveCandidate,
) -> tuple[float, dict[str, int]]:
    recent_sources = _clean_recent_items(ctx.recent_shadow_sources)[-_DIVERSITY_RECENT_SOURCE_LIMIT:]
    recent_candidate_ids = _clean_recent_items(ctx.recent_candidate_ids)
    source_repeat_count = sum(1 for source in recent_sources if source == candidate.source_type)
    source_streak = 0
    for source in reversed(recent_sources):
        if source != candidate.source_type:
            break
        source_streak += 1
    candidate_repeat_count = sum(1 for item in recent_candidate_ids if item == candidate.id)
    source_repeat_penalty = min(
        _DIVERSITY_SOURCE_REPEAT_MAX,
        _DIVERSITY_SOURCE_REPEAT_STEP * source_repeat_count,
    )
    source_streak_penalty = min(
        _DIVERSITY_SOURCE_STREAK_MAX,
        _DIVERSITY_SOURCE_STREAK_STEP * source_streak,
    )
    candidate_repeat_penalty = (
        _DIVERSITY_CANDIDATE_REPEAT_PENALTY if candidate_repeat_count > 0 else 0.0
    )
    penalty = min(
        _DIVERSITY_PENALTY_MAX,
        source_repeat_penalty + source_streak_penalty + candidate_repeat_penalty,
    )
    return penalty, {
        "shadow_source_repeat_count": source_repeat_count,
        "shadow_source_streak": source_streak,
        "candidate_repeat_count": candidate_repeat_count,
    }


def _clean_recent_items(values: Sequence[Any]) -> list[str]:
    return [_text(item) for item in values or () if _text(item)]


def _candidate(
    source_type: str,
    family: str,
    topic: str,
    summary: str,
    *,
    payload: dict[str, Any],
    freshness: float,
    quality: float,
    risk_flags: tuple[str, ...] = (),
) -> ProactiveCandidate:
    return ProactiveCandidate(
        id=_candidate_id(source_type, topic, payload),
        source_type=source_type,
        family=family,
        topic=topic,
        summary=summary,
        payload=payload,
        freshness=_clamp01(freshness),
        risk_flags=risk_flags,
        quality=_clamp01(quality),
    )


def _candidate_id(source_type: str, topic: str, payload: Mapping[str, Any]) -> str:
    link = payload.get("link")
    url = ""
    if isinstance(link, Mapping):
        url = _text(link.get("url"))
    raw = f"{source_type}|{topic}|{url}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{source_type}:{digest}"


def _family_for_source(source_type: str) -> str:
    return {
        "news": "news",
        "video": "video",
        "home": "trending",
        "personal": "personal_dynamic",
        "window": "window_context",
        "music": "music",
        "meme": "meme",
        "vision": "screen_context",
    }.get(source_type, source_type)


def _phase1_topic_by_channel(phase1_topics: Sequence[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in phase1_topics or ():
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 2:
            continue
        channel = _text(item[0])
        topic = _text(item[1])
        if channel and topic and channel not in out:
            out[channel] = topic
    return out


def _web_material_source_type(
    ctx: ProactiveRecommendationContext,
    selected_web_link: Mapping[str, Any],
) -> str:
    mode = _text(selected_web_link.get("mode"))
    if mode:
        return mode
    return _fallback_web_source_type(ctx)


def _fallback_web_source_type(ctx: ProactiveRecommendationContext) -> str:
    for mode in ctx.enabled_modes or ():
        normalized = _text(mode)
        if normalized in {"news", "video", "home", "personal"}:
            return normalized
    return "web"


def _safe_link_payload(link: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("title", "artist", "url", "source", "type", "mode")
    return {
        key: _text(link.get(key))
        for key in keep
        if _text(link.get(key))
    }


def _link_quality(link: Mapping[str, Any]) -> float:
    title = bool(_text(link.get("title")))
    url = bool(_text(link.get("url")))
    source = bool(_text(link.get("source")))
    artist = bool(_text(link.get("artist")))
    return min(1.0, 0.35 + 0.25 * title + 0.25 * url + 0.10 * source + 0.05 * artist)


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


def _candidate_has_material_link(candidate: ProactiveCandidate) -> bool:
    return bool(_candidate_url(candidate))


def _score_gap(candidates: Sequence[ProactiveCandidate]) -> float | None:
    if len(candidates) < 2:
        return None
    return float(candidates[0].score) - float(candidates[1].score)


def _active_bias_fallback(
    reason: str,
    *,
    candidate: ProactiveCandidate | None = None,
    preferred_tag: str | None = None,
    score_gap: float | None = None,
) -> ProactiveActiveBias:
    return ProactiveActiveBias(
        applied=False,
        preferred_source_type=candidate.source_type if candidate else None,
        preferred_source_tag=preferred_tag,
        preferred_candidate_id=candidate.id if candidate else None,
        score_gap=score_gap,
        fallback_reason=reason,
    )


def _active_bias_info(active_bias: ProactiveActiveBias | Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(active_bias, ProactiveActiveBias):
        return active_bias.to_log_dict()
    if isinstance(active_bias, Mapping):
        return {
            "applied": active_bias.get("applied") is True,
            "preferred_source_type": _text(active_bias.get("preferred_source_type")) or None,
            "preferred_source_tag": _text(active_bias.get("preferred_source_tag")).upper() or None,
            "preferred_candidate_id": _text(active_bias.get("preferred_candidate_id")) or None,
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


def _actual_source_aliases(source_mode: Any, source_tag: Any, active_channels: Sequence[str]) -> set[str]:
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


def _raw_source_hint(content: Mapping[str, Any]) -> str:
    raw = content.get("raw_data")
    if isinstance(raw, Mapping):
        return _text(raw.get("source")) or _text(raw.get("region"))
    return ""


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:160]
    return ""


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


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
