from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge import (
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    BridgeError,
    BridgeSettings,
    YuiStardewBridgeClient,
)


DISCOVERY_VERSION = 1
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
    started_at_utc: str = ""


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
            "label": f"HimiYui · 端口 {self.candidate.settings.port} · {self.candidate.instance_id[:8]}",
            "port": self.candidate.settings.port,
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
        selected_candidate = self.selected.candidate if self.selected is not None else None
        return {
            "state": self.state,
            "code": self.code,
            "message": self.message,
            "source": selected_candidate.source if selected_candidate is not None else "unconfigured",
            "port": selected_candidate.settings.port if selected_candidate is not None else None,
            "instance_id": selected_candidate.instance_id if selected_candidate is not None else "",
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
                if selected_candidate is not None and selected_candidate.config_path is not None
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
        descriptor_candidates, invalid_descriptors = _descriptor_candidates(base_settings)
        candidates = _deduplicate_candidates(
            descriptor_candidates + _legacy_candidates(base_settings)
        )

    if not candidates:
        if invalid_descriptors:
            return BridgeDiscoveryResult(
                "invalid",
                "DISCOVERY-DESCRIPTOR-INVALID",
                "发现信息无效；请重启 Stardew Valley 与 HimiYui。",
            )
        return BridgeDiscoveryResult(
            "not_found",
            "DISCOVERY-NOT-FOUND",
            "等待 HimiYui 启动；无需设置游戏路径。",
        )

    verified, failures = await _probe_candidates(candidates)
    if not verified:
        if any(code in {"AUTH-FAILED", "BRIDGE-AUTH-FAILED"} for code in failures):
            return BridgeDiscoveryResult(
                "auth_failed",
                "BRIDGE-AUTH-FAILED",
                "桥配置已经变化；请清除旧版兼容覆盖后重新发现。",
            )
        return BridgeDiscoveryResult(
            "offline",
            "DISCOVERY-CANDIDATES-OFFLINE",
            "已找到 HimiYui，但本机桥尚未响应；请等待游戏完成加载后刷新。",
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
            "发现多个可用的 HimiYui 实例，请选择当前游戏实例。",
            verified=tuple(verified),
        )
    return BridgeDiscoveryResult(
        "ready",
        "STATUS",
        "HimiYui 本机桥已连接。",
        selected=selected,
        verified=tuple(verified),
    )


def compatibility_override_active(value: Mapping[str, Any]) -> bool:
    token = value.get("token")
    path = value.get("himi_yui_config_path")
    has_token = isinstance(token, str) and bool(token.strip())
    has_path = isinstance(path, str) and bool(path.strip())
    return has_token or has_path


def _explicit_candidate(
    value: Mapping[str, Any],
    base_settings: BridgeSettings,
) -> BridgeCandidate | None:
    token_value = value.get("token")
    if isinstance(token_value, str) and token_value.strip():
        if not 16 <= len(token_value) <= 256:
            raise BridgeError("BRIDGE-CONFIG-INVALID", "旧版手动密钥长度无效。")
        port_value = value.get("port", DEFAULT_PORT)
        if isinstance(port_value, bool) or not isinstance(port_value, int) or not 1024 <= port_value <= 65535:
            raise BridgeError("BRIDGE-CONFIG-INVALID", "旧版手动端口无效。")
        return BridgeCandidate(
            BridgeSettings(
                port=port_value,
                token=token_value,
                connect_timeout_seconds=base_settings.connect_timeout_seconds,
                request_timeout_seconds=base_settings.request_timeout_seconds,
            ),
            "manual_credentials",
            "manual-credentials",
        )

    explicit_path = value.get("himi_yui_config_path")
    if isinstance(explicit_path, str) and explicit_path.strip():
        return _required_config_candidate(
            Path(explicit_path).expanduser(),
            "manual_path",
            base_settings,
        )
    environment_path = os.environ.get("HIMIYUI_CONFIG_PATH")
    if environment_path:
        return _required_config_candidate(
            Path(environment_path).expanduser(),
            "environment",
            base_settings,
        )
    return None


def _required_config_candidate(
    path: Path,
    source: str,
    base_settings: BridgeSettings,
) -> BridgeCandidate:
    if not path.is_absolute():
        raise BridgeError(
            "DISCOVERY-PATH-INVALID",
            "旧版兼容路径必须是绝对路径；请清除覆盖并恢复自动发现。",
        )
    candidate = _candidate_from_config(path, source, base_settings)
    if candidate is None:
        raise BridgeError(
            "DISCOVERY-PATH-INVALID",
            "旧版兼容路径不是可用的 HimiYui 配置；请清除覆盖并恢复自动发现。",
        )
    return candidate


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
        candidate = _read_descriptor(path, base_settings)
        if candidate is None:
            invalid += 1
        else:
            candidates.append(candidate)
    return candidates, invalid


def _read_descriptor(
    path: Path,
    base_settings: BridgeSettings,
) -> BridgeCandidate | None:
    match = re.fullmatch(r"([0-9a-f]{32})\.json", path.name)
    if match is None or path.is_symlink():
        return None
    try:
        if not path.is_file() or path.stat().st_size > MAXIMUM_DESCRIPTOR_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    instance_id = payload.get("instance_id")
    process_id = payload.get("process_id")
    started_at_utc = payload.get("started_at_utc")
    port = payload.get("port")
    config_path = payload.get("config_path")
    if (
        payload.get("discovery_version") != DISCOVERY_VERSION
        or payload.get("protocol_version") != PROTOCOL_VERSION
        or instance_id != match.group(1)
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
        or payload.get("host") != "127.0.0.1"
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1024 <= port <= 65535
        or not isinstance(config_path, str)
        or not config_path
        or len(config_path) > 4096
        or not Path(config_path).is_absolute()
        or not _valid_utc_timestamp(started_at_utc)
    ):
        return None
    candidate = _candidate_from_config(
        Path(config_path),
        "discovery_v1",
        base_settings,
        instance_id=instance_id,
        started_at_utc=started_at_utc,
    )
    if candidate is None or candidate.settings.port != port:
        return None
    return candidate


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _discovery_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        _append_absolute_root(
            roots,
            os.environ.get("LOCALAPPDATA"),
            "HimiYui",
            "bridge-discovery",
            "v1",
        )
        return roots
    if sys.platform == "darwin":
        _append_absolute_root(
            roots,
            os.environ.get("HOME"),
            "Library",
            "Application Support",
            "HimiYui",
            "bridge-discovery",
            "v1",
        )
        return roots
    _append_absolute_root(
        roots,
        os.environ.get("XDG_RUNTIME_DIR"),
        "himiyui",
        "bridge-discovery",
        "v1",
    )
    _append_absolute_root(
        roots,
        os.environ.get("XDG_STATE_HOME"),
        "himiyui",
        "bridge-discovery",
        "v1",
    )
    _append_absolute_root(
        roots,
        os.environ.get("HOME"),
        ".local",
        "state",
        "himiyui",
        "bridge-discovery",
        "v1",
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


def _legacy_candidates(base_settings: BridgeSettings) -> list[BridgeCandidate]:
    candidates: list[BridgeCandidate] = []
    for path in _standard_himiyui_config_paths():
        candidate = _candidate_from_config(path, "legacy_scan", base_settings)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidate_from_config(
    path: Path,
    source: str,
    base_settings: BridgeSettings,
    *,
    instance_id: str = "",
    started_at_utc: str = "",
) -> BridgeCandidate | None:
    loaded = _read_himiyui_config(path)
    if loaded is None:
        return None
    port, token = loaded
    normalized_path = Path(os.path.abspath(path))
    opaque_id = instance_id or _opaque_instance_id(source, normalized_path, port)
    return BridgeCandidate(
        BridgeSettings(
            port=port,
            token=token,
            connect_timeout_seconds=base_settings.connect_timeout_seconds,
            request_timeout_seconds=base_settings.request_timeout_seconds,
        ),
        source,
        opaque_id,
        normalized_path,
        started_at_utc,
    )


def _opaque_instance_id(source: str, path: Path, port: int) -> str:
    digest = hashlib.sha256(f"{source}\0{str(path).casefold()}\0{port}".encode()).hexdigest()
    return f"compat-{digest[:24]}"


def _deduplicate_candidates(candidates: list[BridgeCandidate]) -> list[BridgeCandidate]:
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.source == "discovery_v1",
            candidate.started_at_utc,
        ),
        reverse=True,
    )
    result: list[BridgeCandidate] = []
    seen: set[str] = set()
    for candidate in ordered:
        key = (
            str(candidate.config_path).casefold()
            if candidate.config_path is not None
            else f"{candidate.source}:{candidate.settings.port}"
        )
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
                response = await YuiStardewBridgeClient(candidate.settings).request("status")
            except BridgeError as error:
                return None, error.code
            if response.get("ok") is not True:
                return None, str(response.get("code") or "BRIDGE-STATUS-FAILED")
            return VerifiedCandidate(candidate, response), ""

    results = await asyncio.gather(*(probe(candidate) for candidate in candidates))
    verified = [candidate for candidate, _code in results if candidate is not None]
    failures = [code for candidate, code in results if candidate is None]
    return verified, failures


