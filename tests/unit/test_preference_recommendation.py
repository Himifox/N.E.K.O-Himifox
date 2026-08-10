import random
import sys
import time

import pytest

from config.prompts.prompts_proactive import build_unified_phase1_prompt
from main_logic.proactive_chat import candidate_selection, generation
from main_logic.proactive_chat.delivery import (
    _register_web_feedback_receipt_if_delivered,
)
from main_logic.proactive_chat.preference_recommendation import (
    CHANNEL_NAMES,
    PRIMARY_TOPICS,
    calculate_pool_probabilities,
    classify_candidate,
    clear_recommendation_feedback_state,
    format_feedback_receipts,
    get_pending_receipts,
    get_source_suppressions,
    get_topic_scores,
    process_recommendation_feedback,
    register_recommendation_receipt,
    run_demo,
    select_preference_candidates,
)
from main_logic.proactive_chat.state import (
    _source_hash,
    _source_skip_probability,
)


@pytest.fixture(autouse=True)
def _clear_state():
    clear_recommendation_feedback_state()
    yield
    clear_recommendation_feedback_state()


def _web(title, url, source="Example", mode="video"):
    return {"title": title, "url": url, "source": source, "mode": mode}


def _receipt(role, link, *, turn="turn-1", memory="主人 | 旧消息", now=0.0):
    receipt = register_recommendation_receipt(
        role,
        turn_id=turn,
        web_link=link,
        memory_context=memory,
        master_name="主人",
        now=now,
    )
    assert receipt is not None
    return receipt


def _feedback(role, receipt, reaction, evidence, memory, *, confidence=0.9, now=1.0, **extra):
    raw = {
        "receipt_id": receipt.receipt_id,
        "reaction": reaction,
        "confidence": confidence,
        "evidence": evidence,
        **extra,
    }
    return process_recommendation_feedback(
        role,
        raw,
        memory_context=memory,
        master_name="主人",
        now=now,
    )


def test_twenty_topics_do_not_overlap_channel_names():
    assert len(PRIMARY_TOPICS) == 20
    assert len(set(PRIMARY_TOPICS)) == 20
    assert set(PRIMARY_TOPICS).isdisjoint(CHANNEL_NAMES)


@pytest.mark.parametrize(
    ("title", "topic"),
    [
        ("人工智能大模型进展", "technology"),
        ("Python 开源项目", "programming"),
        ("新款手机与显卡", "digital_devices"),
        ("天文观测新发现", "science"),
        ("Steam 独立游戏", "games"),
        ("本季动画新作", "anime_comics"),
        ("乐队新专辑", "music_culture"),
        ("电影票房观察", "film_tv"),
        ("网络热搜梗", "internet_culture"),
        ("读书与教育课程", "books_education"),
        ("插画摄影展", "art_creative"),
        ("股票基金投资", "finance_business"),
        ("社会公共政策", "society"),
        ("足球世界杯", "sports"),
        ("新能源车试驾", "automotive"),
        ("健身营养计划", "health_fitness"),
        ("餐厅咖啡菜谱", "food_culture"),
        ("旅行酒店攻略", "travel_culture"),
        ("时尚穿搭护肤", "fashion_lifestyle"),
        ("猫咪宠物日常", "pets_animals"),
    ],
)
def test_classifier_covers_each_primary_topic(title, topic):
    assert classify_candidate({"title": title}).primary_topic == topic


def test_classifier_reads_only_existing_web_fields_and_is_deterministic():
    candidate = {
        "title": "无法分类的内容",
        "description_hint": "",
        "reason": "",
        "source": "Example",
        "url": "https://example.com/item",
        "mode": "games",
        "candidate_id": "python-game-ai",
        "topic": "games",
    }
    results = [classify_candidate(candidate).primary_topic for _ in range(10)]
    assert results == [None] * 10


def test_tie_uses_fixed_topic_order_and_returns_at_most_one_topic():
    result = classify_candidate({"title": "AI Python"})
    assert result.primary_topic == "technology"
    assert isinstance(result.primary_topic, str)


def test_empty_topic_candidates_keep_working():
    candidates = [_web("无法分类甲", "https://a.example/x"), _web("无法分类乙", "https://b.example/y")]
    selected = select_preference_candidates(candidates, {"topic.games": -1.0}, total=2, rng=random.Random(2))
    assert {item["title"] for item in selected} == {"无法分类甲", "无法分类乙"}


