from types import SimpleNamespace

import pytest

from main_logic.proactive_recommendation import (
    ProactiveCandidate,
    ProactiveRecommendationDecision,
    build_recommendation_observation,
)
from main_logic.proactive_recommendation_bandit import (
    BANDIT_PERSONALIZED_SCORE_CONTRACT,
    build_source_bandit_decision,
    finalize_source_bandit_decision,
)
from main_logic.proactive_recommendation_feedback import (
    build_bandit_encounter_reward,
    clear_pending_recommendation_feedback,
    record_feedback_event_with_status,
    register_pending_feedback,
    register_pending_feedback_from_observation,
    source_preference_outcome,
)
from main_logic.proactive_recommendation_bandit_state import (
    get_recommendation_bandit_state,
    update_recommendation_bandit_reward,
)
from main_logic.proactive_recommendation_observer import (
    sanitize_recommendation_policy_decision,
)
from main_logic.proactive_recommendation_preference import (
    get_recommendation_preference_state,
    preference_adjustments,
    reset_recommendation_preference_state,
    update_recommendation_source_preference,
)


def _decision(*scores: tuple[str, float]):
    return SimpleNamespace(
        ranked_candidates=tuple(
            SimpleNamespace(id=f"{source}:1", source_type=source, score=score)
            for source, score in scores
        )
    )


def _personalized_decision():
    decision = _decision(("news", 0.60), ("music", 0.59))
    decision.score_breakdown = {
        "news:1": {"baseline_score": 0.60, "personalized_score": 0.58},
        "music:1": {"baseline_score": 0.59, "personalized_score": 0.61},
    }
    return decision


def test_off_preserves_deterministic_top_source():
    result = build_source_bandit_decision(
        _decision(("news", 0.60), ("music", 0.59)),
        mode="off",
    )

    assert result["chosen_arm"] == "news"
    assert result["action_probabilities"] == {"news": 1.0, "music": 0.0}
    assert result["exploration_eligible"] is False


def test_shadow_logs_exact_probabilities_without_creating_arms():
    result = build_source_bandit_decision(
        _decision(("news", 0.60), ("music", 0.59), ("vision", 0.595)),
        mode="shadow",
        random_value=1.0,
    )

    assert result["eligible_arms"] == ["news", "music"]
    assert result["action_probabilities"] == {"news": 0.975, "music": 0.025}
    assert sum(result["action_probabilities"].values()) == pytest.approx(1.0)


def test_shadow_exploits_learned_reward_only_inside_near_tie():
    bandit_state = {
        "version": "recommendation_bandit_state_v1",
        "arms": {
            "news": {
                "posterior_alpha": 2.0,
                "posterior_beta": 5.0,
                "posterior_mean": 2.0 / 7.0,
                "effective_evidence": 3.0,
            },
            "music": {
                "posterior_alpha": 5.0,
                "posterior_beta": 2.0,
                "posterior_mean": 5.0 / 7.0,
                "effective_evidence": 3.0,
            },
        },
    }
    result = build_source_bandit_decision(
        _decision(("news", 0.60), ("music", 0.59)),
        mode="shadow",
        bandit_state=bandit_state,
        random_value=1.0,
    )

    assert result["exploit_arm"] == "music"
    assert result["proposed_arm"] == "music"
    assert result["action_probabilities"] == {"news": 0.025, "music": 0.975}
    assert result["arm_bandit_posteriors"]["music"]["mean"] == pytest.approx(
        5.0 / 7.0, abs=1e-6
    )


def test_learned_reward_cannot_override_policy_score_outside_near_tie():
    result = build_source_bandit_decision(
        _decision(("news", 0.70), ("music", 0.50)),
        mode="shadow",
        bandit_state={
            "arms": {
                "news": {"posterior_mean": 0.2},
                "music": {"posterior_mean": 0.9},
            }
        },
        random_value=1.0,
    )

    assert result["near_tie_arms"] == ["news"]
    assert result["exploit_arm"] == result["proposed_arm"] == "news"
    assert result["action_probabilities"] == {"news": 1.0, "music": 0.0}


def test_canary_explores_only_safe_near_tie_arms():
    result = build_source_bandit_decision(
        _decision(("news", 0.60), ("music", 0.59), ("meme", 0.40)),
        mode="canary",
        random_value=0.0,
        random_arm_value=0.75,
    )

    assert result["near_tie_arms"] == ["news", "music"]
    assert result["chosen_arm"] == "music"
    assert result["explored"] is True
    assert result["action_probabilities"]["meme"] == 0.0


