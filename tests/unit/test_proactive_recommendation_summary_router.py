import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main_routers.proactive_router as proactive_router
from config import AUTOSTART_CSRF_TOKEN
from main_logic.proactive_recommendation import PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION
from main_logic.proactive_recommendation_feedback import (
    FEEDBACK_LOG_FILENAME,
    append_recommendation_feedback_jsonl,
    build_feedback_event,
)
from main_logic.proactive_recommendation_observer import (
    CALIBRATION_SAMPLE_LIMIT,
    CALIBRATION_WINDOW_SECONDS,
    OBSERVATION_LOG_FILENAME,
    append_recommendation_observation_jsonl,
)
from main_logic.proactive_recommendation_tuning import (
    TUNING_FILENAME,
    save_recommendation_tuning,
)


class _ConfigManagerStub:
    def __init__(self, config_dir):
        self.config_dir = config_dir

    async def aget_character_data(self):
        return (None, "neko", None, None, None, None, None, None, None)


def _observation(**overrides):
    base = {
        "ts": time.time(),
        "lanlan_name": "neko",
        "turn_id": "turn-1",
        "algorithm_version": PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
        "decision_stage": "phase1_material",
        "candidate_count": 2,
        "shadow_selected_source_type": "music",
        "shadow_selected_candidate_id": "music:1",
        "shadow_selected_score": 0.82,
        "top_candidates": [
            {
                "rank": 1,
                "id": "music:1",
                "source_type": "music",
                "family": "music",
                "topic": "Kitchen Song",
                "score": 0.82,
            }
        ],
        "actual_primary_channel": "music",
        "actual_source_tag": "MUSIC",
        "actual_reason_code": "CHAT_DELIVERED",
        "actual_stage": "delivery",
        "active_channels": ["music"],
        "delivered": True,
        "actual_rank": 1,
        "actual_candidate_score": 0.82,
        "matched_actual_material": True,
        "matched_actual_source": True,
    }
    base.update(overrides)
    return base


def _client(monkeypatch, tmp_path, *, log_mode="jsonl", tuning_mode="off", now=10_000.0):
    monkeypatch.setattr(
        proactive_router,
        "get_config_manager",
        lambda: _ConfigManagerStub(tmp_path),
    )
    monkeypatch.setattr(
        proactive_router,
        "PROACTIVE_RECOMMENDATION_OBSERVATION_LOG",
        log_mode,
    )
    monkeypatch.setattr(
        proactive_router,
        "PROACTIVE_RECOMMENDATION_FEEDBACK_LOG",
        log_mode,
    )
    monkeypatch.setattr(
        proactive_router,
        "PROACTIVE_RECOMMENDATION_TUNING_MODE",
        tuning_mode,
    )
    monkeypatch.setattr(
        proactive_router,
        "get_recommendation_runtime_status",
        lambda: {
            "configured_mode": "shadow",
            "effective_mode": "shadow",
            "active_source_enabled": False,
            "activation_source": "startup_environment_only",
            "runtime_activation_allowed": False,
            "rollback_available": False,
            "rollback_target": "shadow",
            "rollback_count": 0,
            "last_rollback_at": None,
            "last_rollback_reason": None,
            "restart_restores_configured_mode": False,
        },
    )
    monkeypatch.setattr(proactive_router.time, "time", lambda: now)
    app = FastAPI()
    app.include_router(proactive_router.router)
    return TestClient(app)


def _append(tmp_path, observation):
    append_recommendation_observation_jsonl(
        observation,
        log_mode="jsonl",
        path=tmp_path / OBSERVATION_LOG_FILENAME,
    )


def _append_feedback(tmp_path, event):
    append_recommendation_feedback_jsonl(
        event,
        log_mode="jsonl",
        path=tmp_path / FEEDBACK_LOG_FILENAME,
    )


