"""Conservative assistant-only proposals for a P44-F2-R0 blind manifest.

The proposals are deliberately separate from primary review and cannot satisfy
the human-readiness gate.  They use only fields already visible to reviewers.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import json
import re
from typing import Any, Mapping


DRAFT_POLICY_VERSION = 8
DRAFT_ASSISTANT_ID = "codex-timing-blind-draft-v1"


def build_timing_annotation_assistant_draft(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a proposal copy without changing pending human review fields."""
    output = deepcopy(dict(manifest))
    source_sha = _canonical_sha256(manifest)
    counts: dict[str, int] = {"proposed_true": 0, "proposed_false": 0, "abstained": 0}
    repeat_state: dict[str, Any] = {"counts": Counter(), "last_candidate_keys": set()}
    vision_context_occurrences: Counter[str] = Counter()
    for item in output.get("items") or []:
        candidates = list((item.get("context_for_blind_review") or {}).get("candidates") or [])
        repeat_evidence = _candidate_repeat_evidence(
            candidates,
            repeat_state,
        )
        proposal = _propose(item, repeat_evidence=repeat_evidence)
        if any(str(candidate.get("source_type") or "") == "vision" for candidate in candidates):
            context_fingerprint = _review_context_fingerprint(item)
            prior_occurrences = vision_context_occurrences[context_fingerprint]
            proposal["review_context_episode"] = {
                "fingerprint": context_fingerprint,
                "vision_occurrence": prior_occurrences + 1,
                "new_evidence": prior_occurrences == 0,
            }
            if prior_occurrences:
                # This is review triage, not a scheduling/fatigue formula.  A
                # repeated redacted context cannot establish a new visual
                # episode, so the assistant must defer to a human reviewer.
                proposal.update({
                    "status": "abstained",
                    "should_recommend": None,
                    "reason_code": "insufficient_review_context",
                    "abstain_reason": "repeated_vision_review_context",
                    "preferred_candidate_ids": [],
                })
            vision_context_occurrences[context_fingerprint] += 1
        item["assistant_pre_annotation"] = proposal
        if proposal["status"] == "abstained":
            counts["abstained"] += 1
        elif proposal["should_recommend"] is True:
            counts["proposed_true"] += 1
        else:
            counts["proposed_false"] += 1
        # A draft must never turn into a completed primary review by accident.
        if (item.get("primary_review") or {}).get("status") != "pending":
            raise ValueError("assistant draft requires a pending primary-review manifest")
    output["assistant_pre_annotation_provenance"] = {
        "assistant_id": DRAFT_ASSISTANT_ID,
        "policy_version": DRAFT_POLICY_VERSION,
        "source_manifest_sha256": source_sha,
        "evidence_scope": [
            "activity_state", "candidate source/type/title/summary", "redaction notes",
            "causally bounded pre-decision user/assistant dialogue when recovered",
        ],
        "excluded_evidence": ["delivery", "feedback", "timing", "production score/rank/source"],
        "not_a_human_review": True,
        "counts": counts,
    }
    return output


