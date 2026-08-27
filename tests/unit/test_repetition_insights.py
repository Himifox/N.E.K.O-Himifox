from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from utils.llm_client import AIMessage, SQLChatMessageHistory


def _empty_effects(days: int = 30) -> dict:
    return {
        "schema_version": "anti-repeat-effects/v1",
        "source_available": False,
        "period_days": days,
        "totals": {},
        "reason_counts": {},
        "bm25": {},
        "patterns": [],
    }


def test_sql_history_preserves_anti_repeat_link_metadata():
    history = SQLChatMessageHistory.__new__(SQLChatMessageHistory)

    serialized = history._serialize(
        AIMessage(
            content="synthetic reply",
            additional_kwargs={
                "anti_repeat_response_id": "response-1",
                "anti_repeat_visible_text_length": "15",
            },
        )
    )

    assert json.loads(serialized) == {
        "type": "ai",
        "data": {
            "content": "synthetic reply",
            "additional_kwargs": {
                "anti_repeat_response_id": "response-1",
                "anti_repeat_visible_text_length": "15",
            },
        },
    }


def test_sql_history_discards_unapproved_and_non_string_metadata():
    history = SQLChatMessageHistory.__new__(SQLChatMessageHistory)

    serialized = history._serialize(
        AIMessage(
            content="synthetic reply",
            additional_kwargs={
                "anti_repeat_response_id": object(),
                "provider_payload": object(),
                "private_note": "must not persist",
            },
        )
    )

    assert json.loads(serialized) == {
        "type": "ai",
        "data": {"content": "synthetic reply"},
    }


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
        "analyzed_message_count": 3,
        "messages_truncated": False,
        "content_truncated": False,
        "candidate_count": 1,
        "returned_candidate_count": 1,
        "candidates_truncated": False,
        "source_available": True,
    }
    assert result["parameters"]["assistant_message_limit"] == 25
    assert result["parameters"]["message_count_threshold"] == 3
    assert result["candidates"][0]["phrase"] == "quiet lantern"
    assert "context" not in result["candidates"][0]
    # No persisted response IDs -> the key must be OMITTED, not sent as an empty
    # list. An empty list still selects the message-scoped branch in
    # main_routers.memory_router, which then reports "no linked records" forever
    # instead of falling back to the day-scoped aggregate that does work.
    assert "_anti_repeat_response_ids" not in result
    time_manager.aretrieve_latest_assistant_texts.assert_awaited_once_with(
        "test_char",
        25,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_internal_repetition_insights_forwards_present_response_ids():
    """The join key still travels when the persisted history actually carries it."""
    from app.memory_server import routes

    history = SimpleNamespace(
        messages=["quiet lantern", "quiet lantern", "quiet lantern"],
        source_available=True,
        skipped_row_count=0,
        # Positionally aligned with `messages`: one entry per reply.
        response_ids=["turn-a", "turn-b", "turn-c"],
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

    assert result["_anti_repeat_response_ids"] == ["turn-a", "turn-b", "turn-c"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("character_name", ["a" * 60, "chat"])
async def test_internal_repetition_insights_accepts_existing_query_names(character_name):
    from app.memory_server import routes

    history = SimpleNamespace(
        messages=[],
        source_available=True,
        skipped_row_count=0,
    )
    time_manager = SimpleNamespace(
        aretrieve_latest_assistant_texts=AsyncMock(return_value=history)
    )

    with patch.object(routes.runtime, "time_manager", time_manager):
        result = await routes.repetition_insights(
            character_name,
            routes.RepetitionInsightsRequest(language="en"),
        )

    assert result["success"] is True
    assert result["character_name"] == character_name
    time_manager.aretrieve_latest_assistant_texts.assert_awaited_once_with(
        character_name,
        100,
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
@pytest.mark.parametrize("character_name", ["legacy.name", "chat"])
async def test_public_repetition_insights_validates_and_forwards_local_request(
    character_name,
):
    from main_routers import memory_router
    from memory import anti_repeat_effects
    from utils import config_manager, internal_http_client

    response_payload = {
        "success": True,
        "schema_version": "natural-expression-candidates/v1",
        "artifact_type": "user_review_candidates",
        "candidates": [],
    }
    response = SimpleNamespace(
        status_code=200,
        json=lambda: dict(response_payload),
    )
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    effect_store = SimpleNamespace(
        query_effects=MagicMock(return_value=_empty_effects())
    )
    config = SimpleNamespace(
        aload_characters=AsyncMock(return_value={"猫娘": {character_name: {}}})
    )

    with (
        patch.object(config_manager, "get_config_manager", return_value=config),
        patch.object(
            internal_http_client,
            "get_internal_http_client",
            return_value=client,
        ),
        patch.object(
            anti_repeat_effects,
            "get_anti_repeat_effect_store",
            return_value=effect_store,
        ),
    ):
        result = await memory_router.repetition_insights(
            memory_router.RepetitionInsightsRequest(
                character_name=character_name,
                language="zh-CN",
                assistant_message_limit=50,
            )
        )

    assert result["success"] is True
    assert result["artifact_type"] == "user_review_candidates"
    assert result["effectiveness"] == _empty_effects()
    assert result["associations"] == []
    call = client.post.await_args
    assert call.kwargs["json"] == {
        "language": "zh-CN",
        "assistant_message_limit": 50,
    }
    assert call.kwargs["timeout"] == 30.0
    assert call.args[0].startswith("http://127.0.0.1:")
    assert call.args[0].endswith(f"/{character_name}/repetition_insights")
    effect_store.query_effects.assert_called_once_with(character_name, 30)


@pytest.mark.unit
def test_repetition_insight_effect_days_are_limited_to_supported_windows():
    from main_routers import memory_router

    with pytest.raises(ValidationError):
        memory_router.RepetitionInsightsRequest(
            character_name="test_char",
            language="en",
            effect_days=14,
        )


@pytest.mark.unit
def test_repetition_effect_associations_are_exact_or_safe_containment_only():
    from main_routers import memory_router

    candidates = [
        {
            "normalized_phrase": "quiet lantern",
            "language": "en",
            "occurrence_count": 4,
            "message_count": 3,
        },
        {
            "normalized_phrase": "好久不见",
            "language": "zh-CN",
            "occurrence_count": 3,
            "message_count": 3,
        },
        {
            "normalized_phrase": "hello there",
            "language": "en",
            "occurrence_count": 3,
            "message_count": 3,
        },
    ]
    patterns = [
        {
            "normalized_phrase": "quiet lantern",
            "language": "en",
            "detected_count": 6,
            "regen_triggered_count": 4,
            "regen_guard_passed_count": 3,
            "blocked_count": 1,
        },
        {
            "normalized_phrase": "今天好久不见呀",
            "language": "zh",
            "detected_count": 2,
        },
        {
            "normalized_phrase": "好久不见",
            "language": "zh-TW",
            "detected_count": 100,
        },
        {
            "normalized_phrase": "hello their",
            "language": "en",
            "detected_count": 99,
        },
    ]

    result = memory_router._associate_repetition_effects(candidates, patterns)

    assert [item["association_type"] for item in result] == ["exact", "contained"]
    assert result[0] == {
        "normalized_phrase": "quiet lantern",
        "language": "en",
        "effect_normalized_phrase": "quiet lantern",
        "association_type": "exact",
        "detected_count": 6,
        "regen_triggered_count": 4,
        "regen_guard_passed_count": 3,
        "blocked_count": 1,
        "residual_occurrence_count": 4,
        "residual_message_count": 3,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("language", "candidate_phrase", "rejected_phrase", "contained_phrase"),
    [
        ("en", "he said", "she said", "well he said today"),
        ("es", "la casa", "mala casa", "visité la casa hoy"),
        ("pt", "a casa", "na casa", "vi a casa hoje"),
        ("ru", "он сказал", "слон сказал", "вчера он сказал правду"),
    ],
)
def test_word_language_associations_require_contiguous_token_boundaries(
    language,
    candidate_phrase,
    rejected_phrase,
    contained_phrase,
):
    from main_routers import memory_router

    candidates = [
        {
            "normalized_phrase": candidate_phrase,
            "language": language,
            "occurrence_count": 3,
            "message_count": 3,
        }
    ]
    patterns = [
        {
            "normalized_phrase": rejected_phrase,
            "language": language,
            "detected_count": 99,
        },
        {
            "normalized_phrase": contained_phrase,
            "language": language,
            "detected_count": 2,
        },
    ]

    result = memory_router._associate_repetition_effects(candidates, patterns)

    assert len(result) == 1
    assert result[0]["effect_normalized_phrase"] == contained_phrase
    assert result[0]["association_type"] == "contained"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("language", "candidate_phrase", "effect_phrase"),
    [
        ("en", "quiet lantern", "quiet"),
        ("zh-CN", "一直陪着", "陪着"),
    ],
)
def test_associations_accept_actual_runtime_detector_signature_sizes(
    language,
    candidate_phrase,
    effect_phrase,
):
    from main_routers import memory_router

    candidates = [
        {
            "normalized_phrase": candidate_phrase,
            "language": language,
            "occurrence_count": 3,
            "message_count": 3,
        }
    ]
    patterns = [
        {
            "normalized_phrase": effect_phrase,
            "language": language,
            "reasons": {"bm25": 1},
            "detected_count": 1,
        }
    ]

    result = memory_router._associate_repetition_effects(candidates, patterns)

    assert len(result) == 1
    assert result[0]["effect_normalized_phrase"] == effect_phrase
    assert result[0]["association_type"] == "contained"


@pytest.mark.unit
def test_korean_associations_use_word_boundaries_without_losing_character_ngrams():
    from main_routers import memory_router

    candidates = [
        {
            "normalized_phrase": "나는 정말",
            "language": "ko",
            "occurrence_count": 3,
            "message_count": 3,
        },
        {
            "normalized_phrase": "두근두근",
            "language": "ko",
            "occurrence_count": 4,
            "message_count": 3,
        },
    ]
    patterns = [
        {
            "normalized_phrase": "신나는 정말",
            "language": "ko",
            "detected_count": 99,
        },
        {
            "normalized_phrase": "어제 나는 정말 웃었어",
            "language": "ko",
            "detected_count": 2,
        },
        {
            "normalized_phrase": "오늘도 두근두근 설레",
            "language": "ko",
            "detected_count": 3,
        },
    ]

    result = memory_router._associate_repetition_effects(candidates, patterns)

    assert [item["effect_normalized_phrase"] for item in result] == [
        "어제 나는 정말 웃었어",
        "오늘도 두근두근 설레",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_repetition_insights_keeps_residuals_when_effect_query_fails():
    from main_routers import memory_router
    from memory import anti_repeat_effects
    from utils import config_manager, internal_http_client

    client = SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                json=lambda: {"success": True, "candidates": []},
            )
        )
    )
    config = SimpleNamespace(aload_characters=AsyncMock(return_value={}))
    effect_store = SimpleNamespace(
        query_effects=MagicMock(side_effect=OSError("private path"))
    )

    with (
        patch.object(config_manager, "get_config_manager", return_value=config),
        patch.object(
            internal_http_client,
            "get_internal_http_client",
            return_value=client,
        ),
        patch.object(
            anti_repeat_effects,
            "get_anti_repeat_effect_store",
            return_value=effect_store,
        ),
        patch.object(memory_router, "character_memory_exists", return_value=True),
    ):
        result = await memory_router.repetition_insights(
            memory_router.RepetitionInsightsRequest(
                character_name="test_char",
                language="en",
                effect_days=7,
            )
        )

    assert result["success"] is True
    assert result["effectiveness"]["source_available"] is False
    assert result["effectiveness"]["period_days"] == 7
    assert "private path" not in json.dumps(result)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("character_name", ["legacy.name", "chat"])
async def test_reset_repetition_effects_clears_only_selected_character(character_name):
    from main_routers import memory_router
    from memory import anti_repeat_effects
    from utils import config_manager

    config = SimpleNamespace(aload_characters=AsyncMock(return_value={}))
    effect_store = SimpleNamespace(clear_effects=MagicMock())

    with (
        patch.object(config_manager, "get_config_manager", return_value=config),
        patch.object(
            anti_repeat_effects,
            "get_anti_repeat_effect_store",
            return_value=effect_store,
        ),
        patch.object(memory_router, "character_memory_exists", return_value=True),
    ):
        result = await memory_router.reset_repetition_effects(
            memory_router.RepetitionEffectsResetRequest(character_name=character_name)
        )

    assert result == {
        "success": True,
        "character_name": character_name,
        "cleared": True,
    }
    effect_store.clear_effects.assert_called_once_with(character_name)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repetition_endpoints_still_reject_dot_traversal_names():
    from main_routers import memory_router

    insights = await memory_router.repetition_insights(
        memory_router.RepetitionInsightsRequest(
            character_name="../escape",
            language="en",
        )
    )
    reset = await memory_router.reset_repetition_effects(
        memory_router.RepetitionEffectsResetRequest(character_name="../escape")
    )

    assert insights.status_code == 422
    assert reset.status_code == 422


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


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_ids, expected",
    [
        # Every analyzed reply linkable -> message scope is honest.
        (["turn-a", "turn-b", "turn-c"], ["turn-a", "turn-b", "turn-c"]),
        # A legacy row without the key mixed in -> the aggregate would cover
        # only part of the window, so fall back to day scope.
        (["turn-a", None, "turn-c"], None),
        (["turn-a", None, None], None),
        ([None, None, None], None),
    ],
)
async def test_partial_response_id_coverage_falls_back_to_day_scope(
    response_ids, expected
):
    from app.memory_server import routes

    history = SimpleNamespace(
        messages=["quiet lantern", "quiet lantern", "quiet lantern"],
        source_available=True,
        skipped_row_count=0,
        response_ids=response_ids,
    )
    time_manager = SimpleNamespace(
        aretrieve_latest_assistant_texts=AsyncMock(return_value=history)
    )

    with patch.object(routes.runtime, "time_manager", time_manager):
        result = await routes.repetition_insights(
            "test_char",
            routes.RepetitionInsightsRequest(language="en", assistant_message_limit=25),
        )

    assert result.get("_anti_repeat_response_ids") == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_ids_are_sliced_to_the_analyzed_window(monkeypatch):
    """The budget narrows the window newest-first; the IDs must follow it.

    Emitting IDs for replies the report never analyzed would let the panel label
    an out-of-window aggregate as handling for the latest requested replies.
    """
    from app.memory_server import routes
    from utils import natural_expression_candidates as candidate_core

    # One such reply mines to 145 occurrences, so a 200 budget keeps exactly one.
    monkeypatch.setattr(candidate_core, "USER_REVIEW_MAX_OCCURRENCES", 200)
    unbroken = "今天天气真好我们一起去散步你觉得怎么样我觉得非常开心因为可以和你聊天"
    history = SimpleNamespace(
        messages=[unbroken] * 4,
        source_available=True,
        skipped_row_count=0,
        response_ids=["oldest", "older", "newer", "newest"],
    )
    time_manager = SimpleNamespace(
        aretrieve_latest_assistant_texts=AsyncMock(return_value=history)
    )

    with patch.object(routes.runtime, "time_manager", time_manager):
        result = await routes.repetition_insights(
            "test_char",
            routes.RepetitionInsightsRequest(
                language="zh-CN", assistant_message_limit=25
            ),
        )

    analyzed = result["summary"]["analyzed_message_count"]
    assert result["summary"]["messages_truncated"] is True
    assert 0 < analyzed < 4
    # Exactly the newest `analyzed` ids, never the dropped older ones.
    assert result["_anti_repeat_response_ids"] == ["oldest", "older", "newer", "newest"][-analyzed:]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_effect_scope_uses_the_analyzed_count_not_the_request(monkeypatch):
    """A narrowed window must relabel the effect scope.

    Otherwise the panel says "the latest 100 replies" over an aggregate that
    covers however few the budget actually allowed.
    """
    from main_routers import memory_router

    captured = {}

    class _Store:
        def query_effects_for_responses(self, name, ids, limit, **_kwargs):
            captured["limit"] = limit
            return {
                "schema_version": "anti-repeat-effects/v1",
                "source_available": True,
                "started_at": 0.0,
                "scope_type": "assistant_messages",
                "assistant_message_limit": limit,
                "linked_message_count": len(list(ids)),
                "totals": {},
                "reason_counts": {},
                "bm25": {},
                "patterns": [],
            }

    monkeypatch.setattr(
        "memory.anti_repeat_effects.get_anti_repeat_effect_store", lambda: _Store()
    )

    inner = {
        "success": True,
        "summary": {"assistant_message_count": 100, "analyzed_message_count": 10},
        "candidates": [],
        "parameters": {},
        "_anti_repeat_response_ids": ["a", "b"],
    }

    class _Response:
        status_code = 200

        def json(self):
            return dict(inner)

    monkeypatch.setattr(
        memory_router,
        "character_memory_exists",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "utils.internal_http_client.get_internal_http_client",
        lambda: SimpleNamespace(post=AsyncMock(return_value=_Response())),
    )
    monkeypatch.setattr(
        "utils.config_manager.get_config_manager",
        lambda: SimpleNamespace(aload_characters=AsyncMock(return_value={"猫娘": {}})),
    )

    result = await memory_router.repetition_insights(
        memory_router.RepetitionInsightsRequest(
            character_name="测试猫娘", language="en", assistant_message_limit=100
        )
    )

    assert captured["limit"] == 10
    assert result["effectiveness"]["assistant_message_limit"] == 10