def _standard_himiyui_config_paths() -> list[Path]:
    home = Path.home()
    steam_roots: list[Path] = []
    for variable in ("STEAM_PATH", "STEAM_HOME"):
        value = os.environ.get(variable)
        if value:
            steam_roots.append(Path(value).expanduser())
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        value = os.environ.get(variable)
        if value:
            steam_roots.append(Path(value) / "Steam")
    steam_roots.extend(
        [
            Path("C:/steam"),
            Path("C:/Steam"),
            home / ".steam" / "steam",
            home / ".local" / "share" / "Steam",
            home / "Library" / "Application Support" / "Steam",
        ]
    )
    steam_roots.extend(_windows_steam_roots())
    steam_roots.extend(_steam_library_roots(steam_roots))
    suffix = Path("steamapps/common/Stardew Valley/Mods/HimiYui/config.json")
    paths: list[Path] = []
    seen: set[str] = set()
    for root in steam_roots:
        path = root / suffix
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def _windows_steam_roots() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    roots: list[Path] = []
    for hive, key_name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _kind = winreg.QueryValueEx(key, "SteamPath")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            roots.append(Path(value.strip()))
    return roots


def _steam_library_roots(steam_roots: list[Path]) -> list[Path]:
    libraries: list[Path] = []
    for steam_root in steam_roots:
        library_file = steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            if not library_file.is_file() or library_file.stat().st_size > 2 * 1024 * 1024:
                continue
            payload = library_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s*"([^"]+)"', payload, flags=re.IGNORECASE):
            raw_path = match.group(1).replace(r"\\", "\\")
            if raw_path:
                libraries.append(Path(raw_path))
    return libraries


def _read_himiyui_config(path: Path) -> tuple[int, str] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAXIMUM_CONFIG_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("EnableNekoBridge") is False:
        return None
    token = payload.get("NekoBridgeToken")
    port = payload.get("NekoBridgePort", DEFAULT_PORT)
    if not isinstance(token, str) or not 16 <= len(token) <= 256:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        return None
    return port, token
