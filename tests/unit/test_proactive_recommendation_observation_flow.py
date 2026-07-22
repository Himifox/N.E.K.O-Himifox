import json

from main_logic.proactive_recommendation import (
    PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION,
    ProactiveCandidate,
    ProactiveRecommendationDecision,
    build_active_source_bias,
    build_recommendation_review_context,
)
from main_logic.proactive_recommendation_observer import (
    OBSERVATION_LOG_FILENAME,
    load_recommendation_observations_jsonl,
)
from main_logic.proactive_recommendation_feedback_state import (
    clear_temporary_feedback_state_preview,
    update_feedback_state_preview,
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
        activity_state="gaming",
        activity_propensity="restricted",
        decision_context={
            "timing": {
                "configured_interval_seconds": 300,
                "elapsed_since_last_delivery_seconds": 420.25,
                "recent_delivery_count_30m": 2,
                "recent_delivery_count_2h": 5,
                "consecutive_unanswered_deliveries": 1,
            }
        },
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
    assert rows[0]["activity_state"] == "gaming"
    assert rows[0]["activity_propensity"] == "restricted"
    assert rows[0]["decision_context"]["timing"] == {
        "configured_interval_seconds": 300.0,
        "elapsed_since_last_delivery_seconds": 420.25,
        "recent_delivery_count_30m": 2,
        "recent_delivery_count_2h": 5,
        "consecutive_unanswered_deliveries": 1,
    }
    assert rows[0]["algorithm_version"] == PROACTIVE_RECOMMENDATION_ALGORITHM_VERSION
    assert rows[0]["top_candidates"][0] == {
        "rank": 1,
        "id": "music:1",
        "source_type": "music",
        "family": "music",
        "topic_usable": True,
        "score": 0.91,
    }
    assert "payload" not in dumped
    assert "source_links" not in dumped
    assert "must-not-leak" not in dumped


def test_shadow_observation_records_feedback_state_preview_without_reranking(tmp_path):
    clear_temporary_feedback_state_preview()
    update_feedback_state_preview(
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
    assert preview["temporary"]["sources"]["music"]["interest_preview"] == 0.2
    assert preview["persistent"]["sources"]["music"]["affinity_preview"] == 0.0
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
    assert "review_context" not in observation
    assert not (tmp_path / OBSERVATION_LOG_FILENAME).exists()


def test_system_router_generates_turn_id_and_persists_same_id_for_pass(tmp_path):
    response_body = {
        "action": "pass",
        "reason_code": "PASS_MODEL_PASS",
    }

    observation = _record_proactive_recommendation_observation(
        _decision(_material_candidate("news")),
        lanlan_name="neko",
        response_body=response_body,
        recommendation_mode="shadow",
        observation_log_mode="jsonl",
        config_dir=tmp_path,
        ts=789.0,
        activity_state="away",
        activity_propensity="restricted",
    )
    rows = load_recommendation_observations_jsonl(tmp_path / OBSERVATION_LOG_FILENAME)

    assert observation["turn_id"]
    assert response_body["turn_id"] == observation["turn_id"]
    assert rows[0]["turn_id"] == observation["turn_id"]


def test_system_router_preserves_existing_turn_id():
    response_body = {
        "action": "pass",
        "reason_code": "PASS_MODEL_PASS",
        "turn_id": "existing-turn",
    }

    observation = _record_proactive_recommendation_observation(
        _decision(_material_candidate("meme")),
        lanlan_name="neko",
        response_body=response_body,
        recommendation_mode="shadow",
        observation_log_mode="off",
        ts=790.0,
    )

    assert response_body["turn_id"] == "existing-turn"
    assert observation["turn_id"] == "existing-turn"


def test_system_router_records_review_context_only_in_explicit_shadow_review_mode(tmp_path):
    response_body = {
        "action": "chat",
        "reason_code": "CHAT_DELIVERED",
        "turn_id": "review-turn",
    }
    observation = _record_proactive_recommendation_observation(
        _decision(_material_candidate("music")),
        lanlan_name="neko",
        response_body=response_body,
        recommendation_mode="shadow",
        observation_log_mode="jsonl",
        config_dir=tmp_path,
        ts=800.0,
        activity_state="focused_work",
        activity_propensity="restricted",
        review_context_mode="shadow_review",
        delivered_text="这是一段用于复核的短投递文本 https://example.test/?token=secret",
    )
    rows = load_recommendation_observations_jsonl(tmp_path / OBSERVATION_LOG_FILENAME)

    assert rows == [observation]
    assert observation["review_context"]["activity_state"] == "focused_work"
    assert observation["review_context"]["candidate_labels"][0]["id"] == "music:1"
    assert "example.test" not in json.dumps(observation["review_context"], ensure_ascii=False)


def test_review_context_omits_raw_vision_and_personal_text():
    vision = ProactiveCandidate(
        id="vision:1",
        source_type="vision",
        family="screen_context",
        topic="Private Window Title",
        summary="Full private screen text",
        score=0.7,
    )
    personal = ProactiveCandidate(
        id="personal:1",
        source_type="personal",
        family="personal",
        topic="Private personal dynamic",
        summary="Private conversation detail",
        score=0.6,
    )
    context = build_recommendation_review_context(
        _decision(vision, personal),
        mode="testbench",
        activity_state="idle",
    )
    dumped = json.dumps(context, ensure_ascii=False)

    assert "Private Window Title" not in dumped
    assert "Full private screen text" not in dumped
    assert "Private personal dynamic" not in dumped
    assert "Private conversation detail" not in dumped
    assert "vision_text_omitted" in context["redaction_notes"]
    assert "personal_text_omitted" in context["redaction_notes"]


def test_shadow_review_mode_does_not_attach_context_to_active_source_observation():
    observation = _record_proactive_recommendation_observation(
        _decision(_material_candidate("news")),
        lanlan_name="neko",
        response_body={
            "action": "chat",
            "reason_code": "CHAT_DELIVERED",
            "turn_id": "active-no-review",
        },
        recommendation_mode="active_source",
        observation_log_mode="off",
        review_context_mode="shadow_review",
        delivered_text="must not be exported in active mode",
    )

    assert "review_context" not in observation