def test_shadow_and_canary_share_personalized_policy_score():
    decision = _personalized_decision()
    shadow = build_source_bandit_decision(
        decision,
        mode="shadow",
        score_contract=BANDIT_PERSONALIZED_SCORE_CONTRACT,
        random_value=1.0,
    )
    canary = build_source_bandit_decision(
        decision,
        mode="canary",
        score_contract=BANDIT_PERSONALIZED_SCORE_CONTRACT,
        random_value=1.0,
    )

    assert shadow["proposed_arm"] == canary["proposed_arm"] == "music"
    assert shadow["arm_baseline_scores"] == {"music": 0.59, "news": 0.60}
    assert shadow["arm_policy_scores"] == {"music": 0.61, "news": 0.58}

    finalized_shadow = finalize_source_bandit_decision(
        shadow,
        actual_candidate=decision.ranked_candidates[0],
        delivered=True,
    )
    finalized_canary = finalize_source_bandit_decision(
        canary,
        actual_candidate=decision.ranked_candidates[1],
        delivered=True,
    )
    assert finalized_shadow["actual_arm"] == "news"
    assert finalized_shadow["policy_applied"] is False
    assert finalized_shadow["behavior_action_probabilities"] == {
        "music": 0.0,
        "news": 1.0,
    }
    assert finalized_canary["actual_arm"] == "music"
    assert finalized_canary["policy_applied"] is True
    assert finalized_canary["behavior_action_probabilities"] == canary[
        "target_action_probabilities"
    ]
    assert sanitize_recommendation_policy_decision(finalized_shadow)[
        "context_version"
    ] == "source-context-v3"


def test_non_bandit_global_top_blocks_policy():
    result = build_source_bandit_decision(
        _decision(("vision", 0.80), ("news", 0.60), ("music", 0.59)),
        mode="canary",
        random_value=0.0,
    )

    assert result["eligible_arms"] == []
    assert result["proposed_arm"] is None
    assert result["fallback_reason"] == "top_source_not_bandit_arm"


def test_shadow_observation_binds_behavior_to_actual_material():
    news = ProactiveCandidate(
        id="news:1",
        source_type="news",
        family="news",
        topic="News",
        score=0.60,
    )
    music = ProactiveCandidate(
        id="music:1",
        source_type="music",
        family="music",
        topic="Music",
        score=0.59,
    )
    decision = ProactiveRecommendationDecision(
        candidate_count=2,
        selected_candidate=news,
        ranked_candidates=(news, music),
        shadow_selected_source_type="news",
        score_breakdown={
            "news:1": {"baseline_score": 0.60, "personalized_score": 0.58},
            "music:1": {"baseline_score": 0.59, "personalized_score": 0.61},
        },
    )
    policy = build_source_bandit_decision(
        decision,
        mode="shadow",
        score_contract=BANDIT_PERSONALIZED_SCORE_CONTRACT,
        random_value=1.0,
    )

    observation = build_recommendation_observation(
        decision,
        recommendation_mode="active_source",
        action="chat",
        reason_code="CHAT_DELIVERED",
        source_mode="chat",
        source_tag="WEB",
        active_channels=["web"],
        policy_decision=policy,
    )

    finalized = observation["policy_decision"]
    assert finalized["proposed_arm"] == "music"
    assert finalized["actual_arm"] == "news"
    assert finalized["actual_candidate_id"] == "news:1"
    assert finalized["policy_applied"] is False
    assert finalized["actual_action_probability"] == 1.0


def test_preference_state_deduplicates_and_explicit_feedback_wins(tmp_path):
    for index in range(3):
        state = update_recommendation_source_preference(
            config_dir=tmp_path,
            turn_id=f"turn-{index}",
            source_type="music",
            success=1,
            failure=0,
            explicit=False,
            now=100 + index,
        )
    assert preference_adjustments(state)["music"] == pytest.approx(0.0075)

    duplicate = update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-2",
        source_type="music",
        success=1,
        failure=0,
        explicit=False,
        now=104,
    )
    assert duplicate["sources"]["music"]["effective_evidence"] == pytest.approx(3.0)

    corrected = update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-2",
        source_type="music",
        success=0,
        failure=1,
        explicit=True,
        now=105,
    )
    assert corrected["sources"]["music"]["effective_success"] == pytest.approx(
        2.0, abs=1e-5
    )
    assert corrected["sources"]["music"]["effective_failure"] == pytest.approx(
        1.0, abs=1e-5
    )


def test_bandit_reward_replaces_turn_aggregate_and_decays(tmp_path):
    first = update_recommendation_bandit_reward(
        config_dir=tmp_path,
        turn_id="turn-1",
        arm="news",
        reward=0.2,
        event_types=["user_reply"],
        now=100,
    )
    assert first["arms"]["news"]["effective_success"] == pytest.approx(0.2)

    combined = update_recommendation_bandit_reward(
        config_dir=tmp_path,
        turn_id="turn-1",
        arm="news",
        reward=0.55,
        event_types=["user_reply", "user_continue"],
        now=101,
    )
    assert combined["finalized_outcome_count"] == 1
    assert combined["arms"]["news"]["effective_success"] == pytest.approx(
        0.55, abs=1e-6
    )

    decayed = get_recommendation_bandit_state(
        config_dir=tmp_path,
        now=101 + 30 * 24 * 60 * 60,
    )
    assert decayed["arms"]["news"]["effective_success"] == pytest.approx(
        0.275, abs=1e-6
    )