def test_only_an_actually_delivered_web_link_creates_a_receipt():
    selected = _web("Python桌宠开发记录", "https://example.com/item", "Bilibili")
    actual = _register_web_feedback_receipt_if_delivered(
        enabled=True,
        role="Neko",
        source_tag="WEB",
        proactive_sid="sid-1",
        selected_web_link=selected,
        source_links=[dict(selected)],
        memory_context="主人 | 旧消息",
        master_name="主人",
    )
    assert actual is not None
    assert actual.resource_key == _source_hash(selected["url"], selected["title"])
    assert _register_web_feedback_receipt_if_delivered(
        enabled=False,
        role="Disabled",
        source_tag="WEB",
        proactive_sid="sid-disabled",
        selected_web_link=selected,
        source_links=[dict(selected)],
        memory_context="主人 | 旧消息",
        master_name="主人",
    ) is None
    assert _register_web_feedback_receipt_if_delivered(
        enabled=True,
        role="Unmatched",
        source_tag="WEB",
        proactive_sid="sid-unmatched",
        selected_web_link=None,
        source_links=[],
        memory_context="主人 | 旧消息",
        master_name="主人",
    ) is None

    for source_tag, links in (
        ("WEB", [_web("别的标题", "https://other.example/item")]),
        ("CHAT", []),
        ("MUSIC", [dict(selected)]),
        ("MEME", [dict(selected)]),
    ):
        assert _register_web_feedback_receipt_if_delivered(
            enabled=True,
            role="Other",
            source_tag=source_tag,
            proactive_sid="sid-2",
            selected_web_link=selected,
            source_links=links,
            memory_context="主人 | 旧消息",
            master_name="主人",
        ) is None


def test_receipt_is_role_local_bounded_and_expires_after_two_hours():
    for index in range(12):
        _receipt(
            "Neko",
            _web(f"游戏 {index}", f"https://example.com/{index}"),
            turn=f"turn-{index}",
            now=float(index),
        )
    assert len(get_pending_receipts("Neko", now=12.0)) == 10
    assert get_pending_receipts("Other", now=12.0) == ()
    assert get_pending_receipts("Neko", now=2 * 3600 + 12.1) == ()


def test_feedback_accepts_only_user_words_added_after_delivery():
    link = _web("主机游戏攻略", "https://example.com/game")
    receipt = _receipt("Neko", link, memory="主人 | 这种游戏我没兴趣")
    rejected = _feedback(
        "Neko",
        receipt,
        "not_interested",
        "这种游戏我没兴趣",
        "主人 | 这种游戏我没兴趣",
    )
    assert rejected.accepted is False

    accepted = _feedback(
        "Neko",
        receipt,
        "not_interested",
        "这种游戏我没兴趣",
        "主人 | 这种游戏我没兴趣\n主人 | 这种游戏我没兴趣",
    )
    assert accepted.accepted is True


def test_low_confidence_and_expired_receipts_are_rejected():
    receipt = _receipt("Neko", _web("游戏攻略", "https://example.com/game"))
    assert not _feedback(
        "Neko", receipt, "not_interested", "我没兴趣", "主人 | 旧消息\n主人 | 我没兴趣", confidence=0.59
    ).accepted
    assert not _feedback(
        "Neko", receipt, "not_interested", "我没兴趣", "主人 | 旧消息\n主人 | 我没兴趣", now=7200.1
    ).accepted


def test_one_topic_evidence_does_not_change_probability():
    candidates = [
        _web("游戏攻略", "https://a.example/game"),
        _web("Python 教程", "https://b.example/python", mode="news"),
    ]
    before = calculate_pool_probabilities(candidates, {})
    receipt = _receipt("Neko", candidates[0])
    assert _feedback(
        "Neko", receipt, "not_interested", "不想看游戏", "主人 | 旧消息\n主人 | 不想看游戏"
    ).accepted
    assert get_topic_scores("Neko", now=1.0) == {}
    assert calculate_pool_probabilities(candidates, get_topic_scores("Neko", now=1.0)) == before


def test_two_distinct_resources_form_one_five_hour_topic_correction():
    first = _receipt("Neko", _web("游戏攻略", "https://a.example/game"), turn="a")
    _feedback("Neko", first, "not_interested", "不想看游戏", "主人 | 旧消息\n主人 | 不想看游戏", confidence=0.8, now=10.0)
    second = _receipt("Neko", _web("独立游戏", "https://b.example/game"), turn="b", memory="主人 | 不想看游戏", now=20.0)
    result = _feedback("Neko", second, "not_interested", "还是没兴趣", "主人 | 不想看游戏\n主人 | 还是没兴趣", confidence=1.0, now=30.0)
    assert result.state_changed is True
    assert get_topic_scores("Neko", now=30.0) == {"topic.games": pytest.approx(-0.9)}
    assert get_topic_scores("Neko", now=30.0 + 5 * 3600 + 0.1) == {}


def test_same_resource_cannot_satisfy_two_resource_threshold():
    link = _web("游戏攻略", "https://a.example/game")
    first = _receipt("Neko", link, turn="a")
    _feedback("Neko", first, "positive", "这个不错", "主人 | 旧消息\n主人 | 这个不错", now=10.0)
    second = _receipt("Neko", link, turn="b", memory="主人 | 这个不错", now=20.0)
    _feedback("Neko", second, "positive", "我还喜欢", "主人 | 这个不错\n主人 | 我还喜欢", now=30.0)
    assert get_topic_scores("Neko", now=30.0) == {}


