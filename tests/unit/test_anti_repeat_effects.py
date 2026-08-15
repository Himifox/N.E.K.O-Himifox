from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import memory.anti_repeat_effects as anti_repeat_effects
from memory.anti_repeat_effects import (
    AntiRepeatDecision,
    AntiRepeatEffectStore,
    RepeatSignature,
    build_repeat_signature,
)
from utils.character_memory import (
    delete_character_memory_storage,
    rename_character_memory_storage,
)


def _store(tmp_path) -> AntiRepeatEffectStore:
    store = AntiRepeatEffectStore()
    config_manager = MagicMock()
    config_manager.memory_dir = str(tmp_path)
    store._config_manager = config_manager
    return store


def test_build_repeat_signature_prefers_safe_detector_evidence():
    signature = build_repeat_signature(
        "我又想说我会一直陪着你的，请放心。",
        ["我会一直陪着你", "https://private.example/path"],
        language="zh-CN",
    )

    assert signature == RepeatSignature(
        phrase="我会一直陪着你",
        normalized_phrase="我会一直陪着你",
        language="zh-CN",
    )


@pytest.mark.parametrize(
    "fragment",
    ["https://example.test/private", "`secret_code()`", "{{PRIVATE_VALUE}}"],
)
def test_build_repeat_signature_rejects_protected_fragments(fragment):
    assert (
        build_repeat_signature(
            f"draft {fragment}",
            [fragment],
            language="en",
        )
        is None
    )


def test_decision_is_counted_once_even_with_multiple_reasons(tmp_path):
    store = _store(tmp_path)
    signature = RepeatSignature("quiet lantern", "quiet lantern", "en")
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25", "unanswered_repeat"),
            action="regenerate",
            outcome="blocked_after_regen_bm25",
            signature=signature,
            score_before=12.0,
            score_after=4.0,
        ),
        now=1_700_000_000.0,
    )

    result = store.query_effects("Neko", 30, now=1_700_000_000.0)
    assert result["totals"]["detected"] == 1
    assert result["totals"]["regen_triggered"] == 1
    assert result["totals"]["blocked_delivery"] == 1
    assert result["reason_counts"] == {
        "bm25": 1,
        "literal_similarity": 0,
        "unanswered_repeat": 1,
    }
    assert result["bm25"] == {
        "pair_count": 1,
        "average_before": 12.0,
        "average_after": 4.0,
        "reduction_ratio": 0.6667,
    }
    assert result["patterns"][0]["blocked_count"] == 1


def test_unattributed_decision_keeps_aggregate_without_text(tmp_path):
    store = _store(tmp_path)
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("literal_similarity",),
            action="block",
            outcome="blocked_initial",
        ),
        now=1_700_000_000.0,
    )

    result = store.query_effects("Neko", 7, now=1_700_000_000.0)
    assert result["totals"]["unattributed"] == 1
    assert result["patterns"] == []


def test_query_missing_store_does_not_create_character_directory(tmp_path):
    store = _store(tmp_path)
    result = store.query_effects("Missing", 30, now=1_700_000_000.0)

    assert result["source_available"] is False
    assert not (tmp_path / "Missing").exists()


def test_storage_contains_fragment_but_not_rejected_draft(tmp_path):
    store = _store(tmp_path)
    rejected_draft = "PRIVATE full rejected draft around quiet lantern and more context"
    signature = build_repeat_signature(
        rejected_draft,
        ["quiet lantern"],
        language="en",
    )
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="regenerate",
            outcome="regen_guard_passed",
            signature=signature,
        ),
        now=1_700_000_000.0,
    )

    payload = (tmp_path / "Neko" / "anti_repeat_effects.json").read_text(
        encoding="utf-8"
    )
    assert "quiet lantern" in payload
    assert rejected_draft not in payload
    assert "PRIVATE full rejected draft" not in payload
    assert json.loads(payload)["schema_version"] == "anti-repeat-effects/v1"


def test_query_rejects_unsupported_period(tmp_path):
    with pytest.raises(ValueError, match="effect days"):
        _store(tmp_path).query_effects("Neko", 14)


def _record_delivered_response(store, response_id, timestamp):
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="regenerate",
            outcome="regen_guard_passed",
            response_id=response_id,
        ),
        now=timestamp,
    )
    staged = store.stage_response_delivered("Neko", response_id, now=timestamp)
    store._flush_snapshot(*staged)


def test_response_query_availability_requires_a_link_in_requested_slice(tmp_path):
    store = _store(tmp_path)
    timestamp = 1_700_000_000.0
    _record_delivered_response(store, "outside-slice", timestamp)

    result = store.query_effects_for_responses(
        "Neko",
        ["requested-response"],
        25,
        now=timestamp,
    )

    assert result["source_available"] is False
    assert result["linked_message_count"] == 0
    assert result["totals"]["detected"] == 0


def test_response_query_prunes_expired_records_and_persists_snapshot(tmp_path):
    store = _store(tmp_path)
    timestamp = 1_700_000_000.0
    _record_delivered_response(store, "expired-response", timestamp)

    result = store.query_effects_for_responses(
        "Neko",
        ["expired-response"],
        100,
        now=timestamp + 121 * 24 * 60 * 60,
    )

    payload = json.loads(
        (tmp_path / "Neko" / "anti_repeat_effects.json").read_text(encoding="utf-8")
    )
    assert result["source_available"] is False
    assert result["linked_message_count"] == 0
    assert payload["response_buckets"] == {}


def test_evict_fences_old_snapshot_before_reusing_character_name(tmp_path):
    store = _store(tmp_path)
    old_snapshot = store.stage_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="block",
            outcome="blocked_initial",
        ),
        now=1_700_000_000.0,
    )

    store.evict_character("Neko")
    store._flush_snapshot(*old_snapshot)
    assert not (tmp_path / "Neko").exists()

    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("literal_similarity",),
            action="regenerate",
            outcome="regen_guard_passed",
        ),
        now=1_700_000_001.0,
    )
    result = store.query_effects("Neko", 30, now=1_700_000_001.0)
    assert result["totals"]["detected"] == 1
    assert result["reason_counts"]["bm25"] == 0


def test_character_storage_rename_and_delete_evict_effect_cache(
    tmp_path, monkeypatch
):
    config_manager = SimpleNamespace(
        memory_dir=str(tmp_path),
        project_memory_dir=None,
    )
    store = _store(tmp_path)
    monkeypatch.setattr(anti_repeat_effects, "_GLOBAL_STORE", store)
    store.record_decision(
        "Old",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="block",
            outcome="blocked_initial",
        ),
        now=1_700_000_000.0,
    )
    store.query_effects("New", 30, now=1_700_000_000.0)

    rename_character_memory_storage(config_manager, "Old", "New")
    assert "Old" not in store._cache
    assert "New" not in store._cache
    assert (tmp_path / "New" / "anti_repeat_effects.json").exists()

    store.query_effects("New", 30, now=1_700_000_000.0)
    assert "New" in store._cache
    delete_character_memory_storage(config_manager, "New")
    assert "New" not in store._cache
    assert not (tmp_path / "New").exists()
