"""Stable hand-off contract for future knowledge-package providers."""

from __future__ import annotations

import re
import json
from dataclasses import asdict, dataclass


SUBSCRIPTION_PROTOCOL_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class KnowledgeSubscription:
    provider: str
    remote_id: str
    version: str
    channel: str
    artifact_sha256: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def validate_subscription(payload: object) -> KnowledgeSubscription:
    if not isinstance(payload, dict):
        raise ValueError("subscription metadata must be an object")
    allowed = {"provider", "remote_id", "version", "channel", "artifact_sha256"}
    if set(payload) - allowed:
        raise ValueError("subscription metadata contains unsupported fields")
    provider = _required_text(payload.get("provider"), "provider", 64)
    remote_id = _required_text(payload.get("remote_id"), "remote_id", 200)
    version = _required_text(payload.get("version"), "version", 100)
    channel = _required_text(payload.get("channel"), "channel", 40)
    digest = _required_text(payload.get("artifact_sha256"), "artifact_sha256", 64).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("artifact_sha256 must be a SHA-256 digest")
    return KnowledgeSubscription(provider, remote_id, version, channel, digest)


def canonical_pack_bytes(payload: object) -> bytes:
    """Canonical JSON bytes hashed by both provider and local hand-off."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_canonical_pack_artifact(raw: bytes) -> object:
    """Decode a market artifact and require its bytes to be canonical JSON."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge artifact is not valid UTF-8 JSON") from exc
    if raw != canonical_pack_bytes(payload):
        raise ValueError("knowledge artifact is not canonical JSON")
    return payload


def _required_text(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_chars:
        raise ValueError(f"subscription {field} is invalid")
    return value.strip()
