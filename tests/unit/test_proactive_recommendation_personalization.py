from main_logic.proactive_recommendation import (
    ProactiveRecommendationContext,
    build_phase1_material_shadow_decision,
)
from main_logic.proactive_recommendation.policy.personalization import (
    build_personalization_plan,
    personalization_adjustments,
)


def _state(*, positive: int, negative: int, affinity: float):
    return {
        "version": "feedback_state_preview_v2",
        "source_affinity": {
            "persistent": {
                "sources": {
                    "music": {
                        "positive_evidence_count": positive,
                        "negative_evidence_count": negative,
                        "affinity_preview": affinity,
                    }
                }
            }
        },
        "conversation_acceptance": {
            "persistent": {"acceptance_preview": 0.2}
        },
    }


def _decision(mode: str, adjustments=None):
    return build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("music", "meme"),
            personalization_mode=mode,
            personalization_adjustments=adjustments or {},
        ),
        phase1_topics=[("music", "calm song"), ("meme", "cat joke")],
        selected_music_link={
            "title": "Song",
            "artist": "Artist",
            "url": "https://example.test/song",
        },
        selected_meme_link={
            "title": "Meme",
            "url": "https://example.test/meme",
        },
        active_channels=("music", "meme"),
    )


def test_gradual_12_plan_requires_three_items_and_accumulates_smoothly():
    below = build_personalization_plan(
        _state(positive=2, negative=0, affinity=0.2),
        mode="active",
    )
    three = build_personalization_plan(
        _state(positive=3, negative=0, affinity=0.2),
        mode="active",
    )
    six = build_personalization_plan(
        _state(positive=6, negative=0, affinity=0.2),
        mode="active",
    )
    twelve = build_personalization_plan(
        _state(positive=12, negative=0, affinity=0.2),
        mode="active",
    )

    assert personalization_adjustments(below) == {"music": 0.0}
    assert personalization_adjustments(three) == {"music": 0.0075}
    assert personalization_adjustments(six) == {"music": 0.015}
    assert personalization_adjustments(twelve) == {"music": 0.03}


def test_plan_is_symmetric_and_ignores_conversation_acceptance():
    mixed = build_personalization_plan(
        _state(positive=3, negative=3, affinity=0.0),
        mode="active",
    )
    negative = build_personalization_plan(
        _state(positive=0, negative=12, affinity=-0.2),
        mode="active",
    )

    assert personalization_adjustments(mixed) == {"music": 0.0}
    assert personalization_adjustments(negative) == {"music": -0.03}


def test_off_is_byte_compatible_and_shadow_compare_does_not_change_ranking():
    baseline = _decision("off")
    off_with_adjustment = _decision("off", {"meme": 0.03})
    shadow = _decision("shadow_compare", {"meme": 0.03})

    assert baseline.to_log_dict() == off_with_adjustment.to_log_dict()
    assert baseline.selected_candidate.id == shadow.selected_candidate.id
    assert shadow.personalization["ranking_consumed"] is False
    assert shadow.personalization["top1_changed"] is True
    assert shadow.personalization["personalized_selected_source_type"] == "meme"


def test_active_uses_bounded_personalized_score_for_ranking():
    decision = _decision("active", {"meme": 0.03, "music": -0.5})

    assert decision.selected_candidate.source_type == "meme"
    assert decision.personalization["ranking_consumed"] is True
    assert decision.personalization["top1_changed"] is True
    music = next(
        row
        for row in decision.personalization["candidates"]
        if row["source_type"] == "music"
    )
    assert music["delta"] == -0.03
