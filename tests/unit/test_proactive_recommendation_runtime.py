from pathlib import Path

from main_logic.proactive_recommendation_runtime import RecommendationRuntimeState


def test_active_source_can_only_roll_back_to_shadow():
    runtime = RecommendationRuntimeState("active_source")

    initial = runtime.status()
    assert initial["configured_mode"] == "active_source"
    assert initial["effective_mode"] == "active_source"
    assert initial["runtime_activation_allowed"] is False
    assert initial["rollback_available"] is True

    result = runtime.rollback(reason="operator safety stop", now=123.5)

    assert result["applied"] is True
    assert result["previous_mode"] == "active_source"
    status = result["status"]
    assert status["effective_mode"] == "shadow"
    assert status["active_source_enabled"] is False
    assert status["rollback_available"] is False
    assert status["rollback_count"] == 1
    assert status["last_rollback_at"] == 123.5
    assert status["last_rollback_reason"] == "operator safety stop"
    assert status["restart_restores_configured_mode"] is True


def test_repeated_rollback_is_a_noop_and_never_promotes_mode():
    runtime = RecommendationRuntimeState("active_source")
    runtime.rollback(reason="first", now=1.0)

    result = runtime.rollback(reason="second", now=2.0)

    assert result["applied"] is False
    assert result["previous_mode"] == "shadow"
    assert result["status"]["effective_mode"] == "shadow"
    assert result["status"]["rollback_count"] == 1
    assert result["status"]["last_rollback_reason"] == "first"


def test_non_active_and_invalid_startup_modes_remain_safe():
    for startup_mode, expected in (("shadow", "shadow"), ("off", "off"), ("invalid", "shadow")):
        runtime = RecommendationRuntimeState(startup_mode)

        result = runtime.rollback(reason="must not activate", now=1.0)

        assert result["applied"] is False
        assert result["status"]["configured_mode"] == expected
        assert result["status"]["effective_mode"] == expected
        assert result["status"]["runtime_activation_allowed"] is False


def test_proactive_flow_snapshots_mode_and_wires_real_activity_state():
    source = (
        Path(__file__).parents[2]
        / "main_routers"
        / "system_router"
        / "proactive_chat_flow.py"
    ).read_text(encoding="utf-8")

    assert source.count("recommendation_mode = get_recommendation_runtime_mode()") == 1
    assert source.count(
        "activity_state=resolve_recommendation_activity_state(activity_snapshot)"
    ) == 2
    assert 'activity_state=str(getattr(activity_snapshot, "propensity"' not in source
