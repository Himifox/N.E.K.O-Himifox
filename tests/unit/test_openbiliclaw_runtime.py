# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.openbiliclaw_runtime import NekoOpenBiliClawRuntime, _openbiliclaw_provider_type


class _FakeCore:
    degraded = False

    def __init__(self) -> None:
        self.stop_calls = 0
        self.events: list[dict[str, object]] = []
        self.proactive_usage: list[dict[str, object]] = []

    async def stop(self) -> None:
        self.stop_calls += 1

    async def get_profile(self) -> dict[str, bool]:
        return {"initialized": True}

    async def recommend(self, *, limit: int) -> list[str]:
        return ["item"] * limit

    async def publish_event(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def record_proactive_llm_usage(self, **usage: object) -> int:
        self.proactive_usage.append(usage)
        return len(self.proactive_usage)


class _FakeBridgeApp:
    def __init__(self, core: _FakeCore) -> None:
        self.core = core


class _FakeServer:
    def __init__(self, app: _FakeBridgeApp, *, starts: bool = True) -> None:
        self.app = app
        self.starts = starts
        self.started = False
        self.should_exit = False

    async def serve(self) -> None:
        if not self.starts:
            return
        self.started = True
        try:
            while not self.should_exit:
                await asyncio.sleep(0.01)
        finally:
            await self.app.core.stop()


def _runtime(tmp_path: Path, *, starts: bool = True, enabled: bool = True):
    core = _FakeCore()
    load_calls: list[tuple[Path, int]] = []
    servers: list[_FakeServer] = []

    def load_backend(data_root: Path, port: int) -> tuple[Any, Any]:
        load_calls.append((data_root, port))
        return core, _FakeBridgeApp(core)

    def server_factory(app: Any, host: str, port: int) -> _FakeServer:
        assert host == "127.0.0.1"
        assert port == 8420
        server = _FakeServer(app, starts=starts)
        servers.append(server)
        return server

    runtime = NekoOpenBiliClawRuntime(
        tmp_path / "openbiliclaw",
        enabled=enabled,
        port=8420,
        backend_loader=load_backend,
        server_factory=server_factory,
    )
    return runtime, core, load_calls, servers


async def test_runtime_owns_one_core_and_bridge_lifecycle(tmp_path: Path) -> None:
    runtime, core, load_calls, servers = _runtime(tmp_path)

    status = await runtime.start()
    duplicate_status = await runtime.start()

    assert status.state == "running"
    assert status.bridge_running is True
    assert status.endpoint == "http://127.0.0.1:8420"
    assert duplicate_status == status
    assert load_calls == [(tmp_path / "openbiliclaw", 8420)]
    assert len(servers) == 1
    assert await runtime.get_profile() == {"initialized": True}
    assert await runtime.recommend(limit=2) == ["item", "item"]
    await runtime.publish_behavior_event({"event_type": "view"})
    assert core.events == [{"event_type": "view"}]

    await runtime.stop()

    assert runtime.status.state == "stopped"
    assert runtime.status.bridge_running is False
    assert runtime.core is None
    assert core.stop_calls == 1


async def test_runtime_mirrors_neko_phase_usage_into_core_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, core, _load_calls, _servers = _runtime(tmp_path)
    await runtime.start()
    monkeypatch.setattr(
        runtime,
        "_provider_type_for_model",
        lambda model: "openai" if model == "gpt-test" else "unknown",
    )

    runtime._observe_neko_usage(
        {
            "model": "gpt-test",
            "pt": 120,
            "ct": 30,
            "cch": 20,
            "type": "proactive.phase2",
            "ok": True,
        }
    )
    await asyncio.sleep(0)
    await asyncio.gather(*runtime._usage_mirror_tasks)

    assert core.proactive_usage == [
        {
            "phase": "phase2",
            "provider": "openai",
            "model": "gpt-test",
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "cached_input_tokens": 20,
        }
    ]
    await runtime.stop()


async def test_disabled_runtime_never_loads_backend(tmp_path: Path) -> None:
    runtime, _core, load_calls, _servers = _runtime(tmp_path, enabled=False)

    status = await runtime.start()

    assert status.state == "disabled"
    assert status.enabled is False
    assert load_calls == []


async def test_bridge_bind_failure_isolated_from_neko(tmp_path: Path) -> None:
    runtime, core, _load_calls, _servers = _runtime(tmp_path, starts=False)

    status = await runtime.start()

    assert status.state == "unavailable"
    assert status.bridge_running is False
    assert status.core_available is False
    assert "could not bind" in status.error
    assert core.stop_calls == 1


async def test_degraded_core_keeps_extension_bridge_available(tmp_path: Path) -> None:
    runtime, core, _load_calls, _servers = _runtime(tmp_path)
    core.degraded = True

    status = await runtime.start()

    assert status.state == "degraded"
    assert status.degraded is True
    assert status.bridge_running is True

    await runtime.stop()


def test_neko_provider_names_map_to_core_adapters() -> None:
    assert _openbiliclaw_provider_type("anthropic") == "claude"
    assert _openbiliclaw_provider_type("google") == "gemini"
    assert _openbiliclaw_provider_type("openrouter") == "openrouter"
    assert _openbiliclaw_provider_type("qwen") == "openai_compatible"