@pytest.mark.asyncio
async def test_insight_characters_lists_history_without_a_recent_file(tmp_path):
    """The selector must offer every identity the analysis route accepts.

    Built from the recent-memory file list, it omitted a character that has
    time-indexed history but no ``recent.json`` -- the shape a cloud-save import
    produces when the profile ships no recent file -- even though the analysis
    route explicitly supports it.
    """
    from main_routers import memory_router
    from utils import config_manager

    memory_dir = tmp_path / "memory"
    (memory_dir / "OnlyHistory").mkdir(parents=True)
    (memory_dir / "OnlyHistory" / "time_indexed.db").write_bytes(b"")
    (memory_dir / "HasRecent").mkdir()
    (memory_dir / "HasRecent" / "recent.json").write_text("[]", encoding="utf-8")
    (memory_dir / "Empty").mkdir()

    config = SimpleNamespace(
        aload_characters=AsyncMock(return_value={"猫娘": {"Configured": {}}}),
        memory_dir=str(memory_dir),
        project_memory_dir=str(tmp_path / "absent"),
    )
    with patch.object(config_manager, "get_config_manager", return_value=config):
        result = await memory_router.get_insight_characters()

    assert "OnlyHistory" in result["characters"], (
        "a character with time-indexed history but no recent.json was omitted"
    )
    assert "HasRecent" in result["characters"]
    assert "Configured" in result["characters"]
    assert "Absent" not in result["characters"]

    # Anti-drift: membership must be exactly the analysis route's admission
    # rule, so the panel can never offer a name the route rejects nor hide one
    # it accepts. A bare directory qualifies under that rule -- the route serves
    # it and reports an empty analysis -- so it belongs in the list too.
    from utils.character_memory import character_memory_exists

    with patch.object(config_manager, "get_config_manager", return_value=config):
        for name in ("OnlyHistory", "HasRecent", "Empty", "Absent"):
            admitted = name == "Configured" or character_memory_exists(
                config, name
            )
            assert (name in result["characters"]) is admitted, name