def test_recommendation_summary_returns_missing_when_jsonl_absent(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, log_mode="off")

    response = client.get("/api/proactive/recommendation/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["missing"] is True
    assert payload["log_enabled"] is False
    assert payload["summary"]["total"] == 0
    assert payload["calibration"]["sample_window_seconds"] == CALIBRATION_WINDOW_SECONDS
    assert payload["calibration"]["sample_limit"] == CALIBRATION_SAMPLE_LIMIT
    assert payload["validation"]["sample_count"] == 0
    assert payload["validation"]["issues"] == []
    assert payload["feedback_calibration"]["sample_count"] == 0
    assert payload["feedback_calibration"]["feedback_joined_count"] == 0
    assert payload["runtime"]["effective_mode"] == "shadow"
    assert payload["runtime"]["runtime_activation_allowed"] is False
    assert payload["sample_count"] == 0
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_recommendation_runtime_status_and_rollback_contract(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proactive_router,
        "rollback_recommendation_runtime",
        lambda *, reason: {
            "applied": True,
            "previous_mode": "active_source",
            "status": {
                "configured_mode": "active_source",
                "effective_mode": "shadow",
                "active_source_enabled": False,
                "runtime_activation_allowed": False,
                "rollback_count": 1,
                "last_rollback_reason": reason,
            },
        },
    )

    read = client.get("/api/proactive/recommendation/runtime")
    denied = client.post("/api/proactive/recommendation/runtime/rollback", json={})
    accepted = client.post(
        "/api/proactive/recommendation/runtime/rollback",
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": AUTOSTART_CSRF_TOKEN,
        },
        json={"reason": "unit_test"},
    )

    assert read.status_code == 200
    assert read.json()["runtime"]["effective_mode"] == "shadow"
    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["applied"] is True
    assert accepted.json()["status"]["effective_mode"] == "shadow"
    assert accepted.json()["status"]["last_rollback_reason"] == "unit_test"


