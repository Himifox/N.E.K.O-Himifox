from types import SimpleNamespace

from main_logic.proactive_recommendation import (
    ProactiveCandidate,
    ProactiveRecommendationDecision,
    ProactiveRecommendationContext,
    build_active_source_bias,
    build_phase1_material_shadow_decision,
    build_recommendation_observation,
    build_candidates,
    build_shadow_recommendation_decision,
    reorder_phase1_topics_for_bias,
    resolve_recommendation_activity_state,
    source_type_to_phase2_tag,
)


def _ranked_material_decision(*candidates):
    return ProactiveRecommendationDecision(
        candidate_count=len(candidates),
        selected_candidate=candidates[0] if candidates else None,
        decision_stage="phase1_material",
        ranked_candidates=tuple(candidates),
        shadow_selected_source_type=candidates[0].source_type if candidates else None,
    )


def _material_candidate(source_type, *, score=0.8, url="https://example.test/item"):
    return ProactiveCandidate(
        id=f"{source_type}:1",
        source_type=source_type,
        family=source_type,
        topic=f"{source_type} topic",
        summary=f"{source_type} summary",
        payload={"link": {"url": url, "title": f"{source_type} title"}} if url else {},
        score=score,
    )


def test_activity_state_resolution_uses_inferred_state_not_collapsed_propensity():
    snapshot = SimpleNamespace(
        state="focused_work",
        propensity="restricted_screen_only",
    )

    assert resolve_recommendation_activity_state(snapshot) == "focused_work"
    assert resolve_recommendation_activity_state(None) == "unknown"


def test_away_activity_filters_non_contextual_material():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("news", "vision"),
            activity_state=resolve_recommendation_activity_state(
                SimpleNamespace(state="away", propensity="restricted_screen_only")
            ),
        ),
        phase1_topics=[("web", "headline")],
        selected_web_link={
            "title": "public headline",
            "url": "https://example.test/news",
            "mode": "news",
        },
        vision_content={"window_category": "desktop"},
        active_channels=["web", "vision"],
    )

    filtered_news = {
        candidate_id: reason
        for candidate_id, reason in decision.filtered_reasons.items()
        if candidate_id.startswith("news:")
    }
    assert filtered_news
    assert set(filtered_news.values()) == {"activity_busy"}


def test_build_candidates_normalizes_sources_topic_hooks_and_mini_game():
    ctx = ProactiveRecommendationContext(
        lanlan_name="neko",
        enabled_modes=("news", "music"),
        topic_materials=[
            {
                "interest": "learning to cook",
                "relevance": 92,
                "risk": 10,
                "material_hint": {"summary": "starter cooking topic"},
            }
        ],
        mini_game_available=True,
    )
    sources = {
        "news": {
            "links": [
                {
                    "title": "new food science story",
                    "url": "https://example.test/food",
                    "source": "example",
                }
            ],
            "raw_data": {"source": "example"},
        },
        "music": {"placeholder": True, "note": "keyword will be picked later"},
        "video": {
            "links": [
                {
                    "title": "disabled source should not enter",
                    "url": "https://example.test/video",
                }
            ]
        },
    }

    candidates = build_candidates(ctx, sources)

    assert {candidate.source_type for candidate in candidates} == {
        "news",
        "music",
        "topic_hook",
        "mini_game",
    }
    assert any(candidate.family == "topic_hook" for candidate in candidates)
    assert any("placeholder" in candidate.risk_flags for candidate in candidates)


def test_shadow_decision_accepts_empty_sources_without_failure():
    decision = build_shadow_recommendation_decision(
        ProactiveRecommendationContext(lanlan_name="neko"),
        {},
    )

    assert decision.candidate_count == 0
    assert decision.selected_candidate is None
    assert decision.shadow_selected_source_type is None


def test_privacy_closed_filters_privacy_sensitive_candidates():
    decision = build_shadow_recommendation_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("vision", "news"),
            privacy_state="closed",
        ),
        {
            "vision": {
                "window_title": "private banking page",
                "screenshot_b64": "abc",
            },
            "news": {
                "links": [
                    {
                        "title": "public story",
                        "url": "https://example.test/news",
                    }
                ]
            },
        },
    )

    assert decision.shadow_selected_source_type == "news"
    assert any(reason == "privacy_sensitive" for reason in decision.filtered_reasons.values())