@pytest.mark.parametrize("reaction", ["quality_issue", "temporary_skip", "unclear"])
def test_non_preference_reactions_do_not_pollute_topic_or_source_state(reaction):
    receipt = _receipt("Neko", _web("游戏攻略", "https://bad.example/game"))
    result = _feedback(
        "Neko", receipt, reaction, "这个反馈很明确", "主人 | 旧消息\n主人 | 这个反馈很明确"
    )
    assert result.accepted is True
    assert result.state_changed is False
    assert get_topic_scores("Neko", now=1.0) == {}
    assert get_source_suppressions("Neko", now=1.0) == frozenset()


def test_source_distrust_is_role_local_and_does_not_create_topic_state():
    receipt = _receipt("Neko", _web("游戏攻略", "https://bad.example/game"))
    result = _feedback(
        "Neko", receipt, "source_distrust", "我不信这个来源", "主人 | 旧消息\n主人 | 我不信这个来源"
    )
    assert result.state_changed is True
    assert get_source_suppressions("Neko", now=1.0) == frozenset({"bad.example"})
    assert get_source_suppressions("Other", now=1.0) == frozenset()
    assert get_topic_scores("Neko", now=1.0) == {}


def test_model_tag_fields_are_ignored_and_cannot_choose_state_topic():
    first = _receipt("Neko", _web("Python 教程", "https://a.example/python"), turn="a")
    _feedback(
        "Neko", first, "positive", "我喜欢这个", "主人 | 旧消息\n主人 | 我喜欢这个",
        now=10.0, topic="games", channel="music", user_tags=["games"],
    )
    second = _receipt("Neko", _web("开源代码", "https://b.example/code"), turn="b", memory="主人 | 我喜欢这个", now=20.0)
    _feedback(
        "Neko", second, "positive", "这个也喜欢", "主人 | 我喜欢这个\n主人 | 这个也喜欢",
        now=30.0, topic="games", channel="music", label="long_term",
    )
    assert set(get_topic_scores("Neko", now=30.0)) == {"topic.programming"}


def test_source_suppression_and_existing_dedupe_happen_before_exploration(monkeypatch):
    bad = _web("游戏 A", "https://bad.example/a", source="Bad")
    duplicate = _web("游戏 B", "https://dup.example/b", source="Dup")
    safe = _web("Python C", "https://safe.example/c", source="Safe", mode="news")
    now = time.time()
    receipt = _receipt("Neko", bad, now=now)
    _feedback(
        "Neko", receipt, "source_distrust", "不信这个网站",
        "主人 | 旧消息\n主人 | 不信这个网站", now=now + 1,
    )
    duplicate_key = _source_hash(duplicate["url"], duplicate["title"])
    monkeypatch.setattr(candidate_selection, "_should_skip_source", lambda key: key == duplicate_key)
    selection = candidate_selection._preference_weighted_phase1_pool(
        ["video", "news"],
        {"video": {"links": [bad, duplicate]}, "news": {"links": [safe]}},
        role="Neko",
        total=3,
        preference_scores={"topic.games": -1.0},
        source_weights={"video": 0.5, "news": 0.5},
        include_music=True,
        include_meme=False,
        rng=random.Random(1),
    )
    selected_urls = {item["url"] for links in selection.links_by_mode.values() for item in links}
    assert bad["url"] not in selected_urls
    assert duplicate["url"] not in selected_urls
    assert selected_urls <= {safe["url"]}


def test_existing_five_hour_hard_dedupe_and_decay_formula_are_unchanged():
    assert _source_skip_probability(5 * 3600 - 0.1, 3 * 86400) == 1.0
    assert _source_skip_probability(5 * 3600, 3 * 86400) == 1.0
    assert _source_skip_probability(5 * 3600 + 3 * 86400, 3 * 86400) == pytest.approx(0.5)


def test_feedback_affects_only_the_phase_after_extraction():
    candidates = [
        _web("游戏 A", "https://a.example/game"),
        _web("游戏 B", "https://b.example/game"),
        _web("Python C", "https://c.example/python", mode="news"),
        _web("天文 D", "https://d.example/space", mode="news"),
    ]
    first = _receipt("Neko", candidates[0], turn="n")
    _feedback("Neko", first, "not_interested", "游戏没兴趣", "主人 | 旧消息\n主人 | 游戏没兴趣", now=10.0)
    second = _receipt("Neko", candidates[1], turn="n+1", memory="主人 | 游戏没兴趣", now=20.0)

    scores_used_by_extraction_phase = get_topic_scores("Neko", now=30.0)
    probabilities_in_extraction_phase = calculate_pool_probabilities(candidates, scores_used_by_extraction_phase)
    _feedback("Neko", second, "not_interested", "还是不想看", "主人 | 游戏没兴趣\n主人 | 还是不想看", now=30.0)
    probabilities_already_chosen = probabilities_in_extraction_phase
    probabilities_next_phase = calculate_pool_probabilities(candidates, get_topic_scores("Neko", now=31.0))

    game_pool = next(pool for pool in probabilities_next_phase if pool.startswith("topic.games"))
    assert scores_used_by_extraction_phase == {}
    assert probabilities_already_chosen[game_pool] > probabilities_next_phase[game_pool]


