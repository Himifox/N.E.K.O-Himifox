"""Bounded feedback state used by optional recommendation personalization."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from main_logic.proactive_recommendation.storage.atomic_json import locked_path


LEGACY_FEEDBACK_STATE_PREVIEW_FILENAME = (
    "proactive_recommendation_feedback_state_preview.json"
)
FEEDBACK_STATE_PREVIEW_FILENAME = (
    "proactive_recommendation_feedback_state_preview_v2.json"
)
FEEDBACK_STATE_PREVIEW_VERSION = "feedback_state_preview_v2"
TEMPORARY_INTEREST_TTL_SECONDS = 2 * 60 * 60
PERSISTENT_INTEREST_MIN_EVIDENCE = 3
PERSISTENT_AFFINITY_MAX = 0.20

_CONVERSATION_SCOPE = "conversation_acceptance"
_SOURCE_SCOPE = "source_affinity"
_temporary_state: dict[tuple[str, str, str], dict[str, Any]] = {}
_state_lock = threading.RLock()


def clear_temporary_feedback_state_preview() -> None:
    """Clear process-local state without touching persistent aggregates."""
    with _state_lock:
        _temporary_state.clear()


def update_conversation_acceptance_preview(
    *,
    config_dir: str | os.PathLike[str] | None,
    score: float,
    persistent_eligible: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply one generic conversation signal to the global preview."""
    return _update_feedback_state_preview(
        config_dir=config_dir,
        scope=_CONVERSATION_SCOPE,
        subject="",
        score=score,
        persistent_eligible=persistent_eligible,
        now=now,
    )


