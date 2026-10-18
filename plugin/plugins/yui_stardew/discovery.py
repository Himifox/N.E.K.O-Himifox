from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil

from .bridge import (
    GATEWAY_PATH,
    PROTOCOL_VERSION,
    BridgeError,
    BridgeSettings,
    YuiStardewBridgeClient,
)


DISCOVERY_VERSION = 2
MAXIMUM_DESCRIPTOR_BYTES = 4 * 1024
MAXIMUM_CONFIG_BYTES = 64 * 1024
MAXIMUM_DESCRIPTORS = 32
MAXIMUM_PROBES = 4


@dataclass(frozen=True, slots=True)
class BridgeCandidate:
    settings: BridgeSettings
    source: str
    instance_id: str
    config_path: Path | None = None
    created_at_utc: str = ""


@dataclass(frozen=True, slots=True)
class VerifiedCandidate:
    candidate: BridgeCandidate
    response: dict[str, Any]

    @property
    def world_ready(self) -> bool:
        state = self.response.get("state")
        return isinstance(state, Mapping) and state.get("world_ready") is True

    @property
    def host_authoritative(self) -> bool:
        state = self.response.get("state")
        return isinstance(state, Mapping) and state.get("host_authoritative") is True

    def ui_summary(self) -> dict[str, Any]:
        return {
            "instance_id": self.candidate.instance_id,
            "label": f"YuiToIssho · WebSocket · {self.candidate.instance_id[:8]}",
            "transport": "websocket",
            "source": self.candidate.source,
            "world_ready": self.world_ready,
            "host_authoritative": self.host_authoritative,
        }


@dataclass(frozen=True, slots=True)
class BridgeDiscoveryResult:
    state: str
    code: str
    message: str
    selected: VerifiedCandidate | None = None
    verified: tuple[VerifiedCandidate, ...] = ()

    def ui_state(self, runtime_config: Mapping[str, Any]) -> dict[str, Any]:
        selected_candidate = (
            self.selected.candidate if self.selected is not None else None
        )
        return {
            "state": self.state,
            "code": self.code,
            "message": self.message,
            "source": selected_candidate.source
            if selected_candidate is not None
            else "unconfigured",
            "transport": "websocket" if selected_candidate is not None else "",
            "instance_id": selected_candidate.instance_id
            if selected_candidate is not None
            else "",
            "protocol_version": PROTOCOL_VERSION,
            "verified_at": (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
                if self.selected is not None
                else ""
            ),
            "candidates": [candidate.ui_summary() for candidate in self.verified],
            "compatibility_override": compatibility_override_active(runtime_config),
            "open_config_directory": (
                str(selected_candidate.config_path.parent)
                if selected_candidate is not None
                and selected_candidate.config_path is not None
                else ""
            ),
        }


async def discover_bridge_connection(
    value: Mapping[str, Any],
    preferred_instance_id: str = "",
) -> BridgeDiscoveryResult:
    base_settings = BridgeSettings.from_mapping(value)
    explicit = _explicit_candidate(value, base_settings)
    invalid_descriptors = 0
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates, invalid_descriptors = _descriptor_candidates(base_settings)

    if not candidates:
        if invalid_descriptors:
            return BridgeDiscoveryResult(
                "invalid",
                "DISCOVERY-DESCRIPTOR-INVALID",
                "发现信息无效；请同时升级并重启 Stardew Valley 与 YuiToIssho。",
            )
        return BridgeDiscoveryResult(
            "not_found",
            "DISCOVERY-NOT-FOUND",
            "等待 YuiToIssho Agent Gateway 启动；无需设置游戏路径或端口。",
        )

    verified, failures = await _probe_candidates(candidates)
    if not verified:
        if any(code in {"AUTH-FAILED", "BRIDGE-AUTH-FAILED"} for code in failures):
            return BridgeDiscoveryResult(
                "auth_failed",
                "BRIDGE-AUTH-FAILED",
                "Gateway 密钥已经变化；请清除手动覆盖后重新发现。",
            )
        return BridgeDiscoveryResult(
            "offline",
            "DISCOVERY-CANDIDATES-OFFLINE",
            "已找到 YuiToIssho，但 Agent Gateway 尚未响应；请等待游戏完成启动后刷新。",
        )

    selected = next(
        (
            candidate
            for candidate in verified
            if preferred_instance_id
            and candidate.candidate.instance_id == preferred_instance_id
        ),
        None,
    )
    if selected is None and len(verified) == 1:
        selected = verified[0]
    if selected is None:
        host_ready = [
            candidate
            for candidate in verified
            if candidate.world_ready and candidate.host_authoritative
        ]
        if len(host_ready) == 1:
            selected = host_ready[0]
    if selected is None:
        return BridgeDiscoveryResult(
            "ambiguous",
            "BRIDGE-AMBIGUOUS",
            "发现多个可用的 YuiToIssho 实例，请选择当前游戏实例。",
            verified=tuple(verified),
        )
    return BridgeDiscoveryResult(
        "ready",
        "STATUS",
        "YuiToIssho Agent Gateway 已通过 WebSocket 连接。",
        selected=selected,
        verified=tuple(verified),
    )


