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

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def test_neko_model_route_fails_closed_when_live_route_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.config import LLMInstanceConfig, load_config
    from utils import config_manager

    class _UnavailableConfigManager:
        @staticmethod
        def get_model_api_config(_model_type: str) -> dict[str, object]:
            raise RuntimeError("conversation route unavailable")

    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda: _UnavailableConfigManager(),
    )
    config = load_config(tmp_path / "missing.toml", consult_environment=False)
    legacy = LLMInstanceConfig(
        name="legacy-direct",
        provider_type="deepseek",
        api_key="legacy-secret",
        model="legacy-model",
        base_url="https://api.deepseek.com",
        enabled=True,
    )
    config = replace(
        config,
        llm=replace(
            config.llm,
            instance_routing=True,
            instances={"legacy-direct": legacy},
            default_chain=["legacy-direct"],
        ),
    )

    projected = openbiliclaw_runtime._apply_neko_model_config(config)

    assert set(projected.llm.instances) == {"neko-conversation"}
    assert projected.llm.default_chain == ["neko-conversation"]
    assert projected.llm.instances["neko-conversation"].api_key == ""
    assert projected.llm.instances["neko-conversation"].model == "neko-managed"
    assert projected.llm.instances["neko-conversation"].enabled is False


def test_neko_public_free_route_is_disabled_for_background_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.config import load_config
    from utils import config_manager

    class _FreeConfigManager:
        @staticmethod
        def get_model_api_config(_model_type: str) -> dict[str, object]:
            return {
                "model": "free-model",
                "base_url": "https://www.lanlan.tech/text/v1",
                "provider_type": "openai_compatible",
            }

    monkeypatch.setattr(config_manager, "get_config_manager", lambda: _FreeConfigManager())
    config = load_config(tmp_path / "missing.toml", consult_environment=False)

    projected = openbiliclaw_runtime._apply_neko_model_config(config)

    assert projected.llm.instances["neko-conversation"].enabled is False


async def test_embedded_config_scrubs_legacy_key_and_reload_keeps_neko_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.config import LLMInstanceConfig, load_config, save_config
    from utils import config_manager

    class _ConfigManager:
        @staticmethod
        def get_model_api_config(_model_type: str) -> dict[str, object]:
            return {
                "model": "neko-model",
                "base_url": "https://model.example/v1",
                "provider_type": "openai",
            }

    monkeypatch.setattr(config_manager, "get_config_manager", lambda: _ConfigManager())
    data_root = tmp_path / "embedded"
    config_path = data_root / "config.toml"
    raw = load_config(config_path, consult_environment=False)
    legacy = LLMInstanceConfig(
        name="legacy-direct",
        provider_type="deepseek",
        api_key="legacy-secret-never-survives",
        model="legacy-model",
        base_url="https://api.deepseek.com",
        enabled=True,
    )
    raw = replace(
        raw,
        llm=replace(
            raw.llm,
            instance_routing=True,
            instances={"legacy-direct": legacy},
            default_chain=["legacy-direct"],
        ),
    )
    save_config(raw, config_path, preserve_override_provenance=False)

    core, _app = openbiliclaw_runtime._load_openbiliclaw_backend(data_root, 8420)
    try:
        assert "legacy-secret-never-survives" not in config_path.read_text(encoding="utf-8")
        assert core.config.llm.default_chain == ["neko-conversation"]
        assert core.context.surface_copy_mode == "lazy"
        assert core.context.runtime_controller.background_expression_copy_enabled is False
        assert core.context.runtime_controller.expression_copy_coordinator is None

        await core.reload(raw)

        assert core.config.llm.default_chain == ["neko-conversation"]
        assert set(core.config.llm.instances) == {"neko-conversation"}
        assert core.context.surface_copy_mode == "lazy"
        assert core.context.runtime_controller.background_expression_copy_enabled is False
        assert core.context.runtime_controller.expression_copy_coordinator is None
    finally:
        await core.stop()


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


async def test_real_core_ranks_three_but_phase1_receives_only_first() -> None:
    from openbiliclaw import OpenBiliClawCore
    from openbiliclaw.discovery.engine import DiscoveredContent
    from openbiliclaw.recommendation.engine import Recommendation
    from openbiliclaw.soul.profile import InterestTag, PreferenceLayer, SoulProfile
    from main_logic.proactive_chat.openbiliclaw_candidate import (
        format_phase1_candidate,
        format_phase2_candidate,
        project_openbiliclaw_candidate,
    )

    now = datetime.now(UTC)
    profile = SoulProfile(
        preferences=PreferenceLayer(
            interests=[
                InterestTag(
                    name="Agent architecture",
                    category="AI coding",
                    state="active",
                    evidence_count=8,
                    first_seen=now - timedelta(days=45),
                    last_seen=now - timedelta(days=1),
                    last_evidence_at=(now - timedelta(days=1)).isoformat(),
                )
            ],
            source_platform_mix={"bilibili": 1.0},
        )
    )
    recommendations = [
        Recommendation(
            content=DiscoveredContent(
                bvid=f"BV{index}",
                title=f"Agent architecture topic {index}",
                description=f"Structured summary {index}",
                content_url=f"https://www.bilibili.com/video/BV{index}",
                source_platform="bilibili",
                author_name="Creator",
                content_type="video",
                temporal_class="current",
                temporal_policy_version="v2",
                temporal_evaluated_at=now.isoformat(),
                temporal_evidence_complete=True,
                topic_group="Agent architecture",
                relevance_score=0.8,
                quality_score=0.85,
                summary_quality_score=0.8,
                evaluation_contract_version="content-eval-v8",
            ),
            expression=f"private expression {index}",
            topic_label="Agent architecture",
            confidence=0.8,
        )
        for index in range(3)
    ]
    delivered: list[Recommendation] = []

    class _Soul:
        async def get_profile(self) -> SoulProfile:
            return profile

    class _Engine:
        async def preview_semantic(
            self, *_args: object, **_kwargs: object
        ) -> list[Recommendation]:
            return recommendations

        async def record_delivery(
            self,
            recommendation: Recommendation,
            *,
            surface: str,
        ) -> int:
            assert surface == "neko_proactive"
            delivered.append(recommendation)
            return len(delivered)

    database = SimpleNamespace(
        get_saved_topic_signals=lambda: {},
        get_enabled_recipes=lambda: [],
    )
    core = OpenBiliClawCore.from_context(
        SimpleNamespace(
            soul_engine=_Soul(),
            recommendation_engine=_Engine(),
            database=database,
            degraded=False,
        )
    )

    candidates = await core.preview_proactive_candidates(limit=3)
    envelopes = [
        project_openbiliclaw_candidate(candidate, language="en")
        for candidate in candidates
    ]
    phase1_prompt = "\n".join(
        format_phase1_candidate(index, envelope)
        for index, envelope in enumerate(envelopes[:1], start=1)
    )
    phase2_prompt = format_phase2_candidate(envelopes[0], language="en")

    assert len(envelopes) == 3
    assert "Agent architecture topic 0" in phase1_prompt
    assert "Agent architecture topic 1" not in phase1_prompt
    assert "Agent architecture topic 2" not in phase1_prompt
    for forbidden in (
        "candidate_id",
        "item_key",
        "delivery_ref",
        "https://",
        "private expression",
    ):
        assert forbidden not in phase1_prompt
        assert forbidden not in phase2_prompt

    await core.record_recommendation_delivery(
        envelopes[0].tracking.delivery_ref,
        surface="neko_proactive",
    )
    assert delivered == [recommendations[0]]
