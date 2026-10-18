from __future__ import annotations

import json
import os

import pytest

from plugin.plugins.yui_stardew import discovery
from plugin.plugins.yui_stardew.bridge import BridgeSettings


@pytest.mark.parametrize(
    ("platform_name", "system", "environment", "expected"),
    [
        (
            "nt",
            "win32",
            {"LOCALAPPDATA": "local"},
            [
                ("local", "YuiToIssho", "agent-gateway-discovery", "v2"),
            ],
        ),
        (
            "posix",
            "darwin",
            {"HOME": "home"},
            [
                (
                    "home",
                    "Library",
                    "Application Support",
                    "YuiToIssho",
                    "agent-gateway-discovery",
                    "v2",
                ),
            ],
        ),
        (
            "posix",
            "linux",
            {"HOME": "home", "XDG_RUNTIME_DIR": "runtime", "XDG_STATE_HOME": "state"},
            [
                ("runtime", "yuitoissho", "agent-gateway-discovery", "v2"),
                ("state", "yuitoissho", "agent-gateway-discovery", "v2"),
                (
                    "home",
                    ".local",
                    "state",
                    "yuitoissho",
                    "agent-gateway-discovery",
                    "v2",
                ),
            ],
        ),
    ],
)
def test_discovery_uses_game_published_directories(
    monkeypatch,
    platform_name,
    system,
    environment,
    expected,
):
    calls = []
    # Capture directory selection without constructing foreign-platform Paths.
    with monkeypatch.context() as patch:
        patch.setattr(discovery.os, "name", platform_name)
        patch.setattr(discovery.sys, "platform", system)
        patch.setattr(discovery.os, "environ", environment)
        patch.setattr(
            discovery,
            "_append_absolute_root",
            lambda roots, root, *segments: calls.append((root, *segments)),
        )
        discovery._discovery_roots()
    assert calls == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://127.0.0.1:12345/himiyui/agent/v2",
        "ws://192.168.1.2:12345/yuitoissho/agent/v2",
        "ws://user:password@127.0.0.1:12345/yuitoissho/agent/v2",
        "ws://127.0.0.1:12345/yuitoissho/agent/v2?override=1",
    ],
)
def test_discovery_rejects_noncanonical_endpoints(endpoint):
    assert not discovery._valid_gateway_endpoint(endpoint)


def test_game_descriptor_and_config_are_accepted(tmp_path):
    mod_directory = tmp_path / "YuiToIssho"
    mod_directory.mkdir()
    config_path = mod_directory / "config.json"
    token = "test-only-key-1234567890"
    config_path.write_text(
        json.dumps(
            {
                "EnableNekoBridge": True,
                "NekoBridgeToken": token,
            }
        ),
        encoding="utf-8",
    )
    instance_id = "a" * 32
    descriptor_path = tmp_path / f"{instance_id}.json"
    endpoint = "ws://127.0.0.1:12345/yuitoissho/agent/v2"
    descriptor_path.write_text(
        json.dumps(
            {
                "discovery_version": 2,
                "protocol_version": 2,
                "instance_id": instance_id,
                "process_id": os.getpid(),
                "created_at_utc": "2026-10-12T00:00:00.000Z",
                "transport": "websocket",
                "endpoint": endpoint,
                "config_path": str(config_path),
            }
        ),
        encoding="utf-8",
    )

    candidate = discovery._read_descriptor(descriptor_path, BridgeSettings())

    assert candidate is not None
    assert candidate.settings.endpoint == endpoint
    assert candidate.settings.token == token
    assert candidate.config_path == config_path

    # The renamed directory must not bypass the bridge enablement gate.
    config_path.write_text(
        json.dumps(
            {
                "EnableNekoBridge": False,
                "NekoBridgeToken": token,
            }
        ),
        encoding="utf-8",
    )
    assert discovery._read_descriptor(descriptor_path, BridgeSettings()) is None


