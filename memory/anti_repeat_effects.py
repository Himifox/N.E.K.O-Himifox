# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Privacy-bounded, per-character observability for runtime anti-repeat decisions.

The corpus remains the source of scoring context.  This module stores only
daily aggregate counters and short repeated fragments extracted from detector
evidence; it never persists complete rejected drafts, user text, prompts, URLs,
code, or templates.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from utils.config_manager import get_config_manager
from utils.file_utils import atomic_write_json
from utils.logger_config import get_module_logger
from utils.natural_expression_candidates import (
    _URL_RE,
    _runtime_protected_spans,
    normalize_language,
)

logger = get_module_logger(__name__, "Memory")

SCHEMA_VERSION = "anti-repeat-effects/v1"
RETENTION_DAYS = 120
MAX_PATTERNS_PER_DAY = 64
MAX_RESPONSE_BUCKETS = 512
MAX_PHRASE_CHARS = 48
_DEFAULT_KEY = "default"
_BLOCKED_OUTCOMES = frozenset(
    {
        "blocked_initial",
        "blocked_after_regen_bm25",
        "blocked_after_regen_unanswered",
        "blocked_after_regen_literal",
        "regen_failed",
        "break_reminder_suppressed",
    }
)
_VALID_REASONS = frozenset({"literal_similarity", "bm25", "unanswered_repeat"})
_VALID_SOURCES = frozenset({"regular_prompt", "proactive", "break_reminder"})
_VALID_ACTIONS = frozenset({"block", "regenerate"})
_VALID_OUTCOMES = _BLOCKED_OUTCOMES | {
    "regen_guard_passed",
    "abandoned_user_interaction",
}
_PROTECTED_RE = re.compile(
    r"```[\s\S]*?```|`[^`\r\n]+`|"
    # Same bounded multiline containers as the miner's `_TEMPLATE_RE`, and for
    # the same reason: a template body that merely wrapped was left searchable
    # here, so evidence taken from inside it could reach the sidecar even though
    # its single-line twin was rejected. `<...>` stays line-bounded -- see the
    # miner for the speech it swallows otherwise.
    r"\{\{[^{}\r\n]*(?:\r?\n[^{}\r\n]*){0,3}\}\}|"
    r"\$\{[^{}\r\n]*(?:\r?\n[^{}\r\n]*){0,3}\}|"
    r"<%[^%\r\n]*(?:\r?\n[^%\r\n]*){0,3}%>|"
    r"<[^<>\r\n]{1,80}>|"
    r"\[[A-Z][A-Z0-9_-]{1,63}\]"
)
# How many times a reset re-cuts when a concurrent write lands after its
# flush. Each retry needs a racing writer to win again, so a small bound is
# enough; exhausting it reports failure rather than claiming a cut that the
# racer already overwrote.
_CLEAR_ATTEMPTS = 3
_EDGE_PUNCTUATION = " \t\r\n,，。.!！?？;；:：、()（）[]【】{}<>《》\"'“”‘’"


def _resolve_name(name: str | None) -> str:
    return name or _DEFAULT_KEY