def test_recommendation_summary_uses_recent_hour_window_and_ignores_limit(monkeypatch, tmp_path):
    now = 10_000.0
    _append(tmp_path, _observation(turn_id="too-old", ts=now - CALIBRATION_WINDOW_SECONDS - 1))
    _append(tmp_path, _observation(turn_id="match", ts=now - 3))
    _append(
        tmp_path,
        _observation(
            turn_id="mismatch",
            ts=now - 2,
            shadow_selected_source_type="meme",
            shadow_selected_score=0.44,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "meme:1",
                    "source_type": "meme",
                    "family": "meme",
                    "topic": "meme",
                    "score": 0.44,
                }
            ],
            matched_actual_source=False,
            matched_actual_material=False,
            actual_rank=3,
        ),
    )
    _append(
        tmp_path,
        _observation(
            turn_id="pass-high-score",
            ts=now - 1,
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            actual_rank=None,
            shadow_selected_score=0.91,
        ),
    )
    client = _client(monkeypatch, tmp_path, tuning_mode="auto_safe", now=now)

    response = client.get(
        "/api/proactive/recommendation/summary",
        params={"limit": 1, "high_score_threshold": 0.75},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["missing"] is False
    assert payload["log_enabled"] is True
    assert payload["sample_count"] == 3
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["pass_high_score_count"] == 1
    assert payload["summary"]["shadow_top1_by_source_type"] == {"meme": 1, "music": 2}
    assert payload["validation"]["sample_count"] == 3
    assert "pass_conflict" in payload["validation"]["issues"]
    assert payload["retention"]["requested_limit_ignored"] is True
    assert "examples" not in payload


def test_recommendation_summary_examples_are_sanitized_and_prioritized(monkeypatch, tmp_path):
    now = 10_000.0
    _append(tmp_path, _observation(turn_id="stale-mismatch", ts=now - CALIBRATION_WINDOW_SECONDS - 1, matched_actual_material=False))
    _append(tmp_path, _observation(turn_id="match", ts=now - 3))
    _append(
        tmp_path,
        _observation(
            turn_id="pass-high-score",
            ts=now - 2,
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            shadow_selected_score=0.91,
            source_links=[{"url": "must-not-leak"}],
            raw_data={"secret": "must-not-leak"},
            screenshot_b64="must-not-leak",
        ),
    )
    _append(
        tmp_path,
        _observation(
            turn_id="material-mismatch",
            ts=now - 1,
            matched_actual_material=False,
            actual_rank=2,
            payload={"secret": "must-not-leak"},
        ),
    )
    client = _client(monkeypatch, tmp_path, now=now)

    response = client.get(
        "/api/proactive/recommendation/summary",
        params={"include_examples": "true"},
    )

    assert response.status_code == 200
    payload = response.json()
    examples = payload["examples"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert [item["turn_id"] for item in examples[:2]] == [
        "material-mismatch",
        "pass-high-score",
    ]
    assert "stale-mismatch" not in dumped
    assert examples[1]["reason_code"] == "PASS_MODEL_PASS"
    assert "actual_reason_code" not in examples[1]
    assert set(examples[0]) == {
        "turn_id",
        "ts",
        "decision_stage",
        "shadow_selected_source_type",
        "actual_primary_channel",
        "actual_rank",
        "reason_code",
        "top_candidates",
    }
    assert "payload" not in dumped
    assert "source_links" not in dumped
    assert "raw_data" not in dumped
    assert "screenshot_b64" not in dumped
    assert "must-not-leak" not in dumped


def test_recommendation_summary_uses_fixed_sample_limit_without_returning_path(monkeypatch, tmp_path):
    now = 10_000.0
    for idx in range(60):
        _append(tmp_path, _observation(turn_id=f"row-{idx}", ts=now - 60 + idx))
    client = _client(monkeypatch, tmp_path, now=now)

    response = client.get(
        "/api/proactive/recommendation/summary",
        params={"limit": 999999, "high_score_threshold": 9.0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sample_count"] == CALIBRATION_SAMPLE_LIMIT
    assert payload["summary"]["total"] == CALIBRATION_SAMPLE_LIMIT
    assert payload["retention"]["sample_window_seconds"] == CALIBRATION_WINDOW_SECONDS
    assert payload["retention"]["sample_limit"] == CALIBRATION_SAMPLE_LIMIT
    assert payload["retention"]["requested_limit_ignored"] is True
    assert payload["retention"]["high_score_threshold"] == 1.0
    assert payload["retention"]["filename"] == OBSERVATION_LOG_FILENAME
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_recommendation_summary_returns_validation_without_sensitive_fields(monkeypatch, tmp_path):
    now = 10_000.0
    _append(
        tmp_path,
        _observation(
            turn_id="source-drift",
            ts=now - 2,
            shadow_selected_source_type="meme",
            shadow_selected_score=0.91,
            matched_actual_source=False,
            matched_actual_material=False,
            actual_rank=3,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "meme:1",
                    "source_type": "meme",
                    "family": "meme",
                    "topic": "meme topic",
                    "score": 0.91,
                    "payload": {"secret": "must-not-leak"},
                }
            ],
            source_links=[{"url": "must-not-leak"}],
            raw_data={"secret": "must-not-leak"},
        ),
    )
    _append(
        tmp_path,
        _observation(
            turn_id="pass-conflict",
            ts=now - 1,
            delivered=False,
            actual_reason_code="PASS_MODEL_PASS",
            actual_rank=None,
            shadow_selected_score=0.92,
            screenshot_b64="must-not-leak",
        ),
    )
    client = _client(monkeypatch, tmp_path, now=now)

    response = client.get("/api/proactive/recommendation/summary")

    assert response.status_code == 200
    payload = response.json()
    validation = payload["validation"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert validation["sample_count"] == 2
    assert "source_drift" in validation["issues"]
    assert "pass_conflict" in validation["issues"]
    assert validation["examples"]["source_drift"][0]["turn_id"] == "source-drift"
    assert "payload" not in dumped
    assert "source_links" not in dumped
    assert "raw_data" not in dumped
    assert "screenshot_b64" not in dumped
    assert "must-not-leak" not in dumped


def test_recommendation_summary_returns_feedback_metrics(monkeypatch, tmp_path):
    now = 10_000.0
    _append(tmp_path, _observation(turn_id="music-positive", ts=now - 10, actual_primary_channel="music"))
    _append(
        tmp_path,
        _observation(
            turn_id="meme-ignored",
            ts=now - 700,
            shadow_selected_source_type="meme",
            shadow_selected_score=0.82,
            top_candidates=[
                {
                    "rank": 1,
                    "id": "meme:1",
                    "source_type": "meme",
                    "family": "meme",
                    "topic": "meme",
                    "score": 0.82,
                }
            ],
            actual_primary_channel="meme",
        ),
    )
    _append_feedback(
        tmp_path,
        build_feedback_event(
            lanlan_name="neko",
            turn_id="music-positive",
            event_type="music_played_through",
            source_type="music",
            ts=now - 1,
        ),
    )
    client = _client(monkeypatch, tmp_path, tuning_mode="auto_safe", now=now)

    response = client.get("/api/proactive/recommendation/summary")

    assert response.status_code == 200
    payload = response.json()
    feedback = payload["feedback"]
    assert feedback["feedback_sample_count"] == 2
    assert feedback["event_type_distribution"] == {
        "ignored": 1,
        "music_played_through": 1,
    }
    assert feedback["score_by_source_type"]["music"] == 0.9
    assert feedback["score_by_source_type"]["meme"] == -0.05
    feedback_calibration = payload["feedback_calibration"]
    assert feedback_calibration["sample_count"] == 2
    assert feedback_calibration["feedback_joined_count"] == 1
    assert feedback_calibration["feedback_inferred_count"] == 1
    assert feedback_calibration["feedback_scored_count"] == 2
    assert feedback_calibration["score_bucket_feedback"]["high"]["average_feedback_score"] == 0.425
    assert feedback_calibration["top1_positive_rate"] == 0.5
    assert feedback_calibration["top1_negative_rate"] == 0.5
    assert feedback_calibration["feedback_signal_summary"]["music"]["played_through_count"] == 1
    assert feedback_calibration["feedback_signal_summary"]["meme"]["ignored_count"] == 1
    assert feedback_calibration["source_feedback_pressure"]["meme"]["level"] == "weak_ignored_pressure"
    assert feedback_calibration["feedback_actionable_suggestions"]["meme"] == {
        "adjustment": 0.0,
        "reasons": ["weak_ignored_pressure"],
        "confidence": "low",
    }
    assert payload["manual_tuning_preview"] == feedback_calibration["manual_tuning_preview"]
    assert payload["tuning"]["auto_apply_count"] == 0
    assert payload["retention"]["tuning_mode"] == "auto_safe"
    assert not (tmp_path / TUNING_FILENAME).exists()
    assert feedback_calibration["active_ready_by_feedback"] is False


def test_recommendation_summary_returns_tuning_without_path(monkeypatch, tmp_path):
    save_recommendation_tuning(
        {
            "enabled": True,
            "mode": "auto_safe",
            "source_type_adjustment": {"news": -0.02},
            "created_from": "feedback_calibration",
            "sample_count": 50,
            "auto_apply_count": 1,
            "last_auto_apply": {
                "applied": True,
                "adjustments": {"news": -0.02},
                "reasons": ["over_scored_high_score_low_feedback"],
            },
        },
        config_dir=tmp_path,
    )
    client = _client(monkeypatch, tmp_path, tuning_mode="auto_safe", now=10_000.0)

    response = client.get("/api/proactive/recommendation/summary")

    assert response.status_code == 200
    payload = response.json()
    dumped = json.dumps(payload, ensure_ascii=False)
    assert payload["tuning"]["enabled"] is True
    assert payload["tuning"]["source_type_adjustment"] == {"news": -0.02}
    assert payload["tuning"]["health"]["status"] == "healthy"
    assert payload["retention"]["tuning_filename"] == TUNING_FILENAME
    assert payload["retention"]["tuning_mode"] == "auto_safe"
    assert str(tmp_path) not in dumped


def test_recommendation_tuning_get_and_reset_require_csrf(monkeypatch, tmp_path):
    save_recommendation_tuning(
        {
            "enabled": True,
            "mode": "manual",
            "source_type_adjustment": {"music": 0.03},
        },
        config_dir=tmp_path,
    )
    client = _client(monkeypatch, tmp_path, tuning_mode="manual", now=10_000.0)

    read = client.get("/api/proactive/recommendation/tuning")
    missing_csrf = client.post("/api/proactive/recommendation/tuning/reset", json={})
    headers = {
        "Origin": "http://testserver",
        "X-CSRF-Token": AUTOSTART_CSRF_TOKEN,
    }
    reset = client.post(
        "/api/proactive/recommendation/tuning/reset",
        headers=headers,
        json={},
    )

    assert read.status_code == 200
    assert read.json()["tuning"]["source_type_adjustment"] == {"music": 0.03}
    assert read.json()["tuning"]["health"]["status"] == "healthy"
    assert missing_csrf.status_code == 403
    assert reset.status_code == 200
    assert reset.json()["success"] is True
    assert reset.json()["tuning"]["enabled"] is False
    assert not (tmp_path / TUNING_FILENAME).exists()


def test_recommendation_tuning_pause_and_resume_require_csrf(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, tuning_mode="auto_safe", now=10_000.0)

    missing_csrf = client.post("/api/proactive/recommendation/tuning/pause", json={})
    headers = {
        "Origin": "http://testserver",
        "X-CSRF-Token": AUTOSTART_CSRF_TOKEN,
    }
    paused = client.post(
        "/api/proactive/recommendation/tuning/pause",
        headers=headers,
        json={"reason": "unit_test", "duration_seconds": 60},
    )
    resumed = client.post(
        "/api/proactive/recommendation/tuning/resume",
        headers=headers,
        json={},
    )

    assert missing_csrf.status_code == 403
    assert paused.status_code == 200
    assert paused.json()["success"] is True
    assert paused.json()["tuning"]["health"]["status"] == "paused"
    assert paused.json()["tuning"]["health"]["pause_reason"] == "unit_test"
    assert paused.json()["tuning"]["health"]["paused_until"] == 10_060.0
    assert resumed.status_code == 200
    assert resumed.json()["success"] is True
    assert resumed.json()["tuning"]["health"]["status"] == "healthy"
    assert resumed.json()["tuning"]["health"]["paused_until"] is None
    assert str(tmp_path) not in json.dumps(resumed.json(), ensure_ascii=False)


def test_feedback_endpoint_requires_csrf_and_rejects_sensitive_fields(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, now=10_000.0)

    missing_csrf = client.post(
        "/api/proactive/recommendation/feedback",
        json={"turn_id": "turn-1", "event_type": "user_reply"},
    )
    assert missing_csrf.status_code == 403

    headers = {
        "Origin": "http://testserver",
        "X-CSRF-Token": AUTOSTART_CSRF_TOKEN,
    }
    rejected = client.post(
        "/api/proactive/recommendation/feedback",
        headers=headers,
        json={
            "turn_id": "turn-1",
            "event_type": "user_reply",
            "payload": {"secret": "must-not-leak"},
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["success"] is False

    accepted = client.post(
        "/api/proactive/recommendation/feedback",
        headers=headers,
        json={
            "turn_id": "turn-1",
            "event_type": "user_reply_fast",
            "metadata": {"reply_latency_seconds": 12.5},
        },
    )
    payload = accepted.json()
    rows = (tmp_path / FEEDBACK_LOG_FILENAME).read_text(encoding="utf-8")
    assert payload["success"] is True
    assert payload["event"]["event_type"] == "user_reply_fast"
    assert "must-not-leak" not in json.dumps(payload, ensure_ascii=False)
    assert "must-not-leak" not in rows
