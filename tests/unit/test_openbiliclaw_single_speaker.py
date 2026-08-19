# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import openbiliclaw_runtime
from main_logic.proactive_chat import candidate_selection, delivery, sources
from main_logic.proactive_chat.contracts import ProactiveChatCommand
from main_logic.proactive_chat.openbiliclaw_candidate import (
    project_openbiliclaw_candidate,
)


class _DynamicConfigManager:
    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self.snapshots = snapshots
        self.index = 0

    async def aget_model_api_config(self, model_type: str) -> dict[str, Any]:
        assert model_type == "conversation"
        snapshot = self.snapshots[self.index]
        self.index += 1
        return snapshot


class _FakeChatClient:
    def __init__(self, calls: list[dict[str, Any]], *, block: bool = False) -> None:
        self.calls = calls
        self.block = block
        self.entered = asyncio.Event()
        self.exited = False

    async def __aenter__(self) -> _FakeChatClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
        from utils.token_tracker.call_context import _current_call_type

        self.calls[-1].update(
            {
                "messages": messages,
                "invoke_kwargs": kwargs,
                "call_type": _current_call_type.get(),
            }
        )
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        return SimpleNamespace(
            content='{"ok": true}',
            response_metadata={"token_usage": {"input_tokens": 11, "output_tokens": 7}},
        )


def test_managed_provider_cache_namespace_tracks_route_without_credentials() -> None:
    snapshot = {
        "model": "route-one",
        "base_url": "https://one.example/v1",
        "api_key": "secret-one",
        "provider_type": "openai",
    }
    manager = SimpleNamespace(
        get_model_api_config=lambda _model_type: dict(snapshot)
    )
    provider = openbiliclaw_runtime.NekoManagedLLMProvider(manager)

    first = provider.cache_namespace()
    snapshot["api_key"] = "secret-two"
    assert provider.cache_namespace() == first
    snapshot["model"] = "route-two"
    assert provider.cache_namespace() != first


async def test_managed_provider_uses_live_route_json_budget_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _DynamicConfigManager(
        [
            {
                "model": "route-one",
                "base_url": "https://one.example/v1",
                "api_key": "key-one",
                "provider_type": "openai",
            },
            {
                "model": "route-two",
                "base_url": "https://two.example/v1",
                "api_key": "key-two",
                "provider_type": "openai",
            },
        ]
    )
    calls: list[dict[str, Any]] = []

    async def _factory(model: str, base_url: str, api_key: str, **kwargs: Any) -> Any:
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "factory_kwargs": kwargs,
            }
        )
        return _FakeChatClient(calls)

    from utils import llm_client

    monkeypatch.setattr(llm_client, "create_chat_llm_async", _factory)
    provider = openbiliclaw_runtime.NekoManagedLLMProvider(manager)
    messages = [
        {"role": "system", "content": "Choose"},
        {"role": "assistant", "content": "Earlier"},
        {"role": "user", "content": "Now"},
    ]

    first = await provider.complete(
        messages,
        temperature=1.7,
        max_tokens=321,
        json_mode=True,
        model="must-not-override-live-route",
    )
    second = await provider.complete(messages, max_tokens=123)

    assert [call["model"] for call in calls] == ["route-one", "route-two"]
    assert calls[0]["factory_kwargs"]["max_completion_tokens"] == 321
    assert "temperature" not in calls[0]["factory_kwargs"]
    assert calls[0]["invoke_kwargs"]["response_format"] == {"type": "json_object"}
    assert calls[0]["call_type"] == "openbiliclaw"
    assert [type(message).__name__ for message in calls[0]["messages"][:3]] == [
        "SystemMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert first.model == "route-one"
    assert first.usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert second.model == "route-two"


async def test_managed_provider_cancellation_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _DynamicConfigManager(
        [{"model": "route", "base_url": "https://example/v1", "api_key": "key"}]
    )
    calls: list[dict[str, Any]] = []
    client = _FakeChatClient(calls, block=True)

    async def _factory(model: str, base_url: str, api_key: str, **kwargs: Any) -> Any:
        calls.append({"model": model, "base_url": base_url, "api_key": api_key})
        return client

    from utils import llm_client

    monkeypatch.setattr(llm_client, "create_chat_llm_async", _factory)
    task = asyncio.create_task(
        openbiliclaw_runtime.NekoManagedLLMProvider(manager).complete(
            [{"role": "user", "content": "wait"}]
        )
    )
    await client.entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.exited is True


async def test_managed_provider_error_does_not_expose_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "secret-must-not-leak"
    manager = _DynamicConfigManager(
        [{"model": "route", "base_url": "https://example/v1", "api_key": secret}]
    )

    async def _factory(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"upstream rejected {secret}")

    from utils import llm_client

    monkeypatch.setattr(llm_client, "create_chat_llm_async", _factory)
    from openbiliclaw.llm.base import LLMProviderError

    with pytest.raises(LLMProviderError) as caught:
        await openbiliclaw_runtime.NekoManagedLLMProvider(manager).complete(
            [{"role": "user", "content": "hello"}]
        )
    assert secret not in str(caught.value)


async def test_managed_provider_does_not_call_public_free_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _DynamicConfigManager(
        [
            {
                "model": "free-model",
                "base_url": "https://www.lanlan.tech/text/v1",
                "api_key": "public-route-token",
                "provider_type": "openai_compatible",
            }
        ]
    )
    factory = AsyncMock()
    from utils import llm_client

    monkeypatch.setattr(llm_client, "create_chat_llm_async", factory)
    from openbiliclaw.llm.base import LLMProviderError

    with pytest.raises(LLMProviderError, match="public free model"):
        await openbiliclaw_runtime.NekoManagedLLMProvider(manager).complete(
            [{"role": "user", "content": "background analysis"}]
        )

    factory.assert_not_awaited()


async def test_openbiliclaw_source_is_structured_and_preview_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommendation = object()
    candidate = SimpleNamespace(
        tracking=SimpleNamespace(
            candidate_id="obc:1234",
            item_key="bilibili:BV1",
            url="https://example/video",
            expires_at=None,
            delivery_ref=recommendation,
        ),
        semantics=SimpleNamespace(
            title="A useful video",
            topic="Systems thinking",
            summary="A bounded summary",
            reason_codes=("recent_interest",),
            source_platform="bilibili",
            author_name="Creator",
            content_type="video",
            confidence=0.91,
            freshness="recent",
        ),
        policy=SimpleNamespace(
            sensitivity="none",
            proactive_policy="allow",
            why_now_source="aggregated_interest",
        ),
    )
    second_candidate = copy.deepcopy(candidate)
    second_candidate.semantics.title = "Second ranked video"
    second_candidate.tracking.url = "https://example/second"
    runtime = SimpleNamespace(
        proactive_available=True,
        preview_proactive_candidates=AsyncMock(
            return_value=[candidate, second_candidate]
        ),
    )
    monkeypatch.setattr(
        openbiliclaw_runtime,
        "get_openbiliclaw_runtime",
        lambda: runtime,
    )

    mode, payload = await sources._fetch_source(
        "openbiliclaw",
        command=ProactiveChatCommand(),
        lanlan_name="Neko",
        log=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )

    assert mode == "openbiliclaw"
    runtime.preview_proactive_candidates.assert_awaited_once_with(
        limit=3,
        explicit_context_texts=(),
    )
    link = payload["links"][0]
    assert len(payload["links"]) == 1
    assert {key: link[key] for key in ("title", "url", "source", "mode")} == {
        "title": "A useful video",
        "url": "https://example/video",
        "source": "OpenBiliClaw",
        "mode": "openbiliclaw",
    }
    assert link["_openbiliclaw_candidate"].tracking.delivery_ref is recommendation


def test_phase1_reserves_exactly_one_openbiliclaw_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate_selection, "_should_skip_source", lambda _key: False)
    sources_by_mode = {
        "openbiliclaw": {
            "links": [
                {"title": f"obc-{index}", "url": f"https://obc/{index}"}
                for index in range(3)
            ]
        },
        "personal": {
            "links": [
                {"title": f"personal-{index}", "url": f"https://personal/{index}"}
                for index in range(3)
            ]
        },
        "news": {
            "links": [
                {"title": f"news-{index}", "url": f"https://news/{index}"}
                for index in range(3)
            ]
        },
    }

    selected = candidate_selection._round_robin_phase1_links(
        ["openbiliclaw", "personal", "news"],
        sources_by_mode,
        total=5,
        reserved_mode="openbiliclaw",
    )

    assert len(selected["openbiliclaw"]) == 1
    assert selected["openbiliclaw"][0]["title"] == "obc-0"
    assert len(selected["personal"]) + len(selected["news"]) == 4