def test_config_directory_remains_restricted(tmp_path):
    assert discovery._valid_config_path(tmp_path / "YuiToIssho" / "config.json")
    assert not discovery._valid_config_path(tmp_path / "HimiYui" / "config.json")
    assert not discovery._valid_config_path(tmp_path / "unrelated" / "config.json")
    assert not discovery._valid_config_path(tmp_path / "YuiToIssho" / "other.json")


def _write_discovery_descriptor(root, *, instance_id="a" * 32, process_id=42, endpoint="ws://127.0.0.1:12345/yuitoissho/agent/v2", config_path=""):
    path = root / f"{instance_id}.json"
    path.write_text(json.dumps({
        "discovery_version": 2,
        "protocol_version": 2,
        "instance_id": instance_id,
        "process_id": process_id,
        "created_at_utc": "2026-10-12T00:00:00.000Z",
        "transport": "websocket",
        "endpoint": endpoint,
        "config_path": str(config_path),
    }), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_exited_v2_descriptor_is_waiting_for_start_without_reading_config(tmp_path, monkeypatch):
    descriptor = _write_discovery_descriptor(tmp_path, endpoint="invalid")
    original = descriptor.read_bytes()
    monkeypatch.setattr(discovery, "_discovery_roots", lambda: [tmp_path])
    monkeypatch.setattr(discovery, "_process_exists", lambda _pid: False)

    def unexpected_config_read(_path):
        raise AssertionError("stale process must be ignored before reading configuration")

    monkeypatch.setattr(discovery, "_read_yuitoissho_token", unexpected_config_read)
    assert discovery._read_descriptor(descriptor, BridgeSettings()) is None
    result = await discovery.discover_bridge_connection({})
    assert result.state == "not_found"
    assert result.code == "DISCOVERY-NOT-FOUND"
    assert descriptor.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["endpoint", "json", "identity", "pid"])
async def test_live_or_unidentifiable_broken_descriptors_remain_invalid(tmp_path, monkeypatch, damage):
    descriptor = _write_discovery_descriptor(tmp_path, endpoint="invalid")
    if damage == "json":
        descriptor.write_text("{broken", encoding="utf-8")
    elif damage in {"identity", "pid"}:
        payload = json.loads(descriptor.read_text(encoding="utf-8"))
        payload["instance_id" if damage == "identity" else "process_id"] = "untrusted"
        descriptor.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(discovery, "_discovery_roots", lambda: [tmp_path])
    monkeypatch.setattr(discovery, "_process_exists", lambda _pid: damage == "endpoint")

    result = await discovery.discover_bridge_connection({})
    assert result.state == "invalid"
    assert result.code == "DISCOVERY-DESCRIPTOR-INVALID"


@pytest.mark.asyncio
async def test_stale_and_live_v2_descriptors_connect_to_live_instance(tmp_path, monkeypatch):
    stale = _write_discovery_descriptor(tmp_path, process_id=41, endpoint="invalid")
    config = tmp_path / "YuiToIssho" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"EnableNekoBridge": True, "NekoBridgeToken": "test-only-key-1234567890"}), encoding="utf-8")
    live_id = "b" * 32
    _write_discovery_descriptor(tmp_path, instance_id=live_id, process_id=42, config_path=config)
    monkeypatch.setattr(discovery, "_discovery_roots", lambda: [tmp_path])
    monkeypatch.setattr(discovery, "_process_exists", lambda pid: pid == 42)
    probed = []

    async def probe_candidates(candidates):
        probed.extend(candidate.instance_id for candidate in candidates)
        return ([discovery.VerifiedCandidate(candidate, {"ok": True, "state": {"world_ready": True, "host_authoritative": True}}) for candidate in candidates], [])

    monkeypatch.setattr(discovery, "_probe_candidates", probe_candidates)
    result = await discovery.discover_bridge_connection({})
    assert result.state == "ready"
    assert result.selected.candidate.instance_id == live_id
    assert probed == [live_id]
    assert stale.is_file()
