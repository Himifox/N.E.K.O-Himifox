from types import SimpleNamespace

import pytest

from main_logic.proactive_recommendation_bandit import build_source_bandit_decision
from main_logic.proactive_recommendation_feedback import source_preference_outcome
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