def test_proactive_stage_usage_has_distinct_phase_names() -> None:
    generation_source = (
        Path(__file__).resolve().parents[2]
        / "main_logic"
        / "proactive_chat"
        / "generation.py"
    ).read_text(encoding="utf-8")

    assert 'set_call_type("proactive.phase1")' in generation_source
    assert 'set_call_type("proactive.phase2")' in generation_source


async def test_delivery_acknowledges_only_the_committed_openbiliclaw_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommendation = object()
    envelope = project_openbiliclaw_candidate(
        SimpleNamespace(
            tracking=SimpleNamespace(
                candidate_id="obc:chosen",
                item_key="bilibili:chosen",
                url="https://example/chosen",
                expires_at=None,
                delivery_ref=recommendation,
            ),
            semantics=SimpleNamespace(
                title="Chosen",
                topic="Topic",
                summary="Summary",
                reason_codes=("recent_interest",),
                source_platform="bilibili",
                author_name="Author",
                content_type="video",
                confidence=0.8,
                freshness="recent",
            ),
            policy=SimpleNamespace(
                sensitivity="none",
                proactive_policy="allow",
                why_now_source="aggregated_interest",
            ),
        ),
        language="en",
    )
    runtime = SimpleNamespace(record_recommendation_delivery=AsyncMock(return_value=42))
    monkeypatch.setattr(
        openbiliclaw_runtime,
        "get_openbiliclaw_runtime",
        lambda: runtime,
    )
    selected = {
        "title": "Chosen",
        "url": "https://example/chosen",
        "source": "OpenBiliClaw",
        "mode": "openbiliclaw",
        "_openbiliclaw_candidate": envelope,
    }
    log = SimpleNamespace(
        info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None
    )

    await delivery._acknowledge_openbiliclaw_delivery(
        selected,
        [],
        lanlan_name="Neko",
        log=log,
    )
    runtime.record_recommendation_delivery.assert_not_awaited()

    await delivery._acknowledge_openbiliclaw_delivery(
        selected,
        [{key: selected[key] for key in ("title", "url", "source", "mode")}],
        lanlan_name="Neko",
        log=log,
    )
    runtime.record_recommendation_delivery.assert_awaited_once_with(
        recommendation,
        surface="neko_proactive",
    )


def test_neko_product_paths_never_call_core_chat() -> None:
    root = Path(__file__).resolve().parents[2]
    product_files = [
        root / "app" / "openbiliclaw_runtime.py",
        *(root / "main_logic" / "proactive_chat").glob("*.py"),
    ]
    assert all(
        "core.chat(" not in path.read_text(encoding="utf-8") for path in product_files
    )