def compatibility_override_active(value: Mapping[str, Any]) -> bool:
    return any(
        isinstance(value.get(key), str) and bool(value[key].strip())
        for key in ("endpoint", "token", "himi_yui_config_path")
    )


def _explicit_candidate(
    value: Mapping[str, Any],
    base_settings: BridgeSettings,
) -> BridgeCandidate | None:
    legacy_path = value.get("himi_yui_config_path")
    if isinstance(legacy_path, str) and legacy_path.strip():
        raise BridgeError(
            "BRIDGE-CONFIG-LEGACY",
            "旧版配置路径无法定位动态 WebSocket；请清除兼容覆盖并使用自动发现。",
        )

    endpoint_value = value.get("endpoint")
    token_value = value.get("token")
    endpoint = endpoint_value.strip() if isinstance(endpoint_value, str) else ""
    token = token_value if isinstance(token_value, str) else ""
    if not endpoint and not token:
        return None
    if not _valid_gateway_endpoint(endpoint) or not 16 <= len(token) <= 256:
        raise BridgeError(
            "BRIDGE-CONFIG-INVALID",
            "手动覆盖必须同时提供有效的本机 WebSocket 端点与 16～256 字符密钥。",
        )
    return BridgeCandidate(
        BridgeSettings(
            endpoint=endpoint,
            token=token,
            connect_timeout_seconds=base_settings.connect_timeout_seconds,
            request_timeout_seconds=base_settings.request_timeout_seconds,
        ),
        "manual_credentials",
        "manual-websocket",
    )


def _descriptor_candidates(
    base_settings: BridgeSettings,
) -> tuple[list[BridgeCandidate], int]:
    candidates: list[BridgeCandidate] = []
    invalid = 0
    descriptor_paths: list[Path] = []
    for root in _discovery_roots():
        try:
            if not root.is_dir() or root.is_symlink():
                continue
            descriptor_paths.extend(sorted(root.glob("*.json")))
        except OSError:
            invalid += 1
    for path in descriptor_paths[:MAXIMUM_DESCRIPTORS]:
        candidate, descriptor_invalid = _read_descriptor_result(path, base_settings)
        if descriptor_invalid:
            invalid += 1
        elif candidate is not None:
            candidates.append(candidate)
    return _deduplicate_candidates(candidates), invalid


def _read_descriptor(
    path: Path,
    base_settings: BridgeSettings,
) -> BridgeCandidate | None:
    candidate, _invalid = _read_descriptor_result(path, base_settings)
    return candidate


def _read_descriptor_result(
    path: Path,
    base_settings: BridgeSettings,
) -> tuple[BridgeCandidate | None, bool]:
    """Return a candidate and whether a descriptor is invalid.

    A valid v2 identity whose process has exited is ordinary stale discovery
    state, not a broken installation. Ignore it before reading its endpoint or
    configuration; keep malformed or live-but-invalid descriptors diagnostic.
    """
    match = re.fullmatch(r"([0-9a-f]{32})\.json", path.name)
    if match is None or path.is_symlink():
        return None, True
    try:
        if not path.is_file() or path.stat().st_size > MAXIMUM_DESCRIPTOR_BYTES:
            return None, True
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(payload, dict):
        return None, True

    instance_id = payload.get("instance_id")
    process_id = payload.get("process_id")
    created_at_utc = payload.get("created_at_utc")
    endpoint = payload.get("endpoint")
    config_path = payload.get("config_path")
    if (
        payload.get("discovery_version") != DISCOVERY_VERSION
        or payload.get("protocol_version") != PROTOCOL_VERSION
        or payload.get("transport") != "websocket"
        or instance_id != match.group(1)
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        return None, True
    if not _process_exists(process_id):
        return None, False
    if (
        not isinstance(endpoint, str)
        or not _valid_gateway_endpoint(endpoint)
        or not isinstance(config_path, str)
        or not config_path
        or len(config_path) > 4096
        or not _valid_config_path(Path(config_path))
        or not _valid_utc_timestamp(created_at_utc)
    ):
        return None, True

    token = _read_yuitoissho_token(Path(config_path))
    if token is None:
        return None, True
    return BridgeCandidate(
        BridgeSettings(
            endpoint=endpoint,
            token=token,
            connect_timeout_seconds=base_settings.connect_timeout_seconds,
            request_timeout_seconds=base_settings.request_timeout_seconds,
        ),
        "discovery_v2",
        instance_id,
        Path(os.path.abspath(config_path)),
        created_at_utc,
    ), False


def _valid_gateway_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "ws"
        and parsed.hostname == "127.0.0.1"
        and parsed.username is None
        and parsed.password is None
        and isinstance(port, int)
        and 1024 <= port <= 65535
        and parsed.netloc == f"127.0.0.1:{port}"
        and parsed.path == GATEWAY_PATH
        and not parsed.query
        and not parsed.fragment
    )


