"""Privacy validation and summaries for recommendation review context."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import re
from typing import Any

from .schema import (
    REVIEW_CONTEXT_MAX_CANDIDATES,
    REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH,
    REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH,
    REVIEW_CONTEXT_SAFE_TITLE_MAX_LENGTH,
    _REVIEW_CANDIDATE_LABEL_KEYS,
    _REVIEW_CONTEXT_KEYS,
    sanitize_recommendation_review_context,
)

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
_REVIEW_URL_RE = re.compile(r"https?://[^\\s]+", re.IGNORECASE)
_REVIEW_SECRET_RE = re.compile(
    r"\\b(token|cookie|authorization|api[_-]?key|session[_-]?id)\\s*[:=]\\s*[^\\s,;]+",
    re.IGNORECASE,
)


def validate_recommendation_review_context(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
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
    if (
        not isinstance(labels, Sequence)
        or isinstance(labels, (str, bytes))
        or not labels
    ):
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
        if (
            len(str(raw.get("safe_summary") or ""))
            > REVIEW_CONTEXT_SAFE_SUMMARY_MAX_LENGTH
        ):
            issues.append("review_context_safe_summary_too_long")
    if (
        len(str(value.get("delivered_excerpt") or ""))
        > REVIEW_CONTEXT_DELIVERED_EXCERPT_MAX_LENGTH
    ):
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
