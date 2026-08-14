from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException


@pytest.mark.unit
@pytest.mark.asyncio
async def test_internal_repetition_insights_returns_review_only_candidates():
    from app.memory_server import routes

    history = SimpleNamespace(
        messages=["quiet lantern", "quiet lantern", "quiet lantern"],
        source_available=True,
        skipped_row_count=2,
    )
    time_manager = SimpleNamespace(
        aretrieve_latest_assistant_texts=AsyncMock(return_value=history)
    )

    with patch.object(routes.runtime, "time_manager", time_manager):
        result = await routes.repetition_insights(
            "test_char",
            routes.RepetitionInsightsRequest(
                language="en",
                assistant_message_limit=25,
            ),
        )

    assert result["success"] is True
    assert result["artifact_type"] == "user_review_candidates"
    assert result["summary"] == {
        "assistant_message_count": 3,
        "candidate_count": 1,
        "source_available": True,
    }
    assert result["parameters"]["assistant_message_limit"] == 25
    assert result["parameters"]["message_count_threshold"] == 3
    assert result["candidates"][0]["phrase"] == "quiet lantern"
    assert "context" not in result["candidates"][0]
    time_manager.aretrieve_latest_assistant_texts.assert_awaited_once_with(
        "test_char",
        25,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_internal_repetition_insights_requires_initialized_time_manager():
    from app.memory_server import routes

    with patch.object(routes.runtime, "time_manager", None):
        with pytest.raises(HTTPException) as exc_info:
            await routes.repetition_insights(
                "test_char",
                routes.RepetitionInsightsRequest(language="en"),
            )

    assert exc_info.value.status_code == 503


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_repetition_insights_validates_and_forwards_local_request():
    from main_routers import memory_router
    from utils import config_manager, internal_http_client

    response_payload = {
        "success": True,
        "schema_version": "natural-expression-candidates/v1",
        "artifact_type": "user_review_candidates",
        "candidates": [],
    }
    response = SimpleNamespace(
        status_code=200,
        json=lambda: response_payload,
    )
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    config = SimpleNamespace(
        aload_characters=AsyncMock(return_value={"猫娘": {"test_char": {}}})
    )

    with (
        patch.object(config_manager, "get_config_manager", return_value=config),
        patch.object(
            internal_http_client,
            "get_internal_http_client",
            return_value=client,
        ),
    ):
        result = await memory_router.repetition_insights(
            memory_router.RepetitionInsightsRequest(
                character_name="test_char",
                language="zh-CN",
                assistant_message_limit=50,
            )
        )

    assert result == response_payload
    call = client.post.await_args
    assert call.kwargs["json"] == {
        "language": "zh-CN",
        "assistant_message_limit": 50,
    }
    assert call.kwargs["timeout"] == 30.0
    assert call.args[0].startswith("http://127.0.0.1:")
    assert call.args[0].endswith("/test_char/repetition_insights")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_repetition_insights_returns_sanitized_unavailable_error():
    from main_routers import memory_router
    from utils import config_manager, internal_http_client

    client = SimpleNamespace(
        post=AsyncMock(side_effect=RuntimeError("private upstream detail"))
    )
    config = SimpleNamespace(
        aload_characters=AsyncMock(return_value={"猫娘": {"test_char": {}}})
    )

    with (
        patch.object(config_manager, "get_config_manager", return_value=config),
        patch.object(
            internal_http_client,
            "get_internal_http_client",
            return_value=client,
        ),
    ):
        response = await memory_router.repetition_insights(
            memory_router.RepetitionInsightsRequest(
                character_name="test_char",
                language="en",
            )
        )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload == {
        "success": False,
        "error": "local memory analysis unavailable",
    }
    assert "private upstream detail" not in response.body.decode("utf-8")