def test_rule_score_downranks_recent_source_and_prefers_ready_topic_hook():
    decision = build_shadow_recommendation_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("news",),
            source_weights={"news": 0.9},
            recent_sources=("news", "news"),
            activity_state="stale_returning",
            topic_materials=[
                {
                    "interest": "budget-friendly cooking habit",
                    "relevance": 95,
                    "risk": 5,
                }
            ],
        ),
        {
            "news": {
                "links": [
                    {
                        "title": "another cooking headline",
                        "url": "https://example.test/news",
                    }
                ]
            }
        },
    )

    assert decision.shadow_selected_source_type == "topic_hook"
    news_breakdown = next(
        values
        for key, values in decision.score_breakdown.items()
        if key.startswith("news:")
    )
    topic_breakdown = next(
        values
        for key, values in decision.score_breakdown.items()
        if key.startswith("topic_hook:")
    )
    assert news_breakdown["novelty"] < topic_breakdown["novelty"]
    assert decision.selected_candidate is not None
    assert decision.selected_candidate.score > news_breakdown["score"]


def test_tuning_adjustment_is_reported_and_applied():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("music",),
            source_weights={"music": 1.0},
            source_type_adjustments={"music": 0.5},
        ),
        phase1_topics=[("music", "song topic")],
        selected_music_link={
            "title": "Song",
            "artist": "Artist",
            "url": "https://example.test/song",
        },
        active_channels=("music",),
    )

    assert decision.selected_candidate is not None
    breakdown = decision.score_breakdown[decision.selected_candidate.id]
    assert breakdown["tuning_adjustment"] == 0.15
    assert breakdown["base_score"] < breakdown["score"]
    assert breakdown["score"] == decision.selected_candidate.score


def test_diversity_penalty_downranks_repeated_shadow_source_and_candidate():
    selected_meme_link = {
        "title": "same reaction meme",
        "url": "https://example.test/repeated-meme.png",
    }
    selected_music_link = {
        "title": "Fresh Song",
        "artist": "Neko Band",
        "url": "https://example.test/fresh-song",
    }
    baseline = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("meme", "music"),
            source_weights={"meme": 1.0, "music": 0.2},
        ),
        phase1_topics=[("meme", "meme summary"), ("music", "music summary")],
        selected_meme_link=selected_meme_link,
        selected_music_link=selected_music_link,
        active_channels=["meme", "music"],
    )
    meme_id = next(
        candidate.id for candidate in baseline.ranked_candidates if candidate.source_type == "meme"
    )

    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("meme", "music"),
            source_weights={"meme": 1.0, "music": 0.2},
            recent_shadow_sources=("meme", "meme", "meme", "meme"),
            recent_candidate_ids=(meme_id,),
        ),
        phase1_topics=[("meme", "meme summary"), ("music", "music summary")],
        selected_meme_link=selected_meme_link,
        selected_music_link=selected_music_link,
        active_channels=["meme", "music"],
    )

    meme_breakdown = next(
        values for key, values in decision.score_breakdown.items() if key == meme_id
    )
    music_breakdown = next(
        values
        for key, values in decision.score_breakdown.items()
        if key.startswith("music:")
    )
    assert meme_breakdown["shadow_source_repeat_count"] == 4
    assert meme_breakdown["shadow_source_streak"] == 4
    assert meme_breakdown["candidate_repeat_count"] == 1
    assert meme_breakdown["diversity_penalty"] == 0.3
    assert music_breakdown["diversity_penalty"] == 0.0
    assert decision.shadow_selected_source_type == "music"


def test_observation_marks_shadow_and_actual_source_match():
    decision = build_shadow_recommendation_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            topic_materials=[
                {
                    "interest": "budget-friendly cooking habit",
                    "relevance": 95,
                    "risk": 5,
                }
            ],
        ),
        {},
    )

    observation = build_recommendation_observation(
        decision,
        action="chat",
        reason_code="CHAT_DELIVERED",
        stage="delivery",
        source_mode="topic_hook",
        source_tag="CHAT",
        active_channels=["topic_hook"],
    )

    assert observation["shadow_selected_source_type"] == "topic_hook"
    assert observation["actual_primary_channel"] == "topic_hook"
    assert observation["delivered"] is True
    assert observation["matched_actual_source"] is True