def _valid_config_path(path: Path) -> bool:
    return (
        path.is_absolute()
        and path.name.casefold() == "config.json"
        and path.parent.name.casefold() == "yuitoissho"
        and not path.is_symlink()
    )


def _valid_utc_timestamp(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not value.endswith("Z")
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _process_exists(process_id: int) -> bool:
    return psutil.pid_exists(process_id)


def _discovery_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        _append_absolute_root(
            roots,
            os.environ.get("LOCALAPPDATA"),
            "YuiToIssho",
            "agent-gateway-discovery",
            "v2",
        )
        return roots
    if sys.platform == "darwin":
        _append_absolute_root(
            roots,
            os.environ.get("HOME"),
            "Library",
            "Application Support",
            "YuiToIssho",
            "agent-gateway-discovery",
            "v2",
        )
        return roots
    _append_absolute_root(
        roots,
        os.environ.get("XDG_RUNTIME_DIR"),
        "yuitoissho",
        "agent-gateway-discovery",
        "v2",
    )
    _append_absolute_root(
        roots,
        os.environ.get("XDG_STATE_HOME"),
        "yuitoissho",
        "agent-gateway-discovery",
        "v2",
    )
    _append_absolute_root(
        roots,
        os.environ.get("HOME"),
        ".local",
        "state",
        "yuitoissho",
        "agent-gateway-discovery",
        "v2",
    )
    return roots


def _append_absolute_root(roots: list[Path], root: str | None, *segments: str) -> None:
    if not root:
        return
    root_path = Path(root)
    if not root_path.is_absolute():
        return
    candidate = root_path.joinpath(*segments)
    key = str(candidate).casefold()
    if all(str(existing).casefold() != key for existing in roots):
        roots.append(candidate)


def _deduplicate_candidates(candidates: list[BridgeCandidate]) -> list[BridgeCandidate]:
    ordered = sorted(
        candidates, key=lambda candidate: candidate.created_at_utc, reverse=True
    )
    result: list[BridgeCandidate] = []
    seen: set[str] = set()
    for candidate in ordered:
        key = candidate.settings.endpoint.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


async def _probe_candidates(
    candidates: list[BridgeCandidate],
) -> tuple[list[VerifiedCandidate], list[str]]:
    semaphore = asyncio.Semaphore(MAXIMUM_PROBES)

    async def probe(candidate: BridgeCandidate) -> tuple[VerifiedCandidate | None, str]:
        async with semaphore:
            try:
                response = await YuiStardewBridgeClient(candidate.settings).request(
                    "status"
                )
            except BridgeError as error:
                return None, error.code
            if response.get("ok") is not True:
                return None, str(response.get("code") or "BRIDGE-STATUS-FAILED")
            return VerifiedCandidate(candidate, response), ""

    results = await asyncio.gather(*(probe(candidate) for candidate in candidates))
    verified = [candidate for candidate, _code in results if candidate is not None]
    failures = [code for candidate, code in results if candidate is None]
    return verified, failures


def _read_yuitoissho_token(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAXIMUM_CONFIG_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("EnableNekoBridge") is not True:
        return None
    token = payload.get("NekoBridgeToken")
    if not isinstance(token, str) or not 16 <= len(token) <= 256:
        return None
    return token
