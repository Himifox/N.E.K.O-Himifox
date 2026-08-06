import random

import pytest

from config.prompts.prompts_proactive import build_unified_phase1_prompt
from main_logic.proactive_chat import generation
from main_logic.proactive_chat import decisions
from main_logic.proactive_chat.preference_recommendation import (
    TAG_VALUES,
    PreferenceEvent,
    calculate_pool_probabilities,
    classify_candidate,
    clear_preference_profiles,
    get_preference_scores,
    run_demo,
    select_preference_candidates,
    update_preference_profile,
    validate_preference_events,
)


def _raw_event(**overrides):
    event = {
        "dimension": "domain",
        "value": "tech",
        "signal": "explicit_like",
        "polarity": 1,
        "confidence": 0.9,
        "scope": "long_term",
        "evidence": "我喜欢 AI 新闻",
    }
    event.update(overrides)
    return event


def test_taxonomy_has_three_orthogonal_dimensions():
    assert set(TAG_VALUES) == {"domain", "media", "context"}
    assert "companion" in TAG_VALUES["domain"]
    assert TAG_VALUES["media"] == ("news", "video", "music", "meme")


def test_phase1_parser_accepts_optional_preference_section():
    parsed = generation._parse_unified_phase1_result(
        """[WEB]
Topic: AI Agent updates
Source: GitHub
[MUSIC] [PASS]
[PREFERENCE]
[{"dimension":"domain","value":"tech"}]
"""
    )

    assert parsed["web"]["title"] == "AI Agent updates"
    assert parsed["music_pass"] is True
    assert parsed["preference_events"] == [
        {"dimension": "domain", "value": "tech"}
    ]


def test_old_phase1_output_remains_compatible():
    parsed = generation._parse_unified_phase1_result(
        "[MUSIC] personalized\n[MEME] [PASS]"
    )

    assert parsed["music_keyword"] == "personalized"
    assert parsed["meme_pass"] is True
    assert parsed["preference_events"] == []


def test_preference_events_must_match_a_user_history_line():
    memory = "主人 | 我喜欢 AI 新闻\n猫娘 | 我喜欢游戏新闻\n"

    accepted = validate_preference_events(
        [_raw_event()], memory_context=memory, master_name="主人", now=0.0
    )
    rejected = validate_preference_events(
        [_raw_event(evidence="我喜欢游戏新闻")],
        memory_context=memory,
        master_name="主人",
        now=0.0,
    )

    assert len(accepted) == 1
    assert rejected == ()


def test_invalid_or_unbounded_model_events_fail_open():
    memory = "主人 | 我喜欢 AI 新闻\n"

    assert validate_preference_events(
        [_raw_event(value="invented")],
        memory_context=memory,
        master_name="主人",
    ) == ()
    assert validate_preference_events(
        [_raw_event()] * 4,
        memory_context=memory,
        master_name="主人",
    ) == ()


def test_same_evidence_is_deduped_but_a_repeated_user_line_counts_again():
    clear_preference_profiles()
    first = validate_preference_events(
        [_raw_event()],
        memory_context="主人 | 我喜欢 AI 新闻\n",
        master_name="主人",
        now=0.0,
    )
    assert update_preference_profile("Neko", first, now=0.0) == 1
    assert update_preference_profile("Neko", first, now=0.0) == 0

    repeated = validate_preference_events(
        [_raw_event()],
        memory_context="主人 | 我喜欢 AI 新闻\n主人 | 我喜欢 AI 新闻\n",
        master_name="主人",
        now=1.0,
    )
    assert update_preference_profile("Neko", repeated, now=1.0) == 1
    assert get_preference_scores("Neko", now=1.0)["domain.tech"] > 8.9


def test_profile_applies_decay_and_session_expiry():
    clear_preference_profiles()
    events = (
        PreferenceEvent(
            "domain", "tech", "explicit_like", 1, 1.0, "long_term", 0.0,
            "long-term", None,
        ),
        PreferenceEvent(
            "context", "focus", "current_intent", 1, 1.0, "session", 0.0,
            "short-term", 3600.0,
        ),
    )
    update_preference_profile("Neko", events, now=0.0)

    scores = get_preference_scores("Neko", now=30 * 86400)

    assert scores["domain.tech"] == pytest.approx(2.5)
    assert "context.focus" not in scores