def test_observation_marks_pass_as_not_delivered_without_actual_channel():
    decision = build_shadow_recommendation_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("news",),
            source_weights={"news": 1.0},
        ),
        {
            "news": {
                "links": [
                    {
                        "title": "public story",
                        "url": "https://example.test/news",
                    }
                ]
            }
        },
    )

    observation = build_recommendation_observation(
        decision,
        action="pass",
        reason_code="PASS_MODEL_PASS",
        stage="model_decision",
    )

    assert observation["shadow_selected_source_type"] == "news"
    assert observation["actual_primary_channel"] is None
    assert observation["actual_reason_code"] == "PASS_MODEL_PASS"
    assert observation["delivered"] is False
    assert observation["matched_actual_source"] is False


def test_observation_marks_shadow_and_actual_source_mismatch():
    decision = build_shadow_recommendation_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("meme",),
            source_weights={"meme": 1.0},
        ),
        {
            "meme": {
                "links": [
                    {
                        "title": "meme template",
                        "url": "https://example.test/meme.png",
                    }
                ]
            }
        },
    )

    observation = build_recommendation_observation(
        decision,
        action="chat",
        reason_code="CHAT_DELIVERED",
        stage="delivery",
        source_mode="music",
        source_tag="MUSIC",
        active_channels=["meme", "music"],
    )

    assert observation["shadow_selected_source_type"] == "meme"
    assert observation["actual_primary_channel"] == "music"
    assert observation["delivered"] is True
    assert observation["matched_actual_source"] is False


def test_phase1_material_shadow_builds_concrete_candidates_without_screenshot_payload():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("news", "music", "meme", "vision"),
            topic_materials=[
                {
                    "interest": "long-term cooking interest",
                    "relevance": 90,
                    "risk": 5,
                }
            ],
        ),
        phase1_topics=[
            ("web", "news summary"),
            ("music", "music summary"),
            ("meme", "meme summary"),
        ],
        selected_web_link={
            "title": "fresh food science story",
            "url": "https://example.test/news",
            "mode": "news",
        },
        selected_music_link={
            "title": "Kitchen Song",
            "artist": "Neko Band",
            "url": "https://example.test/music",
        },
        selected_meme_link={
            "title": "pan flip meme",
            "url": "https://example.test/meme.png",
        },
        vision_content={
            "window_title": "Recipe notes",
            "screenshot_b64": "must-not-leak",
        },
        active_channels=["web", "music", "meme", "vision"],
    )

    assert decision.decision_stage == "phase1_material"
    source_types = {candidate.source_type for candidate in decision.ranked_candidates}
    assert {"news", "music", "meme", "vision", "topic_hook"} <= source_types
    vision_candidate = next(
        candidate for candidate in decision.ranked_candidates if candidate.source_type == "vision"
    )
    assert "screenshot_b64" not in vision_candidate.payload
    top_candidate = decision.to_log_dict()["top_candidates"][0]
    assert set(top_candidate) == {"rank", "id", "source_type", "family", "topic", "score"}


def test_news_overuse_calibration_lets_close_meme_candidate_win():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("news", "meme"),
            source_weights={"news": 0.5, "meme": 0.5},
        ),
        phase1_topics=[
            ("web", "fresh but generic headline"),
            ("meme", "lightweight reaction meme"),
        ],
        selected_web_link={
            "title": "fresh but generic headline",
            "url": "https://example.test/news",
            "mode": "news",
        },
        selected_meme_link={
            "title": "lightweight reaction meme",
            "url": "https://example.test/meme.png",
        },
        active_channels=["web", "meme"],
    )

    assert decision.shadow_selected_source_type == "meme"
    news_breakdown = next(
        values
        for key, values in decision.score_breakdown.items()
        if key.startswith("news:")
    )
    assert news_breakdown["source_type_adjustment"] == -0.05


