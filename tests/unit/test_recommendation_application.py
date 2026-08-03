import asyncio
import time

import pytest

from main_logic.proactive_recommendation import application as application_module
from main_logic.proactive_recommendation.application import RecommendationApplication
from main_logic.proactive_recommendation.domain_models import RecordFeedbackCommand
from main_logic.proactive_recommendation.feedback.contracts import (
    RecommendationFeedbackRecordResult,
)
from main_logic.proactive_recommendation.turn import RecommendationTurn


@pytest.mark.asyncio
async def test_turn_create_does_not_block_event_loop(monkeypatch):
    monkeypatch.setattr(RecommendationTurn, "__post_init__", lambda self: time.sleep(0.05))

    create_task = asyncio.create_task(RecommendationTurn.create(lanlan_name="neko"))
    heartbeat = asyncio.create_task(asyncio.sleep(0.005, result="responsive"))

    assert await asyncio.wait_for(heartbeat, timeout=0.03) == "responsive"
    assert isinstance(await create_task, RecommendationTurn)


@pytest.mark.asyncio
async def test_application_records_feedback_off_event_loop(monkeypatch):
    calls = []

    def record(**kwargs):
        time.sleep(0.05)
        calls.append(kwargs)
        return RecommendationFeedbackRecordResult(event={"event_type": kwargs["event_type"]}, logged=True)

    def record_command(self, command):
        return record(
            lanlan_name=command.lanlan_name,
            turn_id=command.turn_id,
            event_type=command.event_type,
            source_type=command.source_type,
            candidate_id=command.candidate_id,
            metadata=command.metadata,
            log_mode=command.log_mode,
            config_dir=command.config_dir,
            ts=command.ts,
        )

    monkeypatch.setattr(
        application_module.FeedbackService,
        "record_event",
        record_command,
    )
    application = RecommendationApplication()
    command = RecordFeedbackCommand(
        lanlan_name="neko",
        turn_id="turn-1",
        event_type="user_reply",
    )

    record_task = asyncio.create_task(application.record_feedback(command))
    heartbeat = asyncio.create_task(asyncio.sleep(0.005, result="responsive"))

    assert await asyncio.wait_for(heartbeat, timeout=0.03) == "responsive"
    result = await record_task
    assert result.logged is True
    assert calls[0]["turn_id"] == "turn-1"
