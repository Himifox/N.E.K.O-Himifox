from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from knowledge.service import KnowledgeTurnContext
from scripts import evaluate_knowledge_response_quality as evaluator


@pytest.mark.asyncio
async def test_route_preflight_uses_production_context_builder(monkeypatch, tmp_path):
    service = object()
    builder = AsyncMock(
        return_value=KnowledgeTurnContext(
            text="Knowledge term: fixture",
            hit_count=1,
            match_mode="automatic_hybrid",
        )
    )
    monkeypatch.setattr(
        evaluator.KnowledgeService,
        "for_database",
        lambda _database: service,
    )
    monkeypatch.setattr(evaluator, "_build_production_context", builder)

    results = await evaluator._route_preflight(
        [{"message": "semantic fixture", "expected_mode": "strong"}],
        tmp_path / "knowledge.db",
    )

    builder.assert_awaited_once_with(service, "semantic fixture")
    assert results[0]["route_pass"] is True
    assert results[0]["production_match_mode"] == "automatic_hybrid"


@pytest.mark.asyncio
async def test_live_receiver_waits_for_explicit_turn_end():
    class SlowWebSocket:
        def __init__(self):
            self.messages = iter(
                (
                    {"type": "gemini_response", "text": "first"},
                    {"type": "system", "data": "working"},
                    {"type": "gemini_response", "text": " second"},
                    {"type": "system", "data": "turn end"},
                )
            )

        async def recv(self):
            await asyncio.sleep(0.01)
            return json.dumps(next(self.messages))

    outcome = await evaluator._receive_until_complete(SlowWebSocket())

    assert outcome["completed"] is True
    assert outcome["reply"] == "first second"
