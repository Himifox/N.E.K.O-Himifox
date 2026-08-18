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

from pathlib import Path

import httpx
import pytest

pytest.importorskip("openbiliclaw")

from app import openbiliclaw_runtime


def test_neko_model_route_is_projected_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.config import load_config
    from utils import config_manager

    class _ConfigManager:
        @staticmethod
        def get_model_api_config(model_type: str) -> dict[str, object]:
            assert model_type == "conversation"
            return {
                "model": "neko-model",
                "api_key": "secret-never-written",
                "base_url": "https://model.example/v1",
                "provider_type": "anthropic",
            }

    monkeypatch.setattr(config_manager, "get_config_manager", lambda: _ConfigManager())
    config_path = tmp_path / "missing-config.toml"
    config = load_config(config_path, consult_environment=False)

    projected = openbiliclaw_runtime._apply_neko_model_config(config)

    instance = projected.llm.instances["neko-conversation"]
    assert projected.llm.default_chain == ["neko-conversation"]
    assert instance.provider_type == "claude"
    assert instance.api_key == ""
    assert config_path.exists() is False


async def test_pinned_core_builds_with_real_fastapi_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    # This contract smoke is about package/runtime compatibility. Model
    # projection has focused unit coverage and would otherwise read the
    # developer's real N.E.K.O profile.
    monkeypatch.setattr(openbiliclaw_runtime, "_apply_neko_model_config", lambda config: config)

    runtime = openbiliclaw_runtime.NekoOpenBiliClawRuntime(
        tmp_path / "integration",
        enabled=True,
        port=unused_tcp_port,
    )
    try:
        status = await runtime.start()
        assert status.state == "running"
        assert status.bridge_running is True
        assert runtime.core.config.data_path == tmp_path / "integration" / "data"

        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"{status.endpoint}/api/ping")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["service"] == "openbiliclaw-api"
    finally:
        await runtime.stop()