def test_bandit_reward_reuses_v2_rules_and_excludes_technical_only():
    reward = build_bandit_encounter_reward(
        [
            {"event_type": "user_reply"},
            {"event_type": "user_continue"},
        ]
    )
    technical = build_bandit_encounter_reward([{"event_type": "music_error"}])

    assert reward == {
        "version": "bandit_encounter_reward_v1",
        "rule_score_version": "reward_score_v2_preview_v2",
        "eligible": True,
        "reward": 0.55,
        "event_types": ["user_reply", "user_continue"],
        "signal_event_types": ["user_reply", "user_continue"],
        "excluded_reason": None,
    }
    assert technical["eligible"] is False
    assert technical["reward"] is None


def test_generic_reply_updates_bandit_but_not_source_preference(tmp_path):
    clear_pending_recommendation_feedback()
    register_pending_feedback(
        lanlan_name="neko",
        turn_id="news-turn",
        source_type="news",
        candidate_id="news:actual",
        delivered_at=100,
        log_mode="jsonl",
        config_dir=tmp_path,
        recommendation_mode="shadow",
    )

    reply = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="news-turn",
        event_type="user_reply",
        ts=110,
    )
    continued = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="news-turn",
        event_type="user_continue",
        ts=111,
    )

    bandit = get_recommendation_bandit_state(config_dir=tmp_path, now=111)
    preference = get_recommendation_preference_state(config_dir=tmp_path, now=111)
    assert reply.bandit_state_updated is True
    assert continued.bandit_state_updated is True
    assert bandit["arms"]["news"]["effective_success"] == pytest.approx(0.55)
    assert bandit["finalized_outcome_count"] == 1
    assert preference["sources"] == {}


def test_v3_shadow_reward_binds_actual_arm_and_excludes_technical_zero(tmp_path):
    clear_pending_recommendation_feedback()
    observation = {
        "delivered": True,
        "lanlan_name": "neko",
        "turn_id": "shadow-v3",
        "ts": 100,
        "recommendation_mode": "shadow",
        "matched_actual_material": True,
        "policy_decision": {
            "context_version": "source-context-v3",
            "mode": "shadow",
            "proposed_arm": "news",
            "proposed_candidate_id": "news:virtual",
            "actual_arm": "music",
            "actual_candidate_id": "music:actual",
        },
    }
    pending = register_pending_feedback_from_observation(
        observation,
        log_mode="jsonl",
        config_dir=tmp_path,
    )
    assert pending is not None
    assert pending.source_type == "music"

    technical = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="shadow-v3",
        event_type="music_error",
        ts=101,
    )
    assert technical.bandit_state_updated is False
    assert get_recommendation_bandit_state(
        config_dir=tmp_path, now=101
    )["arms"] == {}

    played = record_feedback_event_with_status(
        lanlan_name="neko",
        turn_id="shadow-v3",
        event_type="music_played_through",
        ts=102,
    )
    state = get_recommendation_bandit_state(config_dir=tmp_path, now=102)
    assert played.bandit_state_updated is True
    assert set(state["arms"]) == {"music"}
    assert state["arms"]["music"]["effective_success"] == pytest.approx(0.9)


def test_explicit_override_removes_decayed_natural_contribution(tmp_path):
    half_life = 30 * 24 * 60 * 60
    update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-1",
        source_type="music",
        success=1,
        failure=0,
        explicit=False,
        now=100,
    )
    corrected = update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-1",
        source_type="music",
        success=0,
        failure=1,
        explicit=True,
        now=100 + half_life,
    )

    assert corrected["sources"]["music"]["effective_success"] == pytest.approx(0.0)
    assert corrected["sources"]["music"]["effective_failure"] == pytest.approx(1.0)
    assert corrected["legacy_replacement_approximation_count"] == 0


def test_stronger_natural_outcome_replaces_weaker_outcome(tmp_path):
    update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-1",
        source_type="music",
        success=0.5,
        failure=0,
        explicit=False,
        outcome_strength=0.5,
        now=100,
    )
    replaced = update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-1",
        source_type="music",
        success=1,
        failure=0,
        explicit=False,
        outcome_strength=1,
        now=101,
    )

    assert replaced["sources"]["music"]["effective_success"] == pytest.approx(
        1.0, abs=1e-6
    )


def test_preference_state_decays_and_can_be_reset(tmp_path):
    update_recommendation_source_preference(
        config_dir=tmp_path,
        turn_id="turn-1",
        source_type="news",
        success=1,
        failure=0,
        explicit=True,
        now=100,
    )
    decayed = get_recommendation_preference_state(
        config_dir=tmp_path,
        now=100 + 30 * 24 * 60 * 60,
    )
    assert decayed["sources"]["news"]["effective_success"] == pytest.approx(0.5)
    assert reset_recommendation_preference_state(config_dir=tmp_path) is True
    assert get_recommendation_preference_state(config_dir=tmp_path)["sources"] == {}


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("source_interested", (1.0, 0.0, True)),
        ("source_not_interested", (0.0, 1.0, True)),
        ("music_played_through", (1.0, 0.0, False)),
        ("music_hard_skip", (0.0, 1.0, False)),
        ("music_error", None),
    ],
)
def test_source_reward_contract(event_type, expected):
    assert source_preference_outcome(event_type) == expected
