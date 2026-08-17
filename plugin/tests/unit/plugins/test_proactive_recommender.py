from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from datetime import datetime
from types import SimpleNamespace

# Keep these pure-logic tests runnable in an isolated Python 3.11 environment.
# The full suite imports the real plugin package from its parent conftest; the
# fallback namespaces below only avoid unrelated application import side effects.
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _name, _path in (
    ("plugin", _REPO_ROOT / "plugin"),
    ("plugin.plugins", _REPO_ROOT / "plugin" / "plugins"),
    (
        "plugin.plugins.proactive_recommender",
        _REPO_ROOT / "plugin" / "plugins" / "proactive_recommender",
    ),
):
    if _name not in sys.modules:
        _module = ModuleType(_name)
        _module.__path__ = [str(_path)]  # type: ignore[attr-defined]
        sys.modules[_name] = _module

from plugin.plugins.proactive_recommender.config import (
    RecommendationConfig,
    normalize_settings_update,
)
from plugin.plugins.proactive_recommender.feedback import (
    apply_feedback_to_profile,
    settle_history,
)
from plugin.plugins.proactive_recommender.gate import evaluate_gate, in_quiet_hours
from plugin.plugins.proactive_recommender.profile import (
    active_interests,
    apply_profile_updates,
    heuristic_updates,
    message_from_memory_record,
)
from plugin.plugins.proactive_recommender.prompting import (
    build_delivery_context,
    build_delivery_message,
    canonical_candidate_url,
)
from plugin.plugins.proactive_recommender.openbiliclaw_compat import (
    apply_behavior_event_batch,
    behavior_event_updates,
    event_fingerprint,
    infer_platform,
    normalize_timestamp,
)
from plugin.plugins.proactive_recommender.openbiliclaw_recommendations import (
    normalize_openbiliclaw_recommendations,
)
from plugin.plugins.proactive_recommender.ranking import (
    rank_candidates,
    was_previously_delivered,
)
from plugin.plugins.proactive_recommender.sources import (
    normalize_bilibili_results,
    normalize_web_results,
)


def test_config_is_safe_by_default() -> None:
    config = RecommendationConfig.from_mapping({})
    assert config.enabled is False
    assert config.shadow_mode is True
    assert config.bilibili is False
    assert config.openbiliclaw_enabled is False
    assert config.openbiliclaw_backend_port == 8420


def test_prior_delivery_is_matched_by_id_url_or_title() -> None:
    history = [
        {
            "candidate_id": "old-id",
            "url": "https://example.com/video",
            "title": "Same  Title",
        }
    ]

    assert was_previously_delivered({"id": "old-id"}, history)
    assert was_previously_delivered(
        {"id": "new-id", "url": "https://example.com/video"}, history
    )
    assert was_previously_delivered({"id": "new-id", "title": " same title "}, history)
    assert not was_previously_delivered({"id": "new-id", "title": "Different"}, history)


