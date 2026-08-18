# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import openbiliclaw_runtime
from main_logic.proactive_chat import candidate_selection, delivery, sources
from main_logic.proactive_chat.contracts import ProactiveChatCommand


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


async def test_openbiliclaw_source_is_structured_and_preview_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommendation = SimpleNamespace(
        content=SimpleNamespace(
            title="A useful video",
            content_url="https://example/video",
            source_platform="bilibili",
            author_name="Creator",
            up_name="",
            item_key="bilibili:BV1",
        ),
        expression="Matches your current interest",
        topic_label="Systems thinking",
        confidence=0.91,
    )
    runtime = SimpleNamespace(
        proactive_available=True,
        preview_recommendations=AsyncMock(return_value=[recommendation]),
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
    runtime.preview_recommendations.assert_awaited_once_with(limit=3)
    assert payload["links"][0] == {
        "title": "A useful video",
        "url": "https://example/video",
        "source": "OpenBiliClaw",
        "mode": "openbiliclaw",
        "platform": "bilibili",
        "author": "Creator",
        "reason": "Matches your current interest",
        "topic_label": "Systems thinking",
        "confidence": 0.91,
        "item_key": "bilibili:BV1",
        "_openbiliclaw_recommendation": recommendation,
    }


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
    assert len(selected["personal"]) + len(selected["news"]) == 4


async def test_delivery_acknowledges_only_the_committed_openbiliclaw_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommendation = object()
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
        "_openbiliclaw_recommendation": recommendation,
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
