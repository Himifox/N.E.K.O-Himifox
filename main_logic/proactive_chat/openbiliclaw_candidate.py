# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
"""Prompt-bounded OpenBiliClaw candidate projections."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TrackingLayer:
    """Code-only delivery state. This object must never be rendered in a prompt."""

    candidate_id: str
    item_key: str
    url: str
    expires_at: str | None
    delivery_ref: Any = field(repr=False, compare=False)
    sensitivity: str = "none"
    proactive_policy: str = "allow"
    why_now_source: str = "none"


@dataclass(frozen=True, slots=True)
class Phase1Candidate:
    title: str
    topic: str
    summary: str
    why_now: str
    reason_codes: tuple[str, ...]
    source_platform: str
    author_name: str
    content_type: str
    confidence: float
    freshness: str


@dataclass(frozen=True, slots=True)
class Phase2Candidate:
    title: str
    topic: str
    summary: str
    why_now: str


@dataclass(frozen=True, slots=True)
class OpenBiliClawCandidateEnvelope:
    tracking: TrackingLayer
    phase1: Phase1Candidate
    phase2: Phase2Candidate


_WHY_NOW_TEMPLATES: dict[str, dict[str, str]] = {
    "zh": {
        "recent_interest": "这个话题正好接得上近期对 {topic} 的关注",
        "long_term_interest": "{topic} 一直是比较契合的方向",
        "topic_continuation": "可以延续最近关于 {topic} 的话题",
        "related_to_saved": "和近期关注的 {topic} 方向有关",
        "emerging_interest": "{topic} 是最近逐渐出现的新兴趣",
        "source_affinity": "这类 {topic} 内容通常比较合适",
        "current_conversation": "可以接着刚才关于 {topic} 的话题",
        "explicit_subscription": "这是你选择关注的 {topic} 最新动态",
        "public_timing": "这是近期值得关注的新进展",
        "default": "这是近期值得关注的话题",
    },
    "en": {
        "recent_interest": "This connects with your recent interest in {topic}",
        "long_term_interest": "{topic} remains a fitting direction",
        "topic_continuation": "This can continue the recent {topic} thread",
        "related_to_saved": "This relates to the {topic} direction you follow",
        "emerging_interest": "{topic} is becoming a new interest",
        "source_affinity": "This kind of {topic} content is usually a good fit",
        "current_conversation": "This continues what you just said about {topic}",
        "explicit_subscription": "This is a new update for your {topic} subscription",
        "public_timing": "This is a timely new development",
        "default": "This is a timely topic worth discussing",
    },
    "ja": {
        "recent_interest": "最近の{topic}への関心につながる話題です",
        "long_term_interest": "{topic}は継続的に相性のよい方向です",
        "topic_continuation": "最近の{topic}の話を続けられます",
        "related_to_saved": "最近注目している{topic}に関係します",
        "emerging_interest": "{topic}は最近生まれつつある関心です",
        "source_affinity": "この種の{topic}は普段の関心に合います",
        "current_conversation": "さっきの{topic}の話を続けられます",
        "explicit_subscription": "登録した{topic}の最新情報です",
        "public_timing": "最近注目に値する新しい動きです",
        "default": "最近話す価値のある話題です",
    },
}

_PHASE2_INSTRUCTION = {
    "zh": "why_now 仅是选题动机，不要逐字复述。",
    "en": "why_now is only a selection motive; do not repeat it verbatim.",
    "ja": "why_now は話題選択の理由であり、そのまま復唱しないでください。",
}


def _language_family(language: str) -> str:
    normalized = str(language or "").lower()
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("ja"):
        return "ja"
    return "en"


def _why_now(core_candidate: Any, language: str) -> str:
    semantics = core_candidate.semantics
    policy = core_candidate.policy
    templates = _WHY_NOW_TEMPLATES[_language_family(language)]
    reason_codes = tuple(semantics.reason_codes or ())
    if policy.why_now_source in {"current_conversation", "explicit_subscription"}:
        template_key = policy.why_now_source
    elif reason_codes:
        template_key = str(reason_codes[0])
    elif policy.why_now_source == "public_timing" or semantics.freshness == "recent":
        template_key = "public_timing"
    else:
        template_key = "default"
    rendered = templates.get(template_key, templates["default"]).format(
        topic=str(semantics.topic)
    )
    return rendered[:40]


def project_openbiliclaw_candidate(
    core_candidate: Any,
    *,
    language: str,
) -> OpenBiliClawCandidateEnvelope:
    """Project one Core object into mutually isolated tracking and prompt layers."""

    tracking = core_candidate.tracking
    semantics = core_candidate.semantics
    policy = core_candidate.policy
    why_now = _why_now(core_candidate, language)
    phase1 = Phase1Candidate(
        title=str(semantics.title)[:60],
        topic=str(semantics.topic)[:16],
        summary=str(semantics.summary)[:80],
        why_now=why_now,
        reason_codes=tuple(str(code) for code in semantics.reason_codes[:3]),
        source_platform=str(semantics.source_platform),
        author_name=str(semantics.author_name),
        content_type=str(semantics.content_type),
        confidence=min(1.0, max(0.0, float(semantics.confidence))),
        freshness=str(semantics.freshness),
    )
    return OpenBiliClawCandidateEnvelope(
        tracking=TrackingLayer(
            candidate_id=str(tracking.candidate_id),
            item_key=str(tracking.item_key),
            url=str(tracking.url),
            expires_at=tracking.expires_at,
            delivery_ref=tracking.delivery_ref,
            sensitivity=str(policy.sensitivity),
            proactive_policy=str(policy.proactive_policy),
            why_now_source=str(policy.why_now_source),
        ),
        phase1=phase1,
        phase2=Phase2Candidate(
            title=phase1.title,
            topic=phase1.topic,
            summary=phase1.summary,
            why_now=phase1.why_now,
        ),
    )


def is_proactive_candidate_allowed(core_candidate: Any) -> bool:
    """Fail closed if a malformed Core object crosses the sensitive-topic gate."""

    policy = core_candidate.policy
    sensitivity = str(policy.sensitivity)
    proactive_policy = str(policy.proactive_policy)
    why_now_source = str(policy.why_now_source)
    if proactive_policy == "deny":
        return False
    if sensitivity == "none":
        return proactive_policy == "allow"
    return (
        proactive_policy == "explicit_context_only"
        and why_now_source == "current_conversation"
    ) or (
        proactive_policy == "explicit_context_or_subscription"
        and why_now_source == "explicit_subscription"
    )


def openbiliclaw_link(envelope: OpenBiliClawCandidateEnvelope) -> dict[str, Any]:
    """Return the established public link shape plus one private envelope."""

    return {
        "title": envelope.phase1.title,
        "url": envelope.tracking.url,
        "source": "OpenBiliClaw",
        "mode": "openbiliclaw",
        "_openbiliclaw_candidate": envelope,
    }


def format_phase1_candidate(index: int, envelope: OpenBiliClawCandidateEnvelope) -> str:
    """Render only the Phase 1 projection, keyed by its external sequence number."""

    phase1 = envelope.phase1
    payload = {
        "title": phase1.title,
        "topic": phase1.topic,
        "summary": phase1.summary,
        "why_now": phase1.why_now,
        "reason_codes": list(phase1.reason_codes),
        "source_platform": phase1.source_platform,
        "author_name": phase1.author_name,
        "content_type": phase1.content_type,
        "confidence": phase1.confidence,
        "freshness": phase1.freshness,
    }
    return f"{index}. {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"


def format_phase2_candidate(
    envelope: OpenBiliClawCandidateEnvelope,
    *,
    language: str = "zh",
) -> str:
    """Render exactly one four-field semantic object for final expression."""

    phase2 = envelope.phase2
    payload = {
        "title": phase2.title,
        "topic": phase2.topic,
        "summary": phase2.summary,
        "why_now": phase2.why_now,
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n"
        + _PHASE2_INSTRUCTION[_language_family(language)]
    )


def recent_user_context(session: Any, *, limit: int = 3) -> tuple[str, ...]:
    """Read the active in-memory user tail without persisting or logging it."""

    history = getattr(session, "_conversation_history", None)
    if not isinstance(history, list):
        return ()
    result: list[str] = []
    for message in reversed(history):
        role = str(getattr(message, "type", "") or getattr(message, "role", "")).lower()
        if role not in {"human", "user"}:
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            result.append(content.strip())
        if len(result) >= limit:
            break
    return tuple(reversed(result))