def _propose(
    item: Mapping[str, Any], *, repeat_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    context = item.get("context_for_blind_review") or {}
    activity = str(context.get("activity_state") or "unknown")
    candidates = list(context.get("candidates") or [])
    sources = {str(candidate.get("source_type") or "") for candidate in candidates}
    recovered = dict(context.get("pre_decision_context") or {})
    has_pre_decision_context = bool(recovered.get("available")) and bool(recovered.get("messages"))
    user_messages = [
        str(message.get("content") or "")
        for message in recovered.get("messages") or []
        if str(message.get("role") or "") == "user"
    ]
    recent_user_text = user_messages[-1] if user_messages else ""
    interruptibility = _interruptibility(activity, recent_user_text)
    candidate_relevance = _candidate_relevance(
        candidates,
        dialogue=recent_user_text,
        repeat_evidence=repeat_evidence or {},
        vision_competition_score=interruptibility["vision_competition_score"],
    )
    preferred_candidate_ids = _preferred_candidate_ids(candidate_relevance)
    common = {
        "assistant_id": DRAFT_ASSISTANT_ID,
        "policy_version": DRAFT_POLICY_VERSION,
        "confidence": "low",
        "comment": (
            "Assistant preliminary only. It used the blind manifest's candidate metadata and "
            "activity state; human confirmation is required."
        ),
        "candidate_relevance": candidate_relevance,
        "preferred_candidate_ids": preferred_candidate_ids,
        "candidate_repeat_evidence": dict(repeat_evidence or {}),
        "interruptibility": interruptibility,
    }
    if not item.get("review_eligible"):
        return {
            **common, "status": "abstained", "should_recommend": None,
            "reason_code": "insufficient_review_context",
            "abstain_reason": "structurally_ineligible",
        }
    if not has_pre_decision_context:
        return {
            **common, "status": "abstained", "should_recommend": None,
            "reason_code": "insufficient_review_context",
            "abstain_reason": "no_causal_dialogue_context",
        }
    # All sources compete on the same 0–3 scale.  Interruptibility changes the
    # vision candidate's competition score instead of creating a separate hard
    # conversational veto.
    if preferred_candidate_ids:
        return {
            **common, "status": "proposed", "should_recommend": True,
            "reason_code": "candidate_appropriate", "abstain_reason": "",
        }
    return {
        **common, "status": "proposed", "should_recommend": False,
        "reason_code": (
            "activity_unsuitable"
            if interruptibility["level"] in {"restricted", "unavailable"}
            else "candidate_irrelevant"
        ),
        "abstain_reason": "",
    }


def _candidate_relevance(
    candidates: list[Mapping[str, Any]], *, dialogue: str,
    repeat_evidence: Mapping[str, Mapping[str, Any]], vision_competition_score: int,
) -> dict[str, int]:
    """Provide a transparent candidate-level draft; it is not a production score."""
    playful = any(marker in dialogue for marker in ("小鱼干", "喵", "猫", "开心", "陪你", "奖励"))
    post_work = any(marker in dialogue for marker in ("忙完", "下班", "休息", "放松"))
    explicit_music_intent = any(marker in dialogue for marker in ("听歌", "放首歌", "音乐", "播歌", "播放音乐"))
    result: dict[str, int] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        source = str(candidate.get("source_type") or "")
        title = f"{candidate.get('safe_title') or ''} {candidate.get('safe_summary') or ''}"
        score = 0
        if source == "vision":
            # screen_context is a redacted placeholder for a production-derived
            # visual context. Semantic relevance is always strong, but its final
            # competition score reflects whether an interruption is appropriate.
            score = vision_competition_score
        elif source == "meme":
            score = 3 if playful and (post_work or any(marker in title for marker in ("开心", "下班", "猫"))) else 2 if playful else 1
        elif source == "music":
            # “忙完”或歌曲标题里的“放松”不等于用户明确想听音乐。
            score = 2 if explicit_music_intent else 1
        elif source in {"news", "video"}:
            # A valid, fresh information candidate is not semantically empty
            # merely because a short final user turn lacks an exact keyword.
            # Specific dialogue/topic evidence raises it above the common
            # baseline; repetition remains a separate downward adjustment.
            score = 2 if any(token and token in dialogue for token in _topic_tokens(title)) else 1
        repeat = repeat_evidence.get(candidate_id) or {}
        occurrence = int(repeat.get("occurrence") or 1)
        if source == "vision":
            pass
        elif occurrence == 2:
            # Every resource loses credit on a repeat. Memes receive the
            # stricter cap because identical reaction images fatigue faster.
            score = min(score, 1) if source == "meme" else max(score - 1, 0)
        elif occurrence >= 3:
            score = 0
        if candidate_id:
            result[candidate_id] = score
    return result


def _interruptibility(activity: str, dialogue: str) -> dict[str, Any]:
    """Map review-visible signals to the score cap for a vision candidate.

    This is a testbench review aid, not a scheduler policy.  Vision starts with
    semantic relevance 3; the returned 0–3 value is its final score when ranked
    alongside music, news, meme, and video candidates.
    """
    tension_markers = ("什么意思", "不对", "不是", "搞错", "为什么")
    visual_focus_markers = (
        "屏幕", "窗口", "界面", "画面", "截图", "这个页面", "这个窗口",
        "看这个", "看一下这个", "这张图",
    )
    if activity == "away":
        return {
            "level": "unavailable", "vision_semantic_relevance": 3,
            "vision_competition_score": 0, "reason": "activity_away",
        }
    if activity in {"busy", "gaming", "focused_work", "chatting"} or any(
        marker in dialogue for marker in tension_markers
    ):
        return {
            "level": "restricted", "vision_semantic_relevance": 3,
            "vision_competition_score": 0,
            "reason": "activity_or_dialogue_not_interruptible",
        }
    if activity == "idle":
        if any(marker in dialogue for marker in visual_focus_markers):
            return {
                "level": "open_visual_focus", "vision_semantic_relevance": 3,
                "vision_competition_score": 3, "reason": "idle_explicit_visual_focus",
            }
        return {
            "level": "open", "vision_semantic_relevance": 3,
            # Vision remains advantaged over a fresh generic candidate (1),
            # but should not monopolise the maximum competition score.
            "vision_competition_score": 2, "reason": "idle",
        }
    return {
        "level": "uncertain", "vision_semantic_relevance": 3,
        "vision_competition_score": 1, "reason": "activity_unknown",
    }


def _topic_tokens(text: str) -> tuple[str, ...]:
    # Long, domain-specific tokens avoid pretending that generic particles are
    # semantic evidence.  This is only a conservative draft aid.
    terms = ("炉石", "帕鲁", "游戏", "猫娘", "AI", "音乐", "小鱼干", "世界杯", "王者")
    return tuple(term for term in terms if term in text)


def _preferred_candidate_ids(relevance: Mapping[str, int]) -> list[str]:
    if not relevance:
        return []
    highest = max(relevance.values())
    return sorted(candidate_id for candidate_id, score in relevance.items() if score == highest and score >= 2)


def _candidate_repeat_evidence(
    candidates: list[Mapping[str, Any]], state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Track stable candidate identity in freeze order, without outcome data."""
    evidence: dict[str, dict[str, Any]] = {}
    current_keys: set[str] = set()
    counts: Counter[str] = state["counts"]
    previous: set[str] = state["last_candidate_keys"]
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        source = str(candidate.get("source_type") or "unknown")
        if source == "vision":
            # A shared `screen_context` title is intentionally non-identifying:
            # it cannot establish that two screenshots/materials are repeats.
            evidence[candidate_id] = {
                "occurrence": None,
                "consecutive": None,
                "penalty": "not_applicable_visual_context",
            }
            continue
        title = re.sub(r"\s+", " ", str(candidate.get("safe_title") or "").strip()).casefold()
        summary = re.sub(r"\s+", " ", str(candidate.get("safe_summary") or "").strip()).casefold()
        # Semantic text catches the same news item even if a crawler regenerated
        # its candidate ID. Stable ID is the fallback for title-less records.
        key = f"{source}|{title}|{summary}" if title or summary else candidate_id
        occurrence = counts[key] + 1
        evidence[candidate_id] = {
            "occurrence": occurrence,
            "consecutive": key in previous,
            "penalty": (
                "none" if occurrence == 1 else
                "meme_cap_1" if occurrence == 2 and source == "meme" else
                "minus_1" if occurrence == 2 else "zero"
            ),
        }
        counts[key] = occurrence
        current_keys.add(key)
    state["last_candidate_keys"] = current_keys
    return evidence


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _review_context_fingerprint(item: Mapping[str, Any]) -> str:
    """Hash the redacted, causal dialogue solely for offline review deduplication."""
    context = item.get("context_for_blind_review") or {}
    pre_decision = context.get("pre_decision_context") or {}
    messages = list(pre_decision.get("messages") or [])
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = ["build_timing_annotation_assistant_draft"]