@pytest.mark.parametrize(
    ("title", "mode", "expected_domain", "expected_media"),
    [
        ("AI Agent 开源工具", "news", "tech", "news"),
        ("本季动画与 VTuber", "video", "acg", "video"),
        ("Steam 独立游戏", "video", "gaming", "video"),
        ("Live2D 桌宠制作", "video", "companion", "video"),
    ],
)
def test_candidate_rule_classifier(title, mode, expected_domain, expected_media):
    tags = classify_candidate({"title": title, "mode": mode})

    assert tags.domain == expected_domain
    assert tags.media == expected_media


def test_pool_probabilities_are_normalized_and_keep_exploration():
    candidates = [
        {"title": "AI Agent 新闻", "mode": "news"},
        {"title": "动画新作", "mode": "video"},
        {"title": "无法分类的内容", "mode": "news"},
    ]
    probabilities = calculate_pool_probabilities(
        candidates, {"domain.tech": 5.0, "media.news": 4.0}
    )

    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert probabilities["tech/news"] == max(probabilities.values())
    assert probabilities["unknown"] > 0.0


def test_preference_selection_changes_the_next_round_candidate_batch():
    candidates = [
        {"title": "动画新作", "mode": "video"},
        {"title": "周末宠物生活", "mode": "video"},
        {"title": "电影娱乐资讯", "mode": "news"},
        {"title": "AI Agent 新闻", "mode": "news"},
        {"title": "Python 开源项目", "mode": "news"},
    ]
    selected = select_preference_candidates(
        candidates,
        {"domain.tech": 8.0, "media.news": 4.0},
        total=3,
        rng=random.Random(3),
    )

    assert {item["title"] for item in selected} & {
        "AI Agent 新闻", "Python 开源项目"
    }


def test_preference_prompt_is_localized_and_bounded():
    for language in ("zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"):
        prompt = build_unified_phase1_prompt(
            language,
            merged_content="1. AI Agent news",
            memory_context="主人 | 我喜欢 AI 新闻",
            master_name="主人",
            preference_enabled=True,
            preference_summary="domain.tech=+4.50",
        )
        assert "[PREFERENCE]" in prompt
        assert "domain: tech" in prompt or "domain=tech" in prompt
        assert "domain.tech=+4.50" in prompt


def test_preference_prompt_is_absent_when_demo_is_disabled():
    prompt = build_unified_phase1_prompt(
        "zh",
        merged_content="1. AI Agent news",
        memory_context="主人 | 我喜欢 AI 新闻",
        master_name="主人",
    )

    assert "[PREFERENCE]" not in prompt


def test_preference_weighting_does_not_create_new_hard_suppression(monkeypatch):
    monkeypatch.setattr(
        decisions,
        "_compute_source_weights",
        lambda _name, _channels: {"news": 0.5, "video": 0.5},
    )
    selection = decisions._select_weighted_sources(
        "Neko",
        ["news", "video"],
        {"news": {}, "video": {}},
        has_reminiscence=False,
        preference_scores={"media.news": 10.0},
    )

    assert selection.weights["news"] > selection.weights["video"]
    assert selection.suppressed == set()


def test_phase1_log_redaction_removes_user_evidence():
    raw = '[WEB] [PASS]\n[PREFERENCE] [{"evidence":"private words"}]'

    redacted = generation._redact_preference_section_for_log(raw)

    assert "private words" not in redacted
    assert "[WEB] [PASS]" in redacted


@pytest.mark.asyncio
async def test_preference_extraction_reuses_the_single_phase1_call(monkeypatch):
    calls = 0

    async def fake_llm_call(**_kwargs):
        nonlocal calls
        calls += 1
        return "[WEB] [PASS]\n[PREFERENCE] []"

    monkeypatch.setattr(generation, "_llm_call_with_retry", fake_llm_call)
    result = await generation._run_unified_phase1(
        model_config=generation.ProactiveModelConfig("model", None, "key", None),
        proactive_lang="zh",
        lanlan_name="Neko",
        master_name="主人",
        merged_web_content="1. AI Agent news",
        memory_context="主人 | 我喜欢 AI 新闻",
        recent_chats_section="",
        has_music_task=False,
        has_meme_task=False,
        preference_enabled=True,
    )

    assert calls == 1
    assert result["preference_events"] == []


def test_executable_demo_closes_the_two_round_loop():
    result = run_demo()

    assert result["new_llm_threads"] == 0
    assert result["new_llm_calls"] == 0
    assert len(result["accepted_events"]) == 2
    assert result["round_1_candidates"] != result["round_2_candidates"]
    assert result["pool_probabilities"]["tech/news"] == max(
        result["pool_probabilities"].values()
    )