def test_music_and_meme_tasks_do_not_read_topic_scores():
    tasks = [
        {"title": "music recommendation task", "mode": "music", "_phase1_task": "music"},
        {"title": "meme recommendation task", "mode": "meme", "_phase1_task": "meme"},
    ]
    weights = {"music": 0.6, "meme": 0.4}
    assert calculate_pool_probabilities(tasks, {}, source_weights=weights) == calculate_pool_probabilities(
        tasks, {"topic.games": -1.0}, source_weights=weights
    )


def test_feedback_prompt_contract_is_consistent_in_every_language():
    receipt = _receipt("Neko", _web("游戏攻略", "https://example.com/game"))
    summary = format_feedback_receipts("Neko", now=1.0)
    assert receipt.receipt_id in summary
    for language in ("zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"):
        prompt = build_unified_phase1_prompt(
            language,
            merged_content="1. 游戏攻略",
            memory_context="主人 | 旧消息\n主人 | 不感兴趣",
            master_name="主人",
            feedback_enabled=True,
            feedback_receipts=summary,
        )
        assert "[RECOMMENDATION_FEEDBACK]" in prompt
        assert all(reaction in prompt for reaction in (
            "positive", "not_interested", "quality_issue", "source_distrust", "temporary_skip", "unclear"
        ))
        assert "dimension" not in prompt
        assert "long_term" not in prompt


def test_feature_disabled_keeps_feedback_out_of_prompt():
    prompt = build_unified_phase1_prompt(
        "zh",
        merged_content="1. 游戏攻略",
        memory_context="主人 | 不感兴趣",
        master_name="主人",
    )
    assert "[RECOMMENDATION_FEEDBACK]" not in prompt


def test_phase1_parser_accepts_only_one_feedback_object_and_redacts_it():
    raw = """[WEB] [PASS]
[RECOMMENDATION_FEEDBACK]
{"receipt_id":"rec-1","reaction":"not_interested","confidence":0.9,"evidence":"私密原话","topic":"games"}
"""
    parsed = generation._parse_unified_phase1_result(raw)
    assert parsed["recommendation_feedback"]["receipt_id"] == "rec-1"
    assert parsed["web_pass"] is True
    redacted = generation._redact_preference_section_for_log(raw)
    assert "私密原话" not in redacted
    assert "<redacted>" in redacted


@pytest.mark.asyncio
async def test_feedback_extraction_reuses_the_single_phase1_call(monkeypatch):
    calls = 0

    async def fake_llm_call(**_kwargs):
        nonlocal calls
        calls += 1
        return '[WEB] [PASS]\n[RECOMMENDATION_FEEDBACK]\n{"receipt_id":"rec-1","reaction":"unclear","confidence":0.7,"evidence":"不太确定"}'

    monkeypatch.setattr(generation, "_llm_call_with_retry", fake_llm_call)
    result = await generation._run_unified_phase1(
        model_config=generation.ProactiveModelConfig("model", None, "key", None),
        proactive_lang="zh",
        lanlan_name="Neko",
        master_name="主人",
        merged_web_content="1. 游戏攻略",
        memory_context="主人 | 不太确定",
        recent_chats_section="",
        has_music_task=False,
        has_meme_task=False,
        feedback_enabled=True,
        feedback_receipts='[{"receipt_id":"rec-1","title":"游戏攻略"}]',
    )
    assert calls == 1
    assert result["recommendation_feedback"]["reaction"] == "unclear"


def test_fixed_demo_closes_the_delayed_web_loop_without_new_llm_work():
    result = run_demo()
    assert result["new_llm_threads"] == 0
    assert result["new_llm_calls"] == 0
    assert result["scores_after_first_phase1"] == {}
    assert result["scores_after_second_phase1"]["topic.games"] < 0
    assert result["game_probability_after"] < result["game_probability_before"]
    assert sum("游戏" in title for title in result["top_k_after_correction"]) < sum(
        "游戏" in title for title in result["top_k_before_correction"]
    )
    assert result["music_meme_behavior_changed"] is False


def test_project_test_runtime_is_python_311():
    assert sys.version_info[:2] == (3, 11)