def test_material_observation_uses_source_link_url_for_actual_rank():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("music", "meme"),
            source_weights={"music": 1.0, "meme": 0.1},
        ),
        phase1_topics=[
            ("music", "music summary"),
            ("meme", "meme summary"),
        ],
        selected_music_link={
            "title": "Kitchen Song",
            "artist": "Neko Band",
            "url": "https://example.test/music",
        },
        selected_meme_link={
            "title": "pan flip meme",
            "url": "https://example.test/meme.png",
        },
        active_channels=["music", "meme"],
    )

    observation = build_recommendation_observation(
        decision,
        action="chat",
        reason_code="CHAT_DELIVERED",
        stage="delivery",
        source_mode="music",
        source_tag="MUSIC",
        active_channels=["music", "meme"],
        source_links=[
            {
                "title": "Kitchen Song",
                "artist": "Neko Band",
                "url": "https://example.test/music",
            }
        ],
    )

    assert observation["decision_stage"] == "phase1_material"
    assert observation["actual_rank"] == 1
    assert observation["actual_candidate_score"] is not None
    assert observation["matched_actual_material"] is True


def test_material_observation_falls_back_to_source_when_url_is_missing():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("music",),
            source_weights={"music": 1.0},
        ),
        phase1_topics=[("music", "music summary")],
        selected_music_link={
            "title": "No URL Song",
            "artist": "Neko Band",
        },
        active_channels=["music"],
    )

    observation = build_recommendation_observation(
        decision,
        action="chat",
        reason_code="CHAT_DELIVERED",
        stage="delivery",
        source_mode="music",
        source_tag="MUSIC",
        active_channels=["music"],
        source_links=[{"title": "No URL Song", "artist": "Neko Band"}],
    )

    assert observation["actual_rank"] == 1
    assert observation["matched_actual_source"] is True
    assert observation["matched_actual_material"] is True


def test_material_observation_pass_does_not_assign_actual_rank():
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("meme",),
            source_weights={"meme": 1.0},
        ),
        phase1_topics=[("meme", "meme summary")],
        selected_meme_link={
            "title": "pan flip meme",
            "url": "https://example.test/meme.png",
        },
        active_channels=["meme"],
    )

    observation = build_recommendation_observation(
        decision,
        action="pass",
        reason_code="PASS_MODEL_PASS",
        stage="model_decision",
        source_mode="meme",
        source_tag="MEME",
        active_channels=["meme"],
        source_links=[{"url": "https://example.test/meme.png"}],
    )

    assert observation["delivered"] is False
    assert observation["actual_rank"] is None
    assert observation["matched_actual_material"] is False


def test_active_source_bias_maps_safe_sources_to_phase2_tags():
    expected = {
        "news": "WEB",
        "video": "WEB",
        "home": "WEB",
        "web": "WEB",
        "music": "MUSIC",
        "meme": "MEME",
    }

    for source_type, tag in expected.items():
        decision = _ranked_material_decision(
            _material_candidate(source_type, score=0.9),
            _material_candidate("meme" if source_type != "meme" else "music", score=0.2),
        )

        bias = build_active_source_bias(decision)

        assert source_type_to_phase2_tag(source_type) == tag
        assert bias.applied is True
        assert bias.preferred_source_type == source_type
        assert bias.preferred_source_tag == tag
        assert bias.preferred_candidate_id == f"{source_type}:1"


def test_active_source_bias_rejects_first_batch_unsupported_sources():
    for source_type in ("personal", "topic_hook", "vision", "window", "mini_game"):
        decision = _ranked_material_decision(
            _material_candidate(source_type, score=0.9),
            _material_candidate("music", score=0.2),
        )

        bias = build_active_source_bias(decision)

        assert bias.applied is False
        assert bias.preferred_source_type == source_type
        assert bias.fallback_reason == "unsupported_source"


def test_active_source_bias_requires_score_gap_and_material_link():
    small_gap = build_active_source_bias(
        _ranked_material_decision(
            _material_candidate("music", score=0.8),
            _material_candidate("meme", score=0.77),
        ),
        min_score_gap=0.05,
    )
    missing_link = build_active_source_bias(
        _ranked_material_decision(
            _material_candidate("music", score=0.9, url=""),
            _material_candidate("meme", score=0.2),
        )
    )

    assert small_gap.applied is False
    assert small_gap.fallback_reason == "score_gap_too_small"
    assert missing_link.applied is False
    assert missing_link.fallback_reason == "missing_material_link"