def update_source_affinity_preview(
    *,
    config_dir: str | os.PathLike[str] | None,
    source_type: Any,
    score: float,
    persistent_eligible: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply one verified material signal to a source-level preview."""
    source = _source_name(source_type)
    if not source:
        return get_feedback_state_preview(config_dir=config_dir, now=now)
    return _update_feedback_state_preview(
        config_dir=config_dir,
        scope=_SOURCE_SCOPE,
        subject=source,
        score=score,
        persistent_eligible=persistent_eligible,
        now=now,
    )


def _update_feedback_state_preview(
    *,
    config_dir: str | os.PathLike[str] | None,
    scope: str,
    subject: str,
    score: float,
    persistent_eligible: bool,
    now: float | None,
) -> dict[str, Any]:
    root = _config_root(config_dir)
    current = time.time() if now is None else float(now)
    signal = _clamp(float(score), -1.0, 1.0)
    if root is None or signal == 0:
        return get_feedback_state_preview(config_dir=config_dir, now=current)

    with _state_lock, locked_path(root / FEEDBACK_STATE_PREVIEW_FILENAME):
        key = (str(root), scope, subject)
        previous = _temporary_state.get(key)
        if previous is None or float(previous.get("expires_at", 0.0)) <= current:
            previous = _empty_evidence_bucket()
        temporary = {
            "interest_preview": round(
                _clamp(float(previous["interest_preview"]) + signal, -1.0, 1.0),
                3,
            ),
            "positive_evidence_count": int(previous["positive_evidence_count"])
            + int(signal > 0),
            "negative_evidence_count": int(previous["negative_evidence_count"])
            + int(signal < 0),
            "updated_at": current,
            "expires_at": current + TEMPORARY_INTEREST_TTL_SECONDS,
        }
        _temporary_state[key] = temporary

        if persistent_eligible:
            state = _load_persistent_state(root)
            bucket = _persistent_bucket(state, scope, subject, current)
            count_key = (
                "positive_evidence_count" if signal > 0 else "negative_evidence_count"
            )
            bucket[count_key] = int(bucket[count_key]) + 1
            bucket["updated_at"] = current
            _save_persistent_state(root, state)

        return _snapshot(root, current)


def get_feedback_state_preview(
    *,
    config_dir: str | os.PathLike[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a point-in-time snapshot; tuning never consumes it."""
    root = _config_root(config_dir)
    current = time.time() if now is None else float(now)
    if root is None:
        return _empty_snapshot()
    with _state_lock, locked_path(root / FEEDBACK_STATE_PREVIEW_FILENAME):
        return _snapshot(root, current)


def _snapshot(root: Path, now: float) -> dict[str, Any]:
    root_key = str(root)
    expired = [
        key
        for key, value in _temporary_state.items()
        if key[0] == root_key and float(value.get("expires_at", 0.0)) <= now
    ]
    for key in expired:
        _temporary_state.pop(key, None)

    conversation_temporary = _temporary_state.get(
        (root_key, _CONVERSATION_SCOPE, ""),
        _empty_evidence_bucket(),
    )
    source_temporary = {
        subject: _temporary_snapshot(bucket, now)
        for (config_key, scope, subject), bucket in sorted(_temporary_state.items())
        if config_key == root_key and scope == _SOURCE_SCOPE
    }
    persistent = _load_persistent_state(root)
    conversation_persistent = persistent["conversation_acceptance"]
    source_persistent = {
        source: {
            **bucket,
            "affinity_preview": _persistent_score(bucket),
        }
        for source, bucket in sorted(persistent["source_affinity"].items())
    }
    return {
        "version": FEEDBACK_STATE_PREVIEW_VERSION,
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "conversation_acceptance": {
            "temporary": {
                "ttl_seconds": TEMPORARY_INTEREST_TTL_SECONDS,
                **_temporary_snapshot(conversation_temporary, now),
            },
            "persistent": {
                "min_explicit_evidence": PERSISTENT_INTEREST_MIN_EVIDENCE,
                **conversation_persistent,
                "acceptance_preview": _persistent_score(conversation_persistent),
            },
        },
        "source_affinity": {
            "temporary": {
                "ttl_seconds": TEMPORARY_INTEREST_TTL_SECONDS,
                "sources": source_temporary,
            },
            "persistent": {
                "min_explicit_evidence": PERSISTENT_INTEREST_MIN_EVIDENCE,
                "sources": source_persistent,
            },
        },
    }


def _empty_snapshot() -> dict[str, Any]:
    empty = _empty_evidence_bucket()
    return {
        "version": FEEDBACK_STATE_PREVIEW_VERSION,
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "conversation_acceptance": {
            "temporary": {
                "ttl_seconds": TEMPORARY_INTEREST_TTL_SECONDS,
                **_temporary_snapshot(empty, 0.0),
            },
            "persistent": {
                "min_explicit_evidence": PERSISTENT_INTEREST_MIN_EVIDENCE,
                "positive_evidence_count": 0,
                "negative_evidence_count": 0,
                "updated_at": 0.0,
                "acceptance_preview": 0.0,
            },
        },
        "source_affinity": {
            "temporary": {
                "ttl_seconds": TEMPORARY_INTEREST_TTL_SECONDS,
                "sources": {},
            },
            "persistent": {
                "min_explicit_evidence": PERSISTENT_INTEREST_MIN_EVIDENCE,
                "sources": {},
            },
        },
    }


def _load_persistent_state(root: Path) -> dict[str, Any]:
    path = root / FEEDBACK_STATE_PREVIEW_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 2:
        raw = {}
    conversation = _sanitize_persistent_bucket(raw.get("conversation_acceptance"))
    raw_sources = raw.get("source_affinity")
    sources: dict[str, dict[str, Any]] = {}
    if isinstance(raw_sources, Mapping):
        for raw_source, raw_bucket in raw_sources.items():
            source = _source_name(raw_source)
            if source and isinstance(raw_bucket, Mapping):
                sources[source] = _sanitize_persistent_bucket(raw_bucket)
    return {
        "schema_version": 2,
        "conversation_acceptance": conversation,
        "source_affinity": sources,
    }


def _save_persistent_state(root: Path, state: Mapping[str, Any]) -> None:
    path = root / FEEDBACK_STATE_PREVIEW_FILENAME
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        root.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _persistent_bucket(
    state: dict[str, Any],
    scope: str,
    subject: str,
    now: float,
) -> dict[str, Any]:
    if scope == _CONVERSATION_SCOPE:
        return state["conversation_acceptance"]
    return state["source_affinity"].setdefault(
        subject,
        {
            "positive_evidence_count": 0,
            "negative_evidence_count": 0,
            "updated_at": now,
        },
    )


def _temporary_snapshot(bucket: Mapping[str, Any], now: float) -> dict[str, Any]:
    return {
        "interest_preview": _number(bucket.get("interest_preview"), 0.0),
        "positive_evidence_count": _count(bucket.get("positive_evidence_count")),
        "negative_evidence_count": _count(bucket.get("negative_evidence_count")),
        "expires_in_seconds": round(
            max(0.0, _number(bucket.get("expires_at"), 0.0) - now),
            3,
        ),
    }


def _sanitize_persistent_bucket(value: Any) -> dict[str, Any]:
    bucket = value if isinstance(value, Mapping) else {}
    return {
        "positive_evidence_count": _count(bucket.get("positive_evidence_count")),
        "negative_evidence_count": _count(bucket.get("negative_evidence_count")),
        "updated_at": max(0.0, _number(bucket.get("updated_at"), 0.0)),
    }


def _empty_evidence_bucket() -> dict[str, Any]:
    return {
        "interest_preview": 0.0,
        "positive_evidence_count": 0,
        "negative_evidence_count": 0,
        "updated_at": 0.0,
        "expires_at": 0.0,
    }


def _persistent_score(bucket: Mapping[str, Any]) -> float:
    positive = _count(bucket.get("positive_evidence_count"))
    negative = _count(bucket.get("negative_evidence_count"))
    total = positive + negative
    if total < PERSISTENT_INTEREST_MIN_EVIDENCE:
        return 0.0
    return round(
        _clamp(
            PERSISTENT_AFFINITY_MAX * (positive - negative) / total,
            -PERSISTENT_AFFINITY_MAX,
            PERSISTENT_AFFINITY_MAX,
        ),
        3,
    )


def _config_root(config_dir: str | os.PathLike[str] | None) -> Path | None:
    return Path(config_dir).resolve() if config_dir is not None else None


def _source_name(value: Any) -> str:
    source = str(value or "").strip().lower()
    return source if source.replace("_", "").isalnum() else ""


def _count(value: Any) -> int:
    try:
        return max(0, min(1_000_000, int(value)))
    except (TypeError, ValueError):
        return 0


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
