import json

from main_logic.proactive_recommendation import (
    ProactiveCandidate,
    ProactiveRecommendationDecision,
    build_active_source_bias,
)
from main_logic.proactive_recommendation_observer import (
    OBSERVATION_LOG_FILENAME,
    load_recommendation_observations_jsonl,
)
from main_logic.proactive_recommendation_feedback_state import (
    clear_temporary_feedback_state_preview,
    update_source_affinity_preview,
)
from main_routers.system_router import _record_proactive_recommendation_observation


def _material_candidate(source_type, *, score=0.8, url="https://example.test/item"):
    return ProactiveCandidate(
        id=f"{source_type}:1",
        source_type=source_type,
        family=source_type,
        topic=f"{source_type} topic",
        summary=f"{source_type} summary",
        payload={
            "link": {
                "url": url,
                "title": f"{source_type} title",
                "payload": "must-not-leak",
            }
        },
        score=score,
    )


def _decision(*candidates):
    return ProactiveRecommendationDecision(
        candidate_count=len(candidates),
        selected_candidate=candidates[0] if candidates else None,
        decision_stage="phase1_material",
        ranked_candidates=tuple(candidates),
        shadow_selected_source_type=candidates[0].source_type if candidates else None,
    )


def test_system_router_records_recommendation_observation_to_jsonl(tmp_path):
    top = _material_candidate("music", score=0.91, url="https://example.test/song")
    runner_up = _material_candidate("meme", score=0.4, url="https://example.test/meme")
    decision = _decision(top, runner_up)
    bias = build_active_source_bias(decision)

    observation = _record_proactive_recommendation_observation(
        decision,
        lanlan_name="neko",
        response_body={
            "action": "chat",
            "reason_code": "CHAT_DELIVERED",
            "stage": "delivery",
            "source_mode": "music",
            "source_tag": "MUSIC",
            "active_channels": ["music", "meme"],
            "source_links": [
                {
                    "title": "music title",
                    "url": "https://example.test/song",
                    "payload": "must-not-leak",
                }
            ],
            "turn_id": "turn-jsonl",
        },
        recommendation_mode="active_source",
        active_bias=bias,
        observation_log_mode="jsonl",
        config_dir=tmp_path,
        ts=123.0,
    )

    rows = load_recommendation_observations_jsonl(tmp_path / OBSERVATION_LOG_FILENAME)
    dumped = json.dumps(rows, ensure_ascii=False)

    assert observation["delivered"] is True
    assert observation["actual_rank"] == 1
    assert observation["matched_actual_source"] is True
    assert observation["matched_actual_material"] is True
    assert observation["active_bias_applied"] is True
    assert observation["active_model_followed_preference"] is True
    assert rows == [observation]
    assert rows[0]["recommendation_mode"] == "active_source"
    assert "feedback_state_preview" not in rows[0]
    assert rows[0]["shadow_selected_source_type"] == "music"
    assert rows[0]["top_candidates"][0] == {
        "rank": 1,
        "id": "music:1",
        "source_type": "music",
        "family": "music",
        "topic": "music topic",
        "score": 0.91,
    }
    assert "payload" not in dumped
    assert "source_links" not in dumped
    assert "must-not-leak" not in dumped


def test_shadow_observation_records_feedback_state_preview_without_reranking(tmp_path):
    clear_temporary_feedback_state_preview()
    update_source_affinity_preview(
        config_dir=tmp_path,
        source_type="music",
        score=0.2,
        persistent_eligible=True,
        now=100.0,
    )
    decision = _decision(_material_candidate("music", score=0.91))

    observation = _record_proactive_recommendation_observation(
        decision,
        lanlan_name="neko",
        response_body={
            "action": "chat",
            "reason_code": "CHAT_DELIVERED",
            "source_mode": "music",
            "source_tag": "MUSIC",
            "active_channels": ["music"],
            "source_links": [{"url": "https://example.test/item"}],
            "turn_id": "shadow-state-preview",
        },
        recommendation_mode="shadow",
        observation_log_mode="jsonl",
        config_dir=tmp_path,
        ts=200.0,
    )

    preview = observation["feedback_state_preview"]
    assert preview["preview_only"] is True
    assert preview["ranking_consumed"] is False
    assert preview["tuning_consumed"] is False
    assert (
        preview["source_affinity"]["temporary"]["sources"]["music"]
        ["interest_preview"]
        == 0.2
    )
    assert (
        preview["source_affinity"]["persistent"]["sources"]["music"]
        ["affinity_preview"]
        == 0.0
    )
    assert preview["conversation_acceptance"]["temporary"]["interest_preview"] == 0.0
    assert decision.selected_candidate.score == 0.91
    assert decision.score_breakdown == {}


def test_system_router_observation_flow_does_not_write_when_jsonl_disabled(tmp_path):
    top = _material_candidate("meme", score=0.88, url="https://example.test/meme")
    decision = _decision(top)

    observation = _record_proactive_recommendation_observation(
        decision,
        lanlan_name="neko",
        response_body={
            "action": "pass",
            "reason_code": "PASS_MODEL_PASS",
            "stage": "model_decision",
            "source_mode": None,
            "source_tag": "PASS",
            "turn_id": "turn-pass",
        },
        recommendation_mode="shadow",
        observation_log_mode="off",
        config_dir=tmp_path,
        ts=456.0,
    )

    assert observation["delivered"] is False
    assert observation["actual_rank"] is None
    assert observation["actual_reason_code"] == "PASS_MODEL_PASS"
    assert not (tmp_path / OBSERVATION_LOG_FILENAME).exists()