def test_active_source_bias_rejects_diversity_overuse():
    selected_meme_link = {
        "title": "same reaction meme",
        "url": "https://example.test/repeated-meme.png",
    }
    baseline = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("meme",),
            source_weights={"meme": 1.0},
        ),
        phase1_topics=[("meme", "meme summary")],
        selected_meme_link=selected_meme_link,
        active_channels=["meme"],
    )
    meme_id = baseline.selected_candidate.id
    decision = build_phase1_material_shadow_decision(
        ProactiveRecommendationContext(
            lanlan_name="neko",
            enabled_modes=("meme",),
            source_weights={"meme": 1.0},
            recent_shadow_sources=("meme", "meme"),
            recent_candidate_ids=(meme_id,),
        ),
        phase1_topics=[("meme", "meme summary")],
        selected_meme_link=selected_meme_link,
        active_channels=["meme"],
    )

    bias = build_active_source_bias(decision)

    assert bias.applied is False
    assert bias.preferred_source_type == "meme"
    assert bias.preferred_source_tag == "MEME"
    assert bias.fallback_reason == "diversity_overuse"


def test_reorder_phase1_topics_for_active_bias_only_moves_preferred_channel():
    bias = build_active_source_bias(
        _ranked_material_decision(
            _material_candidate("music", score=0.9),
            _material_candidate("meme", score=0.2),
        )
    )
    topics = [("web", "web topic"), ("meme", "meme topic"), ("music", "music topic")]

    reordered = reorder_phase1_topics_for_bias(topics, bias)

    assert reordered == [
        ("music", "music topic"),
        ("web", "web topic"),
        ("meme", "meme topic"),
    ]
    assert sorted(reordered) == sorted(topics)


def test_observation_records_active_bias_followed_or_overridden():
    decision = _ranked_material_decision(
        _material_candidate("music", score=0.9),
        _material_candidate("meme", score=0.2),
    )
    bias = build_active_source_bias(decision)

    followed = build_recommendation_observation(
        decision,
        recommendation_mode="active_source",
        active_bias=bias,
        action="chat",
        reason_code="CHAT_DELIVERED",
        source_mode="music",
        source_tag="MUSIC",
        source_links=[{"url": "https://example.test/item"}],
    )
    overridden = build_recommendation_observation(
        decision,
        recommendation_mode="active_source",
        active_bias=bias,
        action="chat",
        reason_code="CHAT_DELIVERED",
        source_mode="meme",
        source_tag="MEME",
    )

    assert followed["recommendation_mode"] == "active_source"
    assert followed["active_bias_applied"] is True
    assert followed["active_preferred_source_type"] == "music"
    assert followed["active_preferred_source_tag"] == "MUSIC"
    assert followed["active_model_followed_preference"] is True
    assert overridden["active_model_followed_preference"] is False


def test_observation_does_not_override_model_pass_for_active_bias():
    decision = _ranked_material_decision(
        _material_candidate("music", score=0.9),
        _material_candidate("meme", score=0.2),
    )

    observation = build_recommendation_observation(
        decision,
        recommendation_mode="active_source",
        active_bias=build_active_source_bias(decision),
        action="pass",
        reason_code="PASS_MODEL_PASS",
        source_tag="PASS",
    )

    assert observation["delivered"] is False
    assert observation["actual_reason_code"] == "PASS_MODEL_PASS"
    assert observation["active_bias_applied"] is True
    assert observation["active_model_followed_preference"] is False


def test_observation_active_fields_are_false_without_active_bias():
    decision = _ranked_material_decision(_material_candidate("music", score=0.9))

    observation = build_recommendation_observation(
        decision,
        recommendation_mode="shadow",
        action="chat",
        reason_code="CHAT_DELIVERED",
        source_mode="music",
        source_tag="MUSIC",
    )

    assert observation["recommendation_mode"] == "shadow"
    assert observation["active_bias_applied"] is False
    assert observation["active_preferred_source_type"] is None
    assert observation["active_model_followed_preference"] is False
