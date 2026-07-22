"""P44-G1 encounter acceptance contract smoke."""
from __future__ import annotations

import inspect
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main_logic.proactive_recommendation_feedback import build_feedback_event
from tests.testbench.pipeline import recommendation_adapter
from tests.testbench.pipeline.recommendation_encounter_acceptance import (
    analyze_encounter_acceptance,
    render_encounter_acceptance_markdown,
)


def _state(value: float) -> dict:
    evidence = 3 if value else 0
    return {
        "version": "feedback_state_preview_v1",
        "preview_only": True,
        "ranking_consumed": False,
        "tuning_consumed": False,
        "temporary": {
            "ttl_seconds": 7_200,
            "sources": {
                "chat": {
                    "interest_preview": value,
                    "positive_evidence_count": evidence,
                    "negative_evidence_count": 0,
                    "expires_in_seconds": 7_000,
                },
                "music": {
                    "interest_preview": value,
                    "positive_evidence_count": evidence,
                    "negative_evidence_count": 0,
                    "expires_in_seconds": 7_000,
                },
            },
        },
        "persistent": {
            "min_explicit_evidence": 3,
            "sources": {
                "chat": {
                    "affinity_preview": value,
                    "positive_evidence_count": evidence,
                    "negative_evidence_count": 0,
                    "updated_at": 900.0,
                },
                "music": {
                    "affinity_preview": value,
                    "positive_evidence_count": evidence,
                    "negative_evidence_count": 0,
                    "updated_at": 900.0,
                },
            },
        },
    }


def _observation(index: int, *, mode: str, delivered: bool = True, value: float = 0.0) -> dict:
    return {
        "ts": 1_000.0 + index,
        "lanlan_name": "g1-user",
        "turn_id": f"g1-{index}",
        "recommendation_mode": "shadow",
        "delivered": delivered,
        "actual_primary_channel": mode if delivered else None,
        "shadow_selected_source_type": "news" if mode == "chat" else "music",
        "shadow_selected_candidate_id": f"candidate-{index}",
        "matched_actual_material": False,
        "feedback_state_preview": _state(value),
    }


def _event(index: int, event_type: str, mode: str, *, offset: float = 100.0) -> dict:
    return build_feedback_event(
        lanlan_name="g1-user",
        turn_id=f"g1-{index}",
        event_type=event_type,
        source_type=mode,
        ts=1_000.0 + index + offset,
    )


def _dataset() -> dict:
    observations: list[dict] = []
    feedback: list[dict] = []
    for index in range(50):
        if index < 20:
            mode = "chat"
            delivered = True
        else:
            mode = "chat"
            delivered = False
        local = index % 10
        value = 0.4 if local % 2 == 0 else 0.0
        observations.append(_observation(index, mode=mode, delivered=delivered, value=value))
        if not delivered or local >= 8:
            continue
        positive = local % 2 == 0
        feedback.append(_event(index, "user_reply" if positive else "proactive_disabled_after", mode))
    # Production reward dedupes an identical event type on the same turn.
    feedback.append(_event(0, "user_reply", "chat", offset=120.0))
    # Future feedback must not affect the point-in-time result.
    feedback.append(_event(0, "music_played_through", "music", offset=5_000.0))
    return {"observations": observations, "feedback": feedback}


def main() -> int:
    dataset = _dataset()
    report = analyze_encounter_acceptance(dataset, as_of=3_000.0)
    replay = analyze_encounter_acceptance(dataset, as_of=3_000.0)
    assert report == replay
    assert report["input"]["preview_observation_count"] == 50
    assert report["input"]["eligible_delivered_encounter_count"] == 20
    assert report["conclusion"]["status"] == "candidate_for_scheduler_shadow_design"
    assert report["encounters"]["chat"]["explicit_reward_count"] == 16
    assert report["encounters"]["chat"]["conversation_feedback_count"] == 16
    assert report["all_encounters"]["conversation_feedback_count"] == 16
    assert report["material_sources"]["news"]["conversation_feedback_count"] == 16
    assert report["encounters"]["chat"]["average_reward"] == -0.25
    assert report["data_issues"]["distribution"]["feedback_after_as_of"] == 1

    music_observation = [_observation(100, mode="music", value=0.4)]
    music_feedback = [
        _event(100, "user_reply", "music"),
        _event(100, "user_reply", "music", offset=120.0),
        _event(100, "music_played_through", "music", offset=130.0),
    ]
    production = recommendation_adapter.run_reward_score_v2_preview(
        music_observation,
        music_feedback,
        now=2_000.0,
        window_seconds=2_000,
        sample_limit=1,
    )["joined"][0]
    assert production["reward_score_v2_preview"] == 1.0
    assert production["reward_components_v2_preview"]["reply"] == 0.2
    assert production["reward_components_v2_preview"]["consumption"] == 0.9
    assert production["feedback_event_types"].count("user_reply") == 1

    music_report = analyze_encounter_acceptance(
        {"observations": music_observation, "feedback": music_feedback},
        as_of=2_000.0,
    )
    assert music_report["all_encounters"]["conversation_feedback_count"] == 1
    assert music_report["material_sources"]["music"]["conversation_feedback_count"] == 1
    assert music_report["material_sources"]["music"]["resource_feedback_count"] == 1
    assert music_report["material_sources"]["music"]["average_combined_reward"] == 1.0
    assert music_report["scope"]["shared_conversation_state_contract_complete"] is False

    chat_observation = [_observation(101, mode="chat", value=0.0)]
    mismatched = recommendation_adapter.run_reward_score_v2_preview(
        chat_observation,
        [_event(101, "music_played_through", "music")],
        now=2_000.0,
        window_seconds=2_000,
        sample_limit=1,
    )["joined"][0]
    assert mismatched["reward_score_v2_preview"] is None
    assert mismatched["attribution_issue"] == "source_mismatch"

    technical = analyze_encounter_acceptance(
        {
            "observations": [_observation(102, mode="music", value=0.0)],
            "feedback": [_event(102, "music_error", "music")],
        },
        as_of=2_000.0,
    )
    assert technical["encounters"]["music"]["technical_only_turn_count"] == 1
    assert technical["encounters"]["music"]["explicit_reward_count"] == 0
    assert technical["conclusion"]["status"] == "insufficient_evidence"

    markdown = render_encounter_acceptance_markdown(report)
    assert "每次主动搭话都共享" in markdown
    assert "素材按来源展示接受度" in markdown
    assert "未修改生产权重" in markdown

    adapter_source = inspect.getsource(recommendation_adapter)
    analysis_source = (PROJECT_ROOT / "tests" / "testbench" / "pipeline" / "recommendation_encounter_acceptance.py").read_text(encoding="utf-8")
    assert "_REWARD_V2_PREVIEW_EVENT_COMPONENTS" not in adapter_source + analysis_source
    assert "decision_source_type" not in adapter_source + analysis_source
    for source_path in (
        PROJECT_ROOT / "main_logic" / "proactive_recommendation.py",
        PROJECT_ROOT / "main_logic" / "proactive_recommendation_tuning.py",
    ):
        assert "feedback_state_preview" not in source_path.read_text(encoding="utf-8")
    print("P53 ENCOUNTER ACCEPTANCE SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