def _utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def _normalized_phrase(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", normalized).strip(_EDGE_PUNCTUATION).casefold()


def _safe_fragment(value: str) -> str:
    fragment = unicodedata.normalize("NFKC", value or "")
    if _URL_RE.search(fragment) or _PROTECTED_RE.search(fragment):
        return ""
    fragment = re.sub(r"\s+", " ", fragment).strip(_EDGE_PUNCTUATION)
    if not fragment:
        return ""
    if len(fragment) > MAX_PHRASE_CHARS:
        return ""
    return fragment


def _without_protected_text(value: str) -> str:
    spans = _runtime_protected_spans(value)
    spans.extend(match.span() for match in _PROTECTED_RE.finditer(value))
    spans.sort()
    chunks: list[str] = []
    cursor = 0
    for start, end in spans:
        if end <= cursor:
            continue
        if start > cursor:
            chunks.append(value[cursor:start])
        chunks.append(" ")
        cursor = end
    chunks.append(value[cursor:])
    return "".join(chunks)


@dataclass(frozen=True, slots=True)
class RepeatSignature:
    phrase: str
    normalized_phrase: str
    language: str


# Sentinel for recorder closures that capture the INITIAL draft's signature:
# it distinguishes "keep what the closure captured" from an explicit ``None``
# (a deliberately unattributed record). Shared so every recorder in
# ``main_logic.proactive_chat`` spells the override the same way.
KEEP_INITIAL_SIGNATURE = object()


@dataclass(frozen=True, slots=True)
class AntiRepeatDecision:
    source: str
    reasons: tuple[str, ...]
    action: str
    outcome: str
    signature: RepeatSignature | None = None
    score_before: float | None = None
    score_after: float | None = None
    response_id: str | None = None

    def validate(self) -> None:
        if self.source not in _VALID_SOURCES:
            raise ValueError("unsupported anti-repeat decision source")
        if self.action not in _VALID_ACTIONS:
            raise ValueError("unsupported anti-repeat decision action")
        if self.outcome not in _VALID_OUTCOMES:
            raise ValueError("unsupported anti-repeat decision outcome")
        if not self.reasons or any(
            reason not in _VALID_REASONS for reason in self.reasons
        ):
            raise ValueError("unsupported anti-repeat decision reason")
        if self.response_id is not None and not (0 < len(self.response_id) <= 128):
            raise ValueError("invalid anti-repeat response ID")


def build_repeat_signature(
    draft_text: str,
    evidence_terms: Iterable[str] = (),
    *,
    language: str,
    fallback_fragment: str = "",
) -> RepeatSignature | None:
    """Build one short, readable detector signature without retaining a draft."""
    try:
        normalized_language = normalize_language(language)
    except Exception:
        return None

    draft_normalized = unicodedata.normalize("NFKC", draft_text or "")
    full_draft_phrase = _normalized_phrase(draft_normalized)
    unprotected_draft_phrase = _normalized_phrase(
        _without_protected_text(draft_normalized)
    )
    candidates: list[str] = []
    fallback = _safe_fragment(fallback_fragment)
    if fallback:
        candidates.append(fallback)
    seen: set[str] = set()
    for raw_term in evidence_terms:
        term = _safe_fragment(str(raw_term))
        normalized = _normalized_phrase(term)
        if not term or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in _normalized_phrase(draft_normalized):
            candidates.append(term)
    for phrase in candidates:
        normalized = _normalized_phrase(phrase)
        if (
            len(normalized) < 2
            or normalized == full_draft_phrase
            or normalized not in unprotected_draft_phrase
        ):
            continue
        return RepeatSignature(phrase, normalized, normalized_language)
    return None


def _default_payload(now: float) -> dict[str, Any]:
    return {
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "started_at": now,
        "daily_buckets": {},
        "response_buckets": {},
    }


def _response_key(response_id: str) -> str:
    return hashlib.sha256(response_id.encode("utf-8")).hexdigest()[:24]


def _empty_counters() -> dict[str, int]:
    return {
        "soft_hint_injected": 0,
        "detected": 0,
        "regen_triggered": 0,
        "regen_guard_passed": 0,
        "blocked_delivery": 0,
        "break_reminder_suppressed": 0,
        "abandoned_user_interaction": 0,
        "unattributed": 0,
    }


def _empty_bucket() -> dict[str, Any]:
    return {
        "counters": _empty_counters(),
        "reason_counts": {reason: 0 for reason in sorted(_VALID_REASONS)},
        "bm25": {"before_sum": 0.0, "after_sum": 0.0, "pair_count": 0},
        "patterns": {},
    }


def _copy_bucket(raw: Any) -> dict[str, Any]:
    """Copy one aggregate bucket so no dict is shared with the live cache."""
    if not isinstance(raw, dict):
        return _empty_bucket()
    counters = raw.get("counters")
    reason_counts = raw.get("reason_counts")
    bm25 = raw.get("bm25")
    patterns = raw.get("patterns")
    return {
        "counters": dict(counters) if isinstance(counters, dict) else _empty_counters(),
        "reason_counts": dict(reason_counts) if isinstance(reason_counts, dict) else {},
        "bm25": dict(bm25) if isinstance(bm25, dict) else {},
        "patterns": (
            {
                pattern_id: {
                    **pattern,
                    "reasons": (
                        dict(pattern["reasons"])
                        if isinstance(pattern.get("reasons"), dict)
                        else {}
                    ),
                }
                for pattern_id, pattern in patterns.items()
                if isinstance(pattern, dict)
            }
            if isinstance(patterns, dict)
            else {}
        ),
    }


def _copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the normalized schema without a JSON serialize/parse round trip.

    Every container in the fixed schema is rebuilt and every leaf is a JSON
    scalar, so the result shares no mutable object with the live cache. That
    isolation is the whole point and cannot be traded for structural sharing:
    ``_flush_snapshot`` serializes the snapshot on a background thread while
    holding only the WRITE lock, whereas ``_apply_decision`` / ``_prune`` mutate
    the cached payload under the separate per-name lock. Reusing "unchanged"
    sub-dicts would therefore let ``atomic_write_json`` iterate a dict that
    another turn is concurrently writing to — a torn file at best, and a
    ``RuntimeError: dictionary changed size during iteration`` at worst.
    """
    daily_buckets = payload.get("daily_buckets")
    response_buckets = payload.get("response_buckets")
    return {
        "version": payload.get("version", 1),
        "schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "started_at": payload.get("started_at", 0.0),
        "daily_buckets": (
            {day: _copy_bucket(bucket) for day, bucket in daily_buckets.items()}
            if isinstance(daily_buckets, dict)
            else {}
        ),
        "response_buckets": (
            {
                response_key: {
                    "created_at": response.get("created_at", 0.0),
                    "delivered_at": response.get("delivered_at", 0.0),
                    "bucket": _copy_bucket(response.get("bucket")),
                }
                for response_key, response in response_buckets.items()
                if isinstance(response, dict)
            }
            if isinstance(response_buckets, dict)
            else {}
        ),
    }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_float(value: Any) -> float:
    try:
        normalized = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, normalized) if math.isfinite(normalized) else 0.0


def _response_retention_key(response: Any) -> tuple[bool, float]:
    if not isinstance(response, dict):
        return False, 0.0
    created_at = _as_float(response.get("created_at"))
    delivered_at = _as_float(response.get("delivered_at"))
    return delivered_at > 0, max(created_at, delivered_at)


def _normalize_bucket(raw: Any) -> dict[str, Any]:
    bucket = _empty_bucket()
    if not isinstance(raw, dict):
        return bucket
    raw_counters = raw.get("counters")
    if isinstance(raw_counters, dict):
        for key in bucket["counters"]:
            bucket["counters"][key] = _as_int(raw_counters.get(key))
    raw_reasons = raw.get("reason_counts")
    if isinstance(raw_reasons, dict):
        for reason in bucket["reason_counts"]:
            bucket["reason_counts"][reason] = _as_int(raw_reasons.get(reason))
    raw_bm25 = raw.get("bm25")
    if isinstance(raw_bm25, dict):
        bucket["bm25"] = {
            "before_sum": _as_float(raw_bm25.get("before_sum")),
            "after_sum": _as_float(raw_bm25.get("after_sum")),
            "pair_count": _as_int(raw_bm25.get("pair_count")),
        }
    raw_patterns = raw.get("patterns")
    if isinstance(raw_patterns, dict):
        for pattern_id, raw_pattern in list(raw_patterns.items())[
            :MAX_PATTERNS_PER_DAY
        ]:
            if not isinstance(pattern_id, str) or not isinstance(raw_pattern, dict):
                continue
            phrase = _safe_fragment(str(raw_pattern.get("phrase", "")))
            normalized = _normalized_phrase(phrase)
            language = str(raw_pattern.get("language", ""))
            if not phrase or not normalized or not language:
                continue
            reasons = raw_pattern.get("reasons")
            bucket["patterns"][pattern_id] = {
                "phrase": phrase,
                "normalized_phrase": normalized,
                "language": language,
                "reasons": {
                    reason: _as_int(reasons.get(reason))
                    for reason in sorted(_VALID_REASONS)
                    if isinstance(reasons, dict) and _as_int(reasons.get(reason))
                },
                "detected_count": _as_int(raw_pattern.get("detected_count")),
                "regen_triggered_count": _as_int(
                    raw_pattern.get("regen_triggered_count")
                ),
                "regen_guard_passed_count": _as_int(
                    raw_pattern.get("regen_guard_passed_count")
                ),
                "blocked_count": _as_int(raw_pattern.get("blocked_count")),
                "last_seen_at": _as_float(raw_pattern.get("last_seen_at")),
            }
    return bucket


def _apply_decision_to_bucket(
    bucket: dict[str, Any],
    decision: AntiRepeatDecision,
    now: float,
) -> None:
    """Apply one decision to a response-scoped aggregate bucket."""
    counters = bucket.setdefault("counters", _empty_counters())
    counters["detected"] = int(counters.get("detected", 0)) + 1
    if decision.action == "regenerate":
        counters["regen_triggered"] = int(counters.get("regen_triggered", 0)) + 1
    if decision.outcome == "regen_guard_passed":
        counters["regen_guard_passed"] = int(counters.get("regen_guard_passed", 0)) + 1
    if decision.outcome in _BLOCKED_OUTCOMES:
        counters["blocked_delivery"] = int(counters.get("blocked_delivery", 0)) + 1
    if decision.outcome == "break_reminder_suppressed":
        counters["break_reminder_suppressed"] = (
            int(counters.get("break_reminder_suppressed", 0)) + 1
        )
    if decision.outcome == "abandoned_user_interaction":
        counters["abandoned_user_interaction"] = (
            int(counters.get("abandoned_user_interaction", 0)) + 1
        )

    reason_counts = bucket.setdefault("reason_counts", {})
    for reason in dict.fromkeys(decision.reasons):
        reason_counts[reason] = int(reason_counts.get(reason, 0)) + 1

    bm25 = bucket.setdefault("bm25", {})
    if (
        decision.score_before is not None
        and decision.score_after is not None
        and decision.score_before > 0
    ):
        bm25["before_sum"] = float(bm25.get("before_sum", 0.0)) + float(
            decision.score_before
        )
        bm25["after_sum"] = float(bm25.get("after_sum", 0.0)) + max(
            0.0, float(decision.score_after)
        )
        bm25["pair_count"] = int(bm25.get("pair_count", 0)) + 1

    signature = decision.signature
    patterns = bucket.setdefault("patterns", {})
    if signature is None:
        counters["unattributed"] = int(counters.get("unattributed", 0)) + 1
        return
    pattern_id = hashlib.sha256(
        f"{signature.language}\0{signature.normalized_phrase}".encode("utf-8")
    ).hexdigest()[:16]
    pattern = patterns.get(pattern_id)
    if pattern is None and len(patterns) >= MAX_PATTERNS_PER_DAY:
        counters["unattributed"] = int(counters.get("unattributed", 0)) + 1
        return
    if pattern is None:
        pattern = {
            "phrase": signature.phrase,
            "normalized_phrase": signature.normalized_phrase,
            "language": signature.language,
            "reasons": {},
            "detected_count": 0,
            "regen_triggered_count": 0,
            "regen_guard_passed_count": 0,
            "blocked_count": 0,
            "last_seen_at": now,
        }
        patterns[pattern_id] = pattern
    pattern["detected_count"] += 1
    if decision.action == "regenerate":
        pattern["regen_triggered_count"] += 1
    if decision.outcome == "regen_guard_passed":
        pattern["regen_guard_passed_count"] += 1
    if decision.outcome in _BLOCKED_OUTCOMES:
        pattern["blocked_count"] += 1
    pattern["last_seen_at"] = max(float(pattern.get("last_seen_at", 0.0)), now)
    for reason in dict.fromkeys(decision.reasons):
        pattern["reasons"][reason] = int(pattern["reasons"].get(reason, 0)) + 1


def _summarize_effect_buckets(buckets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    totals = _empty_counters()
    reason_totals = {reason: 0 for reason in sorted(_VALID_REASONS)}
    before_sum = after_sum = 0.0
    pair_count = 0
    patterns_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for bucket in buckets:
        for key, value in bucket.get("counters", {}).items():
            if key in totals:
                totals[key] += _as_int(value)
        for key, value in bucket.get("reason_counts", {}).items():
            if key in reason_totals:
                reason_totals[key] += _as_int(value)
        bm25 = bucket.get("bm25", {})
        before_sum += _as_float(bm25.get("before_sum"))
        after_sum += _as_float(bm25.get("after_sum"))
        pair_count += _as_int(bm25.get("pair_count"))
        for pattern in bucket.get("patterns", {}).values():
            if not isinstance(pattern, dict):
                continue
            key = (
                str(pattern.get("language", "")),
                str(pattern.get("normalized_phrase", "")),
            )
            if not all(key):
                continue
            merged = patterns_by_key.setdefault(
                key,
                {
                    "phrase": str(pattern.get("phrase", "")),
                    "normalized_phrase": key[1],
                    "language": key[0],
                    "reasons": {},
                    "detected_count": 0,
                    "regen_triggered_count": 0,
                    "regen_guard_passed_count": 0,
                    "blocked_count": 0,
                    "last_seen_at": 0.0,
                },
            )
            for field in (
                "detected_count",
                "regen_triggered_count",
                "regen_guard_passed_count",
                "blocked_count",
            ):
                merged[field] += _as_int(pattern.get(field))
            merged["last_seen_at"] = max(
                merged["last_seen_at"],
                _as_float(pattern.get("last_seen_at")),
            )
            for reason, count in pattern.get("reasons", {}).items():
                if reason in _VALID_REASONS:
                    merged["reasons"][reason] = int(
                        merged["reasons"].get(reason, 0)
                    ) + _as_int(count)

    average_before = before_sum / pair_count if pair_count else 0.0
    average_after = after_sum / pair_count if pair_count else 0.0
    reduction_ratio = 1.0 - after_sum / before_sum if before_sum > 0 else 0.0
    patterns = sorted(
        patterns_by_key.values(),
        key=lambda item: (
            -item["blocked_count"],
            -item["detected_count"],
            item["normalized_phrase"],
        ),
    )
    return {
        "totals": totals,
        "reason_counts": reason_totals,
        "bm25": {
            "pair_count": pair_count,
            "average_before": round(average_before, 4),
            "average_after": round(average_after, 4),
            "reduction_ratio": round(reduction_ratio, 4),
        },
        "patterns": patterns,
    }


class AntiRepeatEffectStore:
    """Thread-safe daily aggregate store with ordered atomic snapshots."""

    def __init__(self) -> None:
        self._config_manager = get_config_manager()
        self._cache: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._write_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._staged_seq: dict[str, int] = {}
        self._written_seq: dict[str, int] = {}
        # Names retired by ``evict_character``; see ``_write_file_path``.
        self._retired: set[str] = set()
        # Bumped whenever an identity is dropped from the cache. A sequence
        # fence cannot stand in for this: eviction fences to exactly the
        # sequence already staged, so "did the sequence advance" reads NO for
        # an eviction and a reset cannot tell it from a quiet window.
        self._evict_generation: dict[str, int] = {}
        self._detached_flushes: set[asyncio.Task] = set()

    def _existing_file_path(self, name: str) -> str:
        return os.path.join(
            str(self._config_manager.memory_dir),
            name,
            "anti_repeat_effects.json",
        )

    def _write_file_path(self, name: str) -> str | None:
        """Resolve the write target, or ``None`` for a retired identity.

        The normal path stays identical to every sibling memory writer
        (``facts.py``, ``event_log.py``, ``cursors.py``, ``anti_repeat.py`` …):
        ``ensure_character_dir`` lazily creates ``memory/<name>/`` on first write,
        because nothing creates it eagerly at character-creation time.

        The exception is a name ``retire_character`` retired. Fencing alone only
        covers snapshots staged *before* the eviction, so a decision
        recorded while delete/rename was still in flight would call
        ``ensure_character_dir`` and ``makedirs`` the directory back into
        existence right after ``shutil.rmtree`` removed it — leaving an orphan
        that makes ``character_memory_exists`` report the removed character
        again.

        A retired name may only write into a directory that already exists: it
        never creates one. Inferring "the identity is live again" from directory
        existence would be wrong, because ``delete_character_memory_storage``
        retires *before* it removes the tree. A flush landing in that window
        still sees the doomed directory, and lifting retirement there would
        disarm the guard for every later flush -- exactly the resurrection this
        exists to prevent. Only ``evict_character`` lifts it, and only callers
        that KNOW an identity is live reach for it: a rename target and a
        cloud-save import. Retiring those would be wrong in the other
        direction -- an imported profile that ships no managed memory files has
        no directory yet, and a retired name never creates one, so its
        aggregates would keep failing to persist while the character is in
        active use.
        """
        from memory import ensure_character_dir

        memory_dir = self._config_manager.memory_dir
        if name in self._retired:
            character_dir = os.path.join(str(memory_dir), name)
            if not os.path.isdir(character_dir):
                return None
            return os.path.join(character_dir, "anti_repeat_effects.json")
        return os.path.join(
            ensure_character_dir(memory_dir, name),
            "anti_repeat_effects.json",
        )

    def _get_lock(self, name: str) -> threading.Lock:
        if name not in self._locks:
            with self._locks_guard:
                self._locks.setdefault(name, threading.Lock())
        return self._locks[name]

    def _get_write_lock(self, name: str) -> threading.Lock:
        if name not in self._write_locks:
            with self._locks_guard:
                self._write_locks.setdefault(name, threading.Lock())
        return self._write_locks[name]

    @staticmethod
    def _normalize_payload(raw: Any, now: float) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return _default_payload(now)
        payload = _default_payload(now)
        payload["started_at"] = _as_float(raw.get("started_at")) or now
        buckets = raw.get("daily_buckets")
        if isinstance(buckets, dict):
            payload["daily_buckets"] = {
                day: _normalize_bucket(bucket)
                for day, bucket in buckets.items()
                if isinstance(day, str) and isinstance(bucket, dict)
            }
        response_buckets = raw.get("response_buckets")
        if isinstance(response_buckets, dict):
            normalized_response_buckets: dict[str, dict[str, Any]] = {}
            retained_responses = heapq.nlargest(
                MAX_RESPONSE_BUCKETS,
                (
                    (response_key, raw_response)
                    for response_key, raw_response in response_buckets.items()
                    if isinstance(response_key, str)
                    and isinstance(raw_response, dict)
                ),
                key=lambda item: _response_retention_key(item[1]),
            )
            for response_key, raw_response in retained_responses:
                normalized_response_buckets[response_key] = {
                    "created_at": _as_float(raw_response.get("created_at")),
                    "delivered_at": _as_float(raw_response.get("delivered_at")),
                    "bucket": _normalize_bucket(raw_response.get("bucket")),
                }
            payload["response_buckets"] = normalized_response_buckets
        return payload

    def _read_payload_from_disk(self, name: str, now: float) -> dict[str, Any]:
        path = self._existing_file_path(name)
        if not os.path.exists(path):
            return _default_payload(now)
        try:
            with open(path, encoding="utf-8") as handle:
                return self._normalize_payload(json.load(handle), now)
        except Exception as exc:
            logger.warning(
                "[AntiRepeatEffects] load failed for %s: %s",
                name,
                type(exc).__name__,
            )
            return _default_payload(now)

    def _load_unlocked(self, name: str, now: float) -> dict[str, Any]:
        if name not in self._cache:
            self._cache[name] = self._read_payload_from_disk(name, now)
        return self._cache[name]

    @staticmethod
    def _prune(
        payload: dict[str, Any],
        now: float,
        *,
        protect_response_key: str | None = None,
    ) -> bool:
        """Drop expired and over-capacity aggregates.

        ``protect_response_key`` fences the bucket the caller is in the middle of
        writing. Capacity eviction prefers to keep DELIVERED buckets, so a freshly
        created one — always ``delivered_at == 0`` — sorts ahead of every delivered
        bucket and would be the first victim. Once the store reaches steady state
        (every retained bucket delivered, because delivery converts them and
        blocked ones are evicted first) that made each turn delete the very bucket
        it had just created, so ``stage_response_delivered`` never found it again
        and message linkage stopped for good.
        """
        changed = False
        cutoff = (
            datetime.fromtimestamp(now, timezone.utc).date()
            - timedelta(days=RETENTION_DAYS - 1)
        ).isoformat()
        current_day = _utc_day(now)
        buckets = payload.get("daily_buckets", {})
        for day in list(buckets):
            if day < cutoff or day > current_day:
                del buckets[day]
                changed = True
        response_buckets = payload.get("response_buckets", {})
        cutoff_timestamp = now - RETENTION_DAYS * 24 * 60 * 60
        for response_key, response in list(response_buckets.items()):
            if response_key == protect_response_key:
                continue
            retention_timestamp = (
                max(
                    _as_float(response.get("created_at")),
                    _as_float(response.get("delivered_at")),
                )
                if isinstance(response, dict)
                else 0.0
            )
            if (
                retention_timestamp < cutoff_timestamp
                or retention_timestamp > now
            ):
                del response_buckets[response_key]
                changed = True
        if len(response_buckets) > MAX_RESPONSE_BUCKETS:
            evictable = sorted(
                (key for key in response_buckets if key != protect_response_key),
                key=lambda key: _response_retention_key(response_buckets[key]),
            )
            for response_key in evictable[
                : len(response_buckets) - MAX_RESPONSE_BUCKETS
            ]:
                del response_buckets[response_key]
                changed = True
        return changed

    def _stage_unlocked(self, name: str) -> tuple[str, dict[str, Any], int]:
        seq = self._staged_seq.get(name, 0) + 1
        self._staged_seq[name] = seq
        # ``_copy_payload`` replaces a json.dumps/json.loads round trip that ran
        # on the event loop for every recorded decision and soft hint. See its
        # docstring for why the copy has to stay complete rather than sharing
        # the untouched sub-dicts.
        payload = _copy_payload(self._cache[name])
        return name, payload, seq

    def _flush_snapshot(
        self,
        name: str,
        payload: dict[str, Any],
        seq: int,
        *,
        raise_on_error: bool = False,
    ) -> None:
        try:
            from utils.cloudsave_runtime import cloudsave_writable_transaction

            with cloudsave_writable_transaction(
                self._config_manager,
                operation="save",
                target=f"memory/{name}/anti_repeat_effects.json",
            ):
                with self._get_write_lock(name):
                    if seq <= self._written_seq.get(name, 0):
                        return
                    target = self._write_file_path(name)
                    if target is None:
                        # Character directory is gone (deleted or renamed while
                        # this turn was in flight). Fence the sequence so later
                        # snapshots do not retry into a removed identity, and
                        # drop this one rather than recreating the directory.
                        self._written_seq[name] = seq
                        logger.debug(
                            "[AntiRepeatEffects] skip save for removed character %s",
                            name,
                        )
                        return
                    atomic_write_json(
                        target,
                        payload,
                        indent=2,
                        ensure_ascii=False,
                    )
                    self._written_seq[name] = seq
        except Exception as exc:
            logger.warning(
                "[AntiRepeatEffects] save failed for %s: %s",
                name,
                type(exc).__name__,
            )
            if raise_on_error:
                raise

    def _apply_decision(
        self,
        name: str,
        decision: AntiRepeatDecision,
        now: float,
    ) -> tuple[str, dict[str, Any], int]:
        decision.validate()
        name = _resolve_name(name)
        with self._get_lock(name):
            payload = self._load_unlocked(name, now)
            self._prune(payload, now)
            bucket = payload["daily_buckets"].setdefault(_utc_day(now), _empty_bucket())
            _apply_decision_to_bucket(bucket, decision, now)

            if decision.response_id:
                response_key = _response_key(decision.response_id)
                response_buckets = payload.setdefault("response_buckets", {})
                response_bucket = response_buckets.setdefault(
                    response_key,
                    {
                        "created_at": now,
                        "delivered_at": 0.0,
                        "bucket": _empty_bucket(),
                    },
                )
                response_bucket["created_at"] = min(
                    _as_float(response_bucket.get("created_at")) or now,
                    now,
                )
                _apply_decision_to_bucket(response_bucket["bucket"], decision, now)
                self._prune(payload, now, protect_response_key=response_key)

            self._cache[name] = payload
            return self._stage_unlocked(name)

    def stage_decision(
        self,
        name: str,
        decision: AntiRepeatDecision,
        *,
        now: float | None = None,
    ) -> tuple[str, dict[str, Any], int]:
        timestamp = time.time() if now is None else float(now)
        return self._apply_decision(name, decision, timestamp)

    def stage_response_delivered(
        self,
        name: str,
        response_id: str,
        *,
        now: float | None = None,
    ) -> tuple[str, dict[str, Any], int] | None:
        """Mark a response-scoped aggregate as belonging to persisted output."""
        if not isinstance(response_id, str) or not (0 < len(response_id) <= 128):
            return None
        timestamp = time.time() if now is None else float(now)
        name = _resolve_name(name)
        with self._get_lock(name):
            payload = self._load_unlocked(name, timestamp)
            response_key = _response_key(response_id)
            response = payload.get("response_buckets", {}).get(response_key)
            if not isinstance(response, dict):
                return None
            response["delivered_at"] = timestamp
            # ``timestamp`` is the publication instant, which is slightly in the
            # past by the time this runs; without the fence the bucket just marked
            # delivered would look future-dated to _prune and be dropped.
            self._prune(payload, timestamp, protect_response_key=response_key)
            self._cache[name] = payload
            return self._stage_unlocked(name)

    def stage_soft_hint(
        self,
        name: str,
        *,
        now: float | None = None,
    ) -> tuple[str, dict[str, Any], int]:
        timestamp = time.time() if now is None else float(now)
        name = _resolve_name(name)
        with self._get_lock(name):
            payload = self._load_unlocked(name, timestamp)
            self._prune(payload, timestamp)
            bucket = payload["daily_buckets"].setdefault(
                _utc_day(timestamp), _empty_bucket()
            )
            counters = bucket.setdefault("counters", _empty_counters())
            counters["soft_hint_injected"] = (
                int(counters.get("soft_hint_injected", 0)) + 1
            )
            self._cache[name] = payload
            return self._stage_unlocked(name)

    async def aflush_staged(
        self,
        staged: tuple[str, dict[str, Any], int] | None,
    ) -> None:
        if staged is None:
            return
        await asyncio.to_thread(self._flush_snapshot, *staged)

    def flush_staged_detached(
        self,
        staged: tuple[str, dict[str, Any], int] | None,
    ) -> None:
        if staged is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_snapshot(*staged)
            return
        task = loop.create_task(self.aflush_staged(staged))
        self._detached_flushes.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._detached_flushes.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.debug(
                    "[AntiRepeatEffects] detached flush failed: %s",
                    type(finished.exception()).__name__,
                )

        task.add_done_callback(_done)

    def record_decision(
        self,
        name: str,
        decision: AntiRepeatDecision,
        *,
        now: float | None = None,
    ) -> None:
        self._flush_snapshot(*self.stage_decision(name, decision, now=now))

    def record_soft_hint(self, name: str, *, now: float | None = None) -> None:
        self._flush_snapshot(*self.stage_soft_hint(name, now=now))

    def query_effects(
        self,
        name: str,
        days: int,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        if days not in {7, 30, 90}:
            raise ValueError("effect days must be one of 7, 30, or 90")
        timestamp = time.time() if now is None else float(now)
        name = _resolve_name(name)
        with self._get_lock(name):
            payload = self._load_unlocked(name, timestamp)
            staged = self._stage_unlocked(name) if self._prune(payload, timestamp) else None
            cutoff = (
                datetime.fromtimestamp(timestamp, timezone.utc).date()
                - timedelta(days=days - 1)
            ).isoformat()
            # Copy under the lock. Summarizing happens after the lock is
            # released, and a concurrent decision mutates these very dicts —
            # adding pattern keys while _summarize_effect_buckets iterates
            # ``.values()`` is a "dictionary changed size during iteration"
            # away. Same reason ``_copy_payload`` refuses to share sub-dicts.
            buckets = [
                _copy_bucket(bucket)
                for day, bucket in payload.get("daily_buckets", {}).items()
                if cutoff <= day <= _utc_day(timestamp) and isinstance(bucket, dict)
            ]
            started_at = float(payload.get("started_at", timestamp))

        if staged is not None:
            self._flush_snapshot(*staged)

        result = {
            "schema_version": SCHEMA_VERSION,
            # Availability means "there are aggregates IN THIS WINDOW", not "a
            # sidecar file exists". ``clear_effects`` leaves an empty payload on
            # disk, so file existence reported available right after the user
            # cleared the statistics and the panel skipped its "no records for
            # this period" state to render a row of zeros. A nonempty sidecar
            # whose buckets all fall outside the 7/30/90-day window had the same
            # problem.
            "source_available": bool(buckets),
            "started_at": started_at,
            "period_days": days,
        }
        result.update(_summarize_effect_buckets(buckets))
        return result

    def query_effects_for_responses(
        self,
        name: str,
        response_ids: Iterable[str],
        assistant_message_limit: int,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Aggregate delivered effects linked to a bounded assistant history slice."""
        if assistant_message_limit < 1:
            raise ValueError("assistant message limit must be greater than zero")
        timestamp = time.time() if now is None else float(now)
        name = _resolve_name(name)
        keys = {
            _response_key(response_id)
            for response_id in response_ids
            if isinstance(response_id, str) and 0 < len(response_id) <= 128
        }
        with self._get_lock(name):
            payload = self._load_unlocked(name, timestamp)
            staged = self._stage_unlocked(name) if self._prune(payload, timestamp) else None
            response_buckets = payload.get("response_buckets", {})
            linked = [
                response_buckets[key]
                for key in keys
                if isinstance(response_buckets.get(key), dict)
                and _as_float(response_buckets[key].get("delivered_at")) > 0
            ]
            source_available = bool(linked)
            # Copied under the lock for the same reason as ``query_effects``.
            buckets = [
                _copy_bucket(response["bucket"])
                for response in linked
                if isinstance(response.get("bucket"), dict)
            ]
            started_at = float(payload.get("started_at", timestamp))

        if staged is not None:
            self._flush_snapshot(*staged)

        result = {
            "schema_version": SCHEMA_VERSION,
            "source_available": source_available,
            "started_at": started_at,
            "scope_type": "assistant_messages",
            "assistant_message_limit": assistant_message_limit,
            "linked_message_count": len(linked),
        }
        result.update(_summarize_effect_buckets(buckets))
        return result

    def clear_effects(self, name: str) -> None:
        """Clear one identity's aggregates, publishing the cut only once durable.

        The flush must NOT run while this holds the character lock. It enters
        ``cloudsave_writable_transaction``, and a cloud import takes those two
        in the opposite order — it holds the cloud-apply fence for its whole
        duration and reaches for the character lock inside it, to evict caches.
        Holding character-lock-then-fence here is the other leg of an ABBA
        deadlock that would hang both the reset and the import.

        So the flush races concurrent writers, and the ordering rule is: the
        cache and the file must always agree on which side of the cut they are
        on, and a reset that reports failure must lose nothing.

        That is why the empty payload is NOT published before the flush.
        Publishing it early made ``_apply_decision`` load the empty payload,
        mutate it in place and stage a newer snapshot built on the cut — so a
        racing writer made the reset durable even when the reset itself failed,
        destroying the pre-reset aggregates and ``started_at`` while the
        endpoint reported failure. No rollback in an ``except`` block can undo
        that: by then the racer has already written the cleared payload out.

        Keeping the pre-reset payload in the cache inverts it. A concurrent
        decision builds on the OLD data, so a failed flush leaves cache and
        file exactly as they were and there is nothing to roll back. If such a
        decision staged after us, its write lands after ours and the cut is not
        durable, so cut again. On success the racing decision is dropped: the
        user asked for this data to be cleared milliseconds earlier, and
        recording one is best-effort by contract.
        """
        name = _resolve_name(name)
        for _ in range(_CLEAR_ATTEMPTS):
            timestamp = time.time()
            with self._get_lock(name):
                cleared = _default_payload(timestamp)
                seq = self._staged_seq.get(name, 0) + 1
                self._staged_seq[name] = seq
                generation = self._evict_generation.get(name, 0)
                staged = (name, _copy_payload(cleared), seq)
            self._flush_snapshot(*staged, raise_on_error=True)
            with self._get_lock(name):
                if self._evict_generation.get(name, 0) != generation:
                    # A cloud import (or a delete) replaced this identity while
                    # the flush was in flight. Eviction fences the write
                    # sequence, so the flush above was silently skipped and the
                    # file on disk is the imported one. Publishing the cut here
                    # would put an empty payload into the cache the import just
                    # dropped, and the next decision would flush it over the
                    # imported file. Re-cutting would be worse: it would clear
                    # data the reset never asked about.
                    raise RuntimeError(
                        "anti-repeat reset abandoned: the identity was replaced"
                    )
                if self._staged_seq.get(name, 0) == seq:
                    self._cache[name] = cleared
                    return
        # Every attempt already wrote the cut out before losing the race. The
        # racer that outran us normally restores the file, but if its own
        # flush then fails or is cancelled the cut stays on disk while this
        # reports failure -- the data would come back erased after a restart.
        # Push the live cache out so the file matches memory before failing.
        with self._get_lock(name):
            restore = self._stage_unlocked(name) if name in self._cache else None
        if restore is not None:
            # raise_on_error, because swallowing here recreates the very loss
            # this restore exists to prevent, one level down: the last cut
            # would stay on the file while the endpoint reports failure, and
            # the aggregates would come back erased after a restart. A failed
            # restore is the more severe condition, so it surfaces instead of
            # the race message.
            self._flush_snapshot(*restore, raise_on_error=True)
        raise RuntimeError("anti-repeat reset lost the race to concurrent writes")

    def _evict_unlocked(self, name: str) -> None:
        self._evict_generation[name] = self._evict_generation.get(name, 0) + 1
        fence = max(
            self._staged_seq.get(name, 0),
            self._written_seq.get(name, 0),
        )
        self._cache.pop(name, None)
        self._staged_seq[name] = fence
        self._written_seq[name] = fence

    def evict_character(self, name: str) -> None:
        """Forget a LIVE identity whose file changed underneath us.

        Same contract as the sibling stores: a cloud-save import replaces
        ``memory/<name>/`` wholesale, and the cache would otherwise shadow the
        new contents and get flushed back over them. The sequence fence stops a
        snapshot staged before the replacement from doing that.

        This is also the explicit "the identity is live" event that lifts
        retirement -- an imported or renamed-to name is a real character, and
        leaving it retired would deny it the lazy directory creation every
        sibling memory writer gets. Directory existence never lifts retirement;
        only this call does.
        """
        name = _resolve_name(name)
        with self._get_lock(name):
            with self._get_write_lock(name):
                self._evict_unlocked(name)
                self._retired.discard(name)

    def revive_character(self, name: str) -> None:
        """Mark a name live again WITHOUT dropping its cache or fencing it.

        The cloud APPLY never rewrites this sidecar -- it is not in
        ``MANAGED_MEMORY_FILENAMES`` -- so the cache still matches the file and
        evicting would only raise the sequence fence, silently discarding a
        snapshot that was staged and not yet flushed. What such an import DOES
        need is the retirement lifted: a name reused after an earlier delete
        cannot create its directory until something says it is live again.
        """
        name = _resolve_name(name)
        with self._get_lock(name):
            with self._get_write_lock(name):
                self._retired.discard(name)

    def retire_character(self, name: str) -> None:
        """Forget one identity whose directory is being REMOVED, and fence it.

        The sequence fence only covers snapshots staged BEFORE this call.
        Retirement is what stops a decision recorded while the delete or
        rename-away is still in flight from recreating the directory.
        """
        name = _resolve_name(name)
        with self._get_lock(name):
            with self._get_write_lock(name):
                self._evict_unlocked(name)
                self._retired.add(name)


_GLOBAL_STORE: Optional[AntiRepeatEffectStore] = None
_GLOBAL_LOCK = threading.Lock()


def get_anti_repeat_effect_store() -> AntiRepeatEffectStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        with _GLOBAL_LOCK:
            if _GLOBAL_STORE is None:
                _GLOBAL_STORE = AntiRepeatEffectStore()
    return _GLOBAL_STORE


def evict_cached_anti_repeat_effects(*character_names: str) -> None:
    """Evict loaded identities without creating the global store."""
    store = _GLOBAL_STORE
    if store is None:
        return
    for character_name in dict.fromkeys(character_names):
        store.evict_character(character_name)

def revive_cached_anti_repeat_effects(*character_names: str) -> None:
    """Lift retirement for live identities without touching their caches."""
    store = _GLOBAL_STORE
    if store is None:
        return
    for character_name in dict.fromkeys(character_names):
        store.revive_character(character_name)


def retire_cached_anti_repeat_effects(*character_names: str) -> None:
    """Retire identities whose directories are being removed."""
    store = _GLOBAL_STORE
    if store is None:
        return
    for character_name in dict.fromkeys(character_names):
        store.retire_character(character_name)


def record_anti_repeat_decision(
    character_name: str,
    decision: AntiRepeatDecision,
) -> None:
    """Best-effort runtime helper that never changes delivery behavior."""
    try:
        store = get_anti_repeat_effect_store()
        store.flush_staged_detached(store.stage_decision(character_name, decision))
    except Exception as exc:
        logger.debug(
            "[AntiRepeatEffects] decision record skipped: %s",
            type(exc).__name__,
        )


def mark_anti_repeat_response_delivered(
    character_name: str,
    response_id: str,
    *,
    now: float | None = None,
) -> None:
    """Best-effort link from one delivered reply to its aggregate decisions."""
    try:
        store = get_anti_repeat_effect_store()
        staged = store.stage_response_delivered(
            character_name,
            response_id,
            now=now,
        )
        store.flush_staged_detached(staged)
    except Exception as exc:
        logger.debug(
            "[AntiRepeatEffects] delivered response link skipped: %s",
            type(exc).__name__,
        )


def record_anti_repeat_soft_hint(character_name: str) -> None:
    """Best-effort soft-hint counter without detector text."""
    try:
        store = get_anti_repeat_effect_store()
        store.flush_staged_detached(store.stage_soft_hint(character_name))
    except Exception as exc:
        logger.debug(
            "[AntiRepeatEffects] soft-hint record skipped: %s",
            type(exc).__name__,
        )