def test_hosted_ui_settings_are_strictly_validated() -> None:
    assert normalize_settings_update(
        {
            "enabled": True,
            "quiet_start": "23:00",
            "score_threshold": 0.8,
            "daily_limit": 3,
            "openbiliclaw_enabled": True,
            "openbiliclaw_port": 8421,
            "openbiliclaw_backend_port": 8420,
            "unknown": "ignored",
        }
    ) == {
        "enabled": True,
        "daily_limit": 3,
        "openbiliclaw_enabled": True,
        "openbiliclaw_port": 8421,
        "openbiliclaw_backend_port": 8420,
        "score_threshold": 0.8,
        "quiet_start": "23:00",
    }
    for invalid in (
        {"enabled": "false"},
        {"quiet_end": "9:00"},
        {"score_threshold": 1.1},
        {"daily_limit": 21},
        {"openbiliclaw_port": 80},
        {"openbiliclaw_backend_port": 80},
        {"unknown": True},
    ):
        try:
            normalize_settings_update(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid settings: {invalid}")


def test_memory_message_is_deduplicatable_without_persisting_extra_fields() -> None:
    record = SimpleNamespace(
        payload={
            "type": "user_message",
            "content": "我喜欢 Rust",
            "_ts": 12.0,
            "lanlan": "neko",
        }
    )
    first = message_from_memory_record(record)
    second = message_from_memory_record(record)
    assert first == second
    assert first and len(first["id"]) == 24
    assert (
        message_from_memory_record(SimpleNamespace(payload={"type": "other"})) is None
    )


def test_profile_merges_positive_and_negative_evidence() -> None:
    profile = apply_profile_updates(
        {},
        [
            {"topic": "Rust", "polarity": "positive", "confidence": 0.9},
            {"topic": "Rust", "polarity": "positive", "confidence": 0.8},
            {"topic": "剧透", "polarity": "negative", "confidence": 1.0},
        ],
        now=100.0,
    )
    interests = {item["name"].lower(): item for item in profile["interests"]}
    assert interests["rust"]["status"] == "active"
    assert interests["剧透"]["weight"] < 0
    assert [item["name"] for item in active_interests(profile)] == ["Rust"]
    assert any(
        item["polarity"] == "negative"
        for item in heuristic_updates([{"text": "不要推荐剧透内容"}])
    )


def test_openbiliclaw_events_become_deduplicatable_non_sensitive_updates() -> None:
    event = {
        "event_id": "producer-1",
        "type": "favorite",
        "url": "https://www.bilibili.com/video/BV1xx",
        "title": "Rust 异步编程教程",
        "timestamp": 1_765_000_000_000,
        "source_platform": "bilibili",
        "metadata": {"tags": ["Rust", "编程"], "content_id": "BV1xx"},
    }
    assert event_fingerprint(event) == event_fingerprint(event)
    assert infer_platform("", event["url"]) == "bilibili"
    assert normalize_timestamp(event["timestamp"], now=1_765_000_100.0) == 1_765_000_000.0
    updates = behavior_event_updates([event])
    assert any(item["topic"] == "rust" and item["polarity"] == "positive" for item in updates)

    sensitive = behavior_event_updates(
        [{**event, "event_id": "producer-2", "title": "我的身份证和Rust", "metadata": {}}]
    )
    assert all("身份证" not in item["topic"] for item in sensitive)

    state: dict[str, object] = {"profile": {"interests": []}}
    first = apply_behavior_event_batch(state, [event], now=1_765_000_100.0)
    second = apply_behavior_event_batch(state, [event], now=1_765_000_200.0)
    assert first == {"accepted": 1, "duplicates": 0, "rejected": []}
    assert second == {"accepted": 1, "duplicates": 1, "rejected": []}
    assert state["platform_events"]["accepted"] == 1  # type: ignore[index]
    assert state["platform_events"]["by_platform"] == {"bilibili": 1}  # type: ignore[index]
    serialized = json.dumps(state, ensure_ascii=False)
    assert "SESSDATA" not in serialized
    assert event["url"] not in serialized
    assert event["title"] not in serialized


def test_sources_are_normalized_to_stable_candidates() -> None:
    web = normalize_web_results(
        {"results": [{"title": "Rust 2026", "url": "https://e/x", "snippet": "news"}]},
        "Rust",
    )
    bili = normalize_bilibili_results(
        {
            "result": {
                "videos": [
                    {"bvid": "BV1xx", "title": "Rust 教程", "description": "intro"}
                ]
            }
        },
        "Rust",
    )
    assert web[0]["url"] == "https://e/x"
    assert bili[0]["url"] == "https://www.bilibili.com/video/BV1xx"
    assert len(web[0]["id"]) == 24


def test_openbiliclaw_recommendations_become_rankable_neko_candidates() -> None:
    candidates = normalize_openbiliclaw_recommendations(
        {
            "items": [
                {
                    "id": 42,
                    "item_key": "bilibili:BV1test",
                    "bvid": "BV1test",
                    "title": "Rust 异步运行时源码分析",
                    "expression": "你最近持续关注异步系统，这篇拆解会很对胃口。",
                    "topic_label": "Rust 异步",
                    "source_platform": "bilibili",
                }
            ]
        },
        now=100.0,
    )
    assert len(candidates) == 1
    assert candidates[0]["url"] == "https://www.bilibili.com/video/BV1test"
    assert candidates[0]["source"] == "openbiliclaw:bilibili"
    ranked = rank_candidates(candidates, [], [])
    assert ranked[0]["score"] >= 0.72
    assert ranked[0]["matched_interests"] == ["Rust 异步"]


def test_ranking_prefers_relevant_and_novel_content() -> None:
    candidates = [
        {
            "id": "1",
            "title": "Rust async 深度解析",
            "snippet": "Rust",
            "llm_quality": 0.8,
        },
        {"id": "2", "title": "无关内容", "snippet": "weather", "llm_quality": 0.8},
    ]
    ranked = rank_candidates(candidates, [{"name": "rust", "weight": 0.9}], [])
    assert ranked[0]["id"] == "1"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_gate_applies_quiet_hours_limits_and_privacy() -> None:
    config = RecommendationConfig.from_mapping(
        {
            "recommendation": {
                "enabled": True,
                "quiet_start": "23:00",
                "quiet_end": "09:00",
            }
        }
    )
    assert in_quiet_hours(datetime(2026, 1, 1, 1, 0), "23:00", "09:00")
    private = evaluate_gate(
        config=config,
        now=datetime(2026, 1, 1, 12, 0),
        history=[],
        proactive_enabled=True,
        privacy_state="private",
    )
    assert (private.allowed, private.reason) == (False, "private_foreground")
    recent = evaluate_gate(
        config=config,
        now=datetime(2026, 1, 1, 12, 0),
        history=[],
        proactive_enabled=True,
        last_user_message_at=datetime(2026, 1, 1, 11, 50).timestamp(),
    )
    assert (recent.allowed, recent.reason) == (False, "recent_user_activity")


def test_feedback_settles_pending_and_tunes_matching_interest() -> None:
    before = [
        {
            "candidate_id": "c1",
            "mode": "live",
            "outcome": "pending",
            "timestamp": 100.0,
            "matched_interests": ["rust"],
        }
    ]
    after = settle_history(
        before,
        [{"timestamp": 120.0, "text": "这个不错"}],
        now=120.0,
        reply_window_seconds=600,
        ignored_window_seconds=1800,
    )
    assert after[0]["outcome"] == "engaged"
    profile = apply_feedback_to_profile(
        {"interests": [{"name": "rust", "weight": 0.5}]}, before, after, now=120.0
    )
    assert profile["interests"][0]["weight"] > 0.5


def test_delivery_message_replaces_placeholders_with_one_canonical_url() -> None:
    candidate = {
        "title": "车迟国的宗教实验",
        "snippet": "从香火之战角度拆解宗教讽刺和五雷法考据。",
        "url": "https://www.bilibili.com/video/BV1twZ4YzEmv",
    }
    message = build_delivery_message(
        candidate,
        "是从香火之战角度拆解的深度分析～链接在这："
        "[这里放具体URL，因为要求里说要准确放一次]",
    )
    assert "这里放" not in message
    assert "具体URL" not in message
    assert message.count(candidate["url"]) == 1
    assert message.endswith(f"[打开内容]({candidate['url']})")

    context = build_delivery_context(candidate, message)
    assert "something you already said" in context
    assert message in context


def test_delivery_message_rejects_unsafe_url_and_strips_generated_urls() -> None:
    assert canonical_candidate_url({"url": "javascript:alert(1)"}) == ""
    assert build_delivery_message(
        {"title": "unsafe", "url": "https://user:pass@example.com/item"}
    ) == ""
    message = build_delivery_message(
        {"title": "safe", "url": "https://example.com/item"},
        "看看这个 https://attacker.example/tracker [错误链接](https://evil.example)",
    )
    assert "attacker.example" not in message
    assert "evil.example" not in message
    assert message.count("https://example.com/item") == 1


def test_manifest_and_push_message_use_supported_plugin_contract() -> None:
    plugin_dir = _REPO_ROOT / "plugin" / "plugins" / "proactive_recommender"
    manifest = tomllib.loads((plugin_dir / "plugin.toml").read_text(encoding="utf-8"))
    assert manifest["plugin"]["id"] == "proactive_recommender"
    assert manifest["plugin"]["version"] == "0.4.1"
    assert manifest["plugin"]["passive"] is True
    assert manifest["plugin_runtime"] == {"enabled": True, "auto_start": True}
    assert manifest["plugin"]["store"]["enabled"] is True
    assert manifest["plugin"]["ui"]["enabled"] is True
    panel = manifest["plugin"]["ui"]["panel"][0]
    assert panel["entry"] == "ui/panel.tsx"
    assert panel["context"] == "dashboard"
    assert panel["permissions"] == ["state:read", "action:call"]
    assert manifest["openbiliclaw"] == {
        "enabled": True,
        "port": 8421,
        "backend_port": 8420,
    }

    tree = ast.parse((plugin_dir / "__init__.py").read_text(encoding="utf-8"))
    push_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "push_message"
    ]
    assert len(push_calls) == 2
    calls_by_behavior = {
        ast.literal_eval(
            next(
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "ai_behavior"
            )
        ): {keyword.arg: keyword.value for keyword in call.keywords}
        for call in push_calls
    }
    visible = calls_by_behavior["blind"]
    assert ast.literal_eval(visible["visibility"]) == ["chat"]
    assert ast.literal_eval(visible["coalesce_key"]) == "proactive_recommender:content"
    context = calls_by_behavior["read"]
    assert ast.literal_eval(context["visibility"]) == []
    assert (
        ast.literal_eval(context["coalesce_key"])
        == "proactive_recommender:content-context"
    )

    panel_source = (plugin_dir / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert 'from "@neko/plugin-ui"' in panel_source
    assert "export default function ProactiveRecommenderPanel" in panel_source
    assert 'props.api.call("update_recommendation_settings"' in panel_source
    assert 't("panel.bridge.title")' in panel_source
    assert "Raw conversations" not in panel_source

    plugin_source = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    assert "await self.ctx.get_own_base_config" in plugin_source
    assert "await self.ctx.update_own_config" in plugin_source
    assert "await self.config.update" not in plugin_source

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for node in node.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    status_decorators = ast.unparse(functions["recommendation_status"])
    update_decorators = ast.unparse(functions["update_recommendation_settings"])
    assert "@ui.action" not in status_decorators
    assert "id='recommendation_status'" in status_decorators
    assert "@ui.action" in update_decorators
    assert "id='update_recommendation_settings'" in update_decorators


def test_all_locales_expose_the_same_plugin_and_entry_keys() -> None:
    i18n_dir = _REPO_ROOT / "plugin" / "plugins" / "proactive_recommender" / "i18n"
    locale_data = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in i18n_dir.glob("*.json")
    }
    assert set(locale_data) == {"zh-CN", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"}
    expected_keys = set(locale_data["zh-CN"])
    assert all(set(values) == expected_keys for values in locale_data.values())
