from __future__ import annotations

import json
from contextlib import contextmanager
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
    evict_character_runtime_caches,
    rename_character_memory_storage,
    retire_character_runtime_caches,
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
    [
        "https://example.test/private",
        "intranet.example/private",
        "10.0.0.1/private",
        "localhost:8080/private",
        "`secret_code()`",
        "{{PRIVATE_VALUE}}",
        "<secret_key>",
    ],
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


@pytest.mark.parametrize(
    ("draft", "tokenized_fragment"),
    [
        ("visit https://secret.example/private now", "//secret"),
        ("visit intranet.example/private now", "example/private"),
        ("visit intranet.example/private now", "intranet"),
        ("run `secret_code()` now", "`secret_code"),
        ("do not expose <secret_key> now", "secret_key"),
        ("```python\nsecret_key = 1", "secret_key"),
        ("~~~python\nsecret_key = 1\n~~~", "secret_key"),
        ("intro\n\n    secret_key = value\noutro", "secret_key"),
    ],
)
def test_build_repeat_signature_rejects_fragments_tokenized_from_protected_spans(
    draft,
    tokenized_fragment,
):
    assert (
        build_repeat_signature(
            draft,
            [tokenized_fragment],
            language="en",
        )
        is None
    )


def test_build_repeat_signature_keeps_same_fragment_when_it_also_appears_in_prose():
    signature = build_repeat_signature(
        "run `secret_code()` then discuss secret_code in prose",
        ["secret_code"],
        language="en",
    )

    assert signature is not None
    assert signature.normalized_phrase == "secret_code"


@pytest.mark.parametrize(
    ("language", "draft"),
    [
        ("en", "quiet lantern"),
        ("zh-CN", "真的好想你"),
    ],
)
def test_build_repeat_signature_never_retains_a_complete_short_draft(
    language,
    draft,
):
    assert (
        build_repeat_signature(
            draft,
            [draft],
            language=language,
            fallback_fragment=draft,
        )
        is None
    )


def test_build_repeat_signature_skips_full_fallback_but_keeps_shorter_evidence():
    signature = build_repeat_signature(
        "quiet lantern again",
        ["quiet lantern"],
        language="en",
        fallback_fragment="quiet lantern again",
    )

    assert signature == RepeatSignature(
        phrase="quiet lantern",
        normalized_phrase="quiet lantern",
        language="en",
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


def test_bm25_summary_preserves_increased_repetition_ratio(tmp_path):
    store = _store(tmp_path)
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="regular_prompt",
            reasons=("bm25",),
            action="regenerate",
            outcome="blocked_after_regen_bm25",
            score_before=9.0,
            score_after=15.0,
        ),
        now=1_700_000_000.0,
    )

    result = store.query_effects("Neko", 30, now=1_700_000_000.0)

    assert result["bm25"] == {
        "pair_count": 1,
        "average_before": 9.0,
        "average_after": 15.0,
        "reduction_ratio": -0.6667,
    }


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


def test_query_sanitizes_invalid_persisted_effect_values(tmp_path):
    effect_dir = tmp_path / "Neko"
    effect_dir.mkdir()
    (effect_dir / "anti_repeat_effects.json").write_text(
        json.dumps(
            {
                "version": 1,
                "schema_version": "anti-repeat-effects/v1",
                "started_at": "inf",
                "daily_buckets": {
                    "2023-11-14": {
                        "counters": {"detected": 1},
                        "bm25": {
                            "before_sum": "nan",
                            "after_sum": "inf",
                            "pair_count": 1,
                        },
                        "patterns": {
                            "pattern": {
                                "phrase": "quiet lantern",
                                "normalized_phrase": "quiet lantern",
                                "language": "en",
                                "detected_count": 1,
                                "last_seen_at": "-inf",
                            }
                        },
                    }
                },
                "response_buckets": {
                    "response": {
                        "created_at": 10**400,
                        "delivered_at": "nan",
                        "bucket": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _store(tmp_path).query_effects(
        "Neko",
        30,
        now=1_700_000_000.0,
    )

    assert result["started_at"] == 1_700_000_000.0
    assert result["bm25"] == {
        "pair_count": 1,
        "average_before": 0.0,
        "average_after": 0.0,
        "reduction_ratio": 0.0,
    }
    assert result["patterns"][0]["last_seen_at"] == 0.0
    json.dumps(result, allow_nan=False)


def test_query_derives_normalized_phrase_from_sanitized_phrase(tmp_path):
    effect_dir = tmp_path / "Neko"
    effect_dir.mkdir()
    (effect_dir / "anti_repeat_effects.json").write_text(
        json.dumps(
            {
                "version": 1,
                "schema_version": "anti-repeat-effects/v1",
                "started_at": 1_700_000_000.0,
                "daily_buckets": {
                    "2023-11-14": {
                        "patterns": {
                            "tampered": {
                                "phrase": "quiet lantern",
                                "normalized_phrase": "https://private.example/path",
                                "language": "en",
                                "detected_count": 1,
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _store(tmp_path).query_effects(
        "Neko",
        30,
        now=1_700_000_000.0,
    )

    assert result["patterns"] == [
        {
            "phrase": "quiet lantern",
            "normalized_phrase": "quiet lantern",
            "language": "en",
            "reasons": {},
            "detected_count": 1,
            "regen_triggered_count": 0,
            "regen_guard_passed_count": 0,
            "blocked_count": 0,
            "last_seen_at": 0.0,
        }
    ]


def test_query_sanitizes_overflowing_counter_without_resetting_other_history(tmp_path):
    effect_dir = tmp_path / "Neko"
    effect_dir.mkdir()
    (effect_dir / "anti_repeat_effects.json").write_text(
        '{"version":1,"schema_version":"anti-repeat-effects/v1",'
        '"started_at":1700000000,"daily_buckets":{"2023-11-14":{'
        '"counters":{"detected":1e400,"regen_triggered":2}}}}',
        encoding="utf-8",
    )

    result = _store(tmp_path).query_effects("Neko", 30, now=1_700_000_000.0)

    assert result["totals"]["detected"] == 0
    assert result["totals"]["regen_triggered"] == 2


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


def test_clear_effects_propagates_write_failure_and_restores_cached_history(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="block",
            outcome="blocked_initial",
        ),
        now=1_700_000_000.0,
    )
    monkeypatch.setattr(
        anti_repeat_effects,
        "atomic_write_json",
        MagicMock(side_effect=OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        store.clear_effects("Neko")

    result = store.query_effects("Neko", 30, now=1_700_000_000.0)
    assert result["totals"]["detected"] == 1


def test_effect_write_fence_runs_before_sidecar_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    write = MagicMock()
    monkeypatch.setattr(anti_repeat_effects, "atomic_write_json", write)

    @contextmanager
    def reject_write(*args, **kwargs):
        raise RuntimeError("maintenance")
        yield

    from utils import cloudsave_runtime

    monkeypatch.setattr(cloudsave_runtime, "cloudsave_writable_transaction", reject_write)

    with pytest.raises(RuntimeError, match="maintenance"):
        store._flush_snapshot(
            "Neko",
            {"version": 1},
            1,
            raise_on_error=True,
        )

    write.assert_not_called()
    assert not (tmp_path / "Neko").exists()


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


def test_query_prunes_future_dated_effect_records_and_persists_snapshot(tmp_path):
    store = _store(tmp_path)
    timestamp = 1_700_000_000.0
    future_timestamp = timestamp + 365 * 24 * 60 * 60
    current_day = anti_repeat_effects._utc_day(timestamp)
    future_day = anti_repeat_effects._utc_day(future_timestamp)
    current_response_key = anti_repeat_effects._response_key("current-response")
    future_response_key = anti_repeat_effects._response_key("future-response")
    effect_dir = tmp_path / "Neko"
    effect_dir.mkdir()
    (effect_dir / "anti_repeat_effects.json").write_text(
        json.dumps(
            {
                "version": 1,
                "daily_buckets": {
                    current_day: {"counters": {"detected": 1}},
                    future_day: {"counters": {"detected": 99}},
                },
                "response_buckets": {
                    current_response_key: {
                        "created_at": timestamp,
                        "delivered_at": timestamp,
                        "bucket": {"counters": {"detected": 1}},
                    },
                    future_response_key: {
                        "created_at": future_timestamp,
                        "delivered_at": future_timestamp,
                        "bucket": {"counters": {"detected": 99}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = store.query_effects("Neko", 30, now=timestamp)
    persisted = json.loads(
        (effect_dir / "anti_repeat_effects.json").read_text(encoding="utf-8")
    )

    assert result["totals"]["detected"] == 1
    assert set(persisted["daily_buckets"]) == {current_day}
    assert set(persisted["response_buckets"]) == {current_response_key}


def test_response_cap_evicts_undelivered_before_delivered(tmp_path, monkeypatch):
    monkeypatch.setattr(anti_repeat_effects, "MAX_RESPONSE_BUCKETS", 2)
    store = _store(tmp_path)
    _record_delivered_response(store, "delivered", 1_700_000_000.0)

    for offset, response_id in enumerate(("blocked-old", "blocked-new"), start=1):
        store.record_decision(
            "Neko",
            AntiRepeatDecision(
                source="proactive",
                reasons=("bm25",),
                action="block",
                outcome="blocked_initial",
                response_id=response_id,
            ),
            now=1_700_000_000.0 + offset,
        )

    result = store.query_effects_for_responses(
        "Neko",
        ["delivered"],
        100,
        now=1_700_000_003.0,
    )

    assert result["linked_message_count"] == 1
    assert len(store._cache["Neko"]["response_buckets"]) == 2


def test_response_load_cap_preserves_delivered_bucket(monkeypatch):
    monkeypatch.setattr(anti_repeat_effects, "MAX_RESPONSE_BUCKETS", 2)
    bucket = {"counters": {"detected": 1}}
    payload = {
        "version": 1,
        "response_buckets": {
            "delivered": {
                "created_at": 1.0,
                "delivered_at": 1.0,
                "bucket": bucket,
            },
            "blocked-old": {
                "created_at": 2.0,
                "delivered_at": 0.0,
                "bucket": bucket,
            },
            "blocked-new": {
                "created_at": 3.0,
                "delivered_at": 0.0,
                "bucket": bucket,
            },
        },
    }

    normalized = AntiRepeatEffectStore._normalize_payload(payload, now=3.0)

    assert set(normalized["response_buckets"]) == {"delivered", "blocked-new"}


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

    store.retire_character("Neko")
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


def _decision(outcome: str = "blocked_initial", **kwargs) -> AntiRepeatDecision:
    return AntiRepeatDecision(
        source="proactive",
        reasons=("bm25",),
        action="block",
        outcome=outcome,
        **kwargs,
    )


def test_capacity_eviction_keeps_the_bucket_the_current_turn_just_created(tmp_path):
    """A full, all-delivered store must not swallow the in-flight response bucket.

    Capacity eviction prefers to keep DELIVERED buckets, so a freshly created one
    (``delivered_at == 0``) sorts ahead of every delivered bucket. Once the store
    reaches steady state — delivery converts buckets, blocked ones are evicted
    first — each turn deleted the very bucket it had just created, and
    ``stage_response_delivered`` could never find it again.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0

    for index in range(anti_repeat_effects.MAX_RESPONSE_BUCKETS):
        response_id = f"old-turn-{index}"
        store.record_decision("Neko", _decision(response_id=response_id), now=now)
        store._flush_snapshot(
            *store.stage_response_delivered("Neko", response_id, now=now)
        )

    payload = store._cache["Neko"]
    assert len(payload["response_buckets"]) == anti_repeat_effects.MAX_RESPONSE_BUCKETS
    assert all(
        bucket["delivered_at"] > 0 for bucket in payload["response_buckets"].values()
    )

    store.record_decision("Neko", _decision(response_id="fresh-turn"), now=now + 1)
    assert store.stage_response_delivered("Neko", "fresh-turn", now=now + 2) is not None

    linked = store.query_effects_for_responses(
        "Neko", ["fresh-turn"], 100, now=now + 3
    )
    assert linked["source_available"] is True
    assert linked["linked_message_count"] == 1
    assert linked["totals"]["detected"] == 1


def test_delivery_mark_survives_a_publication_timestamp_in_the_past(tmp_path):
    """``mark_anti_repeat_response_delivered`` passes the publication instant.

    That instant is already in the past when the mark runs, so pruning with it
    must not treat the bucket being marked as future-dated.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0

    store.record_decision("Neko", _decision(response_id="turn"), now=now + 10)
    staged = store.stage_response_delivered("Neko", "turn", now=now)

    assert staged is not None
    linked = store.query_effects_for_responses("Neko", ["turn"], 100, now=now + 20)
    assert linked["linked_message_count"] == 1


def test_staged_snapshot_shares_no_mutable_state_with_the_live_cache(tmp_path):
    """The snapshot is serialized on a worker thread that does not hold the
    per-name lock, so sharing any sub-dict with the cache would let
    ``atomic_write_json`` iterate a dict another turn is writing to."""
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    signature = RepeatSignature("quiet lantern", "quiet lantern", "en")
    store.record_decision(
        "Neko", _decision(signature=signature, response_id="turn"), now=now
    )

    _name, snapshot, _seq = store.stage_decision(
        "Neko", _decision(signature=signature, response_id="turn"), now=now
    )
    live = store._cache["Neko"]
    day = anti_repeat_effects._utc_day(now)

    assert snapshot is not live
    assert snapshot["daily_buckets"] is not live["daily_buckets"]
    assert snapshot["daily_buckets"][day] is not live["daily_buckets"][day]
    assert (
        snapshot["daily_buckets"][day]["counters"]
        is not live["daily_buckets"][day]["counters"]
    )
    live_patterns = live["daily_buckets"][day]["patterns"]
    pattern_id = next(iter(live_patterns))
    assert snapshot["daily_buckets"][day]["patterns"] is not live_patterns
    assert (
        snapshot["daily_buckets"][day]["patterns"][pattern_id]["reasons"]
        is not live_patterns[pattern_id]["reasons"]
    )
    response_key = next(iter(live["response_buckets"]))
    assert (
        snapshot["response_buckets"][response_key]["bucket"]
        is not live["response_buckets"][response_key]["bucket"]
    )

    expected = json.loads(json.dumps(live, ensure_ascii=False))
    assert snapshot == expected

    # Mutating the cache the way a later turn would must not reach the snapshot.
    store.record_decision("Neko", _decision(response_id="turn"), now=now)
    assert snapshot["daily_buckets"][day]["counters"]["detected"] == 2


def test_decision_after_eviction_does_not_recreate_a_removed_character_dir(tmp_path):
    """``retire_character`` fences snapshots staged before it, not after it.

    Without the retirement guard, a decision recorded while delete/rename was
    still in flight went through ``ensure_character_dir`` and made the directory
    the caller had just removed reappear.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    store.record_decision("Neko", _decision(), now=now)
    assert (tmp_path / "Neko" / "anti_repeat_effects.json").exists()

    import shutil

    store.retire_character("Neko")
    shutil.rmtree(tmp_path / "Neko")

    store.record_decision("Neko", _decision(), now=now + 1)

    assert not (tmp_path / "Neko").exists()


def test_a_retired_name_writes_again_only_once_a_directory_exists(tmp_path):
    """Retirement outlives the directory, but it never blocks a live one.

    Exercises the real delete-then-recreate order: retire, remove the tree, then
    let another writer create the directory again. The store must refuse while
    the directory is gone and resume once it exists — without ever creating it
    itself, and without un-retiring the name. Directory existence cannot be
    treated as proof the identity is live, because
    ``delete_character_memory_storage`` retires BEFORE it removes the tree, so a
    flush landing in that window would otherwise disarm the guard permanently.
    Only ``evict_character`` -- the explicit live-identity event -- lifts it.
    """
    import shutil

    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0

    store.retire_character("Neko")

    # Still inside the delete window: the doomed directory is present. Writing
    # here is harmless (rmtree wins), but retirement must NOT be lifted.
    store.record_decision("Neko", _decision(), now=now)
    assert "Neko" in store._retired

    shutil.rmtree(tmp_path / "Neko")
    store.record_decision("Neko", _decision(), now=now + 1)
    assert not (tmp_path / "Neko").exists()

    # A sibling writer (or an explicit re-creation) brings the directory back.
    (tmp_path / "Neko").mkdir()
    store.record_decision("Neko", _decision(), now=now + 2)

    assert (tmp_path / "Neko" / "anti_repeat_effects.json").exists()
    assert "Neko" in store._retired


def test_queries_do_not_hand_live_cache_buckets_to_the_summarizer(tmp_path):
    """Summarizing happens after the per-name lock is released.

    Handing out the live bucket dicts lets a concurrent decision add pattern
    keys while `_summarize_effect_buckets` iterates them — a "dictionary changed
    size during iteration" away, and torn counters short of that. The staging
    path already refuses to share sub-dicts for exactly this reason.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    signature = RepeatSignature("quiet lantern", "quiet lantern", "en")
    store.record_decision(
        "Neko", _decision(signature=signature, response_id="turn"), now=now
    )
    store._flush_snapshot(*store.stage_response_delivered("Neko", "turn", now=now))

    live_day = store._cache["Neko"]["daily_buckets"]
    live_responses = store._cache["Neko"]["response_buckets"]
    handed_out = []
    original = anti_repeat_effects._summarize_effect_buckets

    def _spy(buckets):
        buckets = list(buckets)
        handed_out.extend(buckets)
        return original(buckets)

    anti_repeat_effects._summarize_effect_buckets = _spy
    try:
        day_result = store.query_effects("Neko", 30, now=now)
        response_result = store.query_effects_for_responses(
            "Neko", ["turn"], 100, now=now
        )
    finally:
        anti_repeat_effects._summarize_effect_buckets = original

    assert handed_out, "the summarizer should have received buckets"
    live_objects = list(live_day.values()) + [
        entry["bucket"] for entry in live_responses.values()
    ]
    for bucket in handed_out:
        assert not any(bucket is live for live in live_objects)

    # A later decision must not retroactively change an already-returned result.
    store.record_decision("Neko", _decision(response_id="turn"), now=now)
    assert day_result["totals"]["detected"] == 1
    assert response_result["totals"]["detected"] == 1


def test_availability_reflects_in_period_buckets_not_file_existence(tmp_path):
    """`clear_effects` leaves an empty payload on disk.

    Reporting availability from file existence made the panel skip its "no
    records for this period" state and render a row of zeros right after the
    user cleared the statistics. A nonempty sidecar whose buckets all fall
    outside the requested window had the same problem.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    store.record_decision("Neko", _decision(), now=now)

    assert store.query_effects("Neko", 30, now=now)["source_available"] is True

    store.clear_effects("Neko")
    assert (tmp_path / "Neko" / "anti_repeat_effects.json").exists()
    assert store.query_effects("Neko", 30, now=now)["source_available"] is False

    # Buckets outside the requested window are equally unavailable.
    store.record_decision("Neko", _decision(), now=now)
    much_later = now + 60 * 24 * 60 * 60
    assert store.query_effects("Neko", 7, now=much_later)["source_available"] is False


def test_clear_effects_does_not_hold_the_character_lock_across_the_fence(tmp_path):
    """Lock ORDER, not just correctness: fence first, character lock second.

    A cloud import holds the cloud-apply fence for its whole duration and takes
    the character lock inside it (to evict caches). If a reset takes the
    character lock and then reaches for the fence, the two deadlock. This pins
    the order by proving the character lock is free while the flush is inside
    the transaction.
    """
    import threading

    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    store.record_decision("Neko", _decision(), now=1_700_000_000.0)

    observed: list[bool] = []
    original = anti_repeat_effects.AntiRepeatEffectStore._flush_snapshot

    def _flush_and_probe(self, *args, **kwargs):
        # Stands in for the point where the real flush enters the cloud-save
        # transaction: another thread must be able to take the character lock.
        lock = self._get_lock("Neko")
        acquired = lock.acquire(timeout=1.0)
        observed.append(acquired)
        if acquired:
            lock.release()
        return original(self, *args, **kwargs)

    anti_repeat_effects.AntiRepeatEffectStore._flush_snapshot = _flush_and_probe
    try:
        done = threading.Event()
        errors: list[BaseException] = []

        def run_clear():
            try:
                store.clear_effects("Neko")
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append(exc)
            done.set()

        worker = threading.Thread(target=run_clear)
        worker.start()
        worker.join(5)
    finally:
        anti_repeat_effects.AntiRepeatEffectStore._flush_snapshot = original

    assert done.is_set()
    assert errors == []
    assert observed == [True], "the character lock was still held during the flush"


def test_evicting_a_live_identity_does_not_retire_it(tmp_path):
    """A cloud-save import replaces the files of a LIVE character.

    Retiring it would deny it the lazy directory creation every sibling memory
    writer gets, so an imported profile that ships no managed memory files
    would never persist its aggregates while the character is in active use.
    """
    store = _store(tmp_path)
    now = 1_700_000_000.0

    store.evict_character("Neko")

    assert "Neko" not in store._retired
    store.record_decision("Neko", _decision(), now=now)
    assert (tmp_path / "Neko" / "anti_repeat_effects.json").exists()


def test_evicting_a_retired_name_brings_it_back(tmp_path):
    """Re-creating an identity is the explicit event that lifts retirement.

    A rename target and a cloud-save import both name a live character. Nothing
    else lifts it -- directory existence in particular does not, because the
    delete path retires while the doomed tree is still on disk.
    """
    import shutil

    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0

    store.retire_character("Neko")
    shutil.rmtree(tmp_path / "Neko")
    store.record_decision("Neko", _decision(), now=now)
    assert not (tmp_path / "Neko").exists()

    store.evict_character("Neko")

    assert "Neko" not in store._retired
    store.record_decision("Neko", _decision(), now=now + 1)
    assert (tmp_path / "Neko" / "anti_repeat_effects.json").exists()


def test_a_failed_reset_loses_nothing(tmp_path):
    """A reset that reports failure must lose NEITHER generation.

    Publishing the empty payload before the flush made a concurrent decision
    load it, mutate it in place and stage a newer snapshot built on the cut --
    so the racer made the reset durable even though the reset failed, taking
    the pre-reset aggregates and ``started_at`` with it while the endpoint
    reported failure. No rollback can undo that, because by then the racer has
    already written the cleared payload out. Keeping the pre-reset payload in
    the cache until the cut is durable inverts it: the concurrent decision
    builds on the OLD data, so both survive.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    # Distinct reasons: the pre-reset state and the concurrent decision land
    # on the SAME day bucket with the same counters, so only the reason tells
    # a preserved decision from a restored `previous`.
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="block",
            outcome="blocked_initial",
        ),
        now=now,
    )

    original_flush = store._flush_snapshot

    def _flush_then_fail(*args, **kwargs):
        # Runs after clear_effects released the character lock, exactly where a
        # proactive decision can land. Restore the real flush first so the
        # concurrent decision persists normally instead of recursing.
        store._flush_snapshot = original_flush
        store.record_decision(
            "Neko",
            AntiRepeatDecision(
                source="proactive",
                reasons=("literal_similarity",),
                action="block",
                outcome="blocked_initial",
            ),
            now=now + 1,
        )
        raise OSError("disk full")

    store._flush_snapshot = _flush_then_fail
    try:
        with pytest.raises(OSError):
            store.clear_effects("Neko")
    finally:
        store._flush_snapshot = original_flush

    day = anti_repeat_effects._utc_day(now + 1)
    reasons = store._cache["Neko"]["daily_buckets"][day]["reason_counts"]
    assert reasons.get("literal_similarity") == 1, (
        "the concurrent decision was lost with the failed reset"
    )
    assert reasons.get("bm25") == 1, (
        "the failed reset destroyed the pre-reset aggregates anyway"
    )
    assert store._cache["Neko"]["started_at"] == now, (
        "the failed reset still moved the statistics-since date"
    )

    # The file is the authority, and it must not carry the cut either.
    persisted = json.loads(
        (tmp_path / "Neko" / "anti_repeat_effects.json").read_text(encoding="utf-8")
    )
    assert persisted["daily_buckets"][day]["reason_counts"]["bm25"] == 1
    assert persisted["started_at"] == now


def test_runtime_cache_entry_points_split_live_from_removed(tmp_path):
    """The two entry points differ only in retirement, and must keep differing.

    A delete or a rename SOURCE is going away and retires. A cloud-save import
    or a rename TARGET is live and only invalidates. Collapsing them in either
    direction breaks one of the two: retiring a live name stops it persisting,
    and not retiring a removed one lets an in-flight decision recreate the
    directory that was just deleted.
    """
    previous = anti_repeat_effects._GLOBAL_STORE
    store = _store(tmp_path)
    anti_repeat_effects._GLOBAL_STORE = store
    try:
        store._cache["Neko"] = anti_repeat_effects._default_payload(1_700_000_000.0)
        retire_character_runtime_caches("Neko")
        assert "Neko" not in store._cache
        assert "Neko" in store._retired

        store._cache["Neko"] = anti_repeat_effects._default_payload(1_700_000_000.0)
        evict_character_runtime_caches("Neko")
        assert "Neko" not in store._cache
        assert "Neko" not in store._retired
    finally:
        anti_repeat_effects._GLOBAL_STORE = previous


_WRAPPED_TEMPLATE_DRAFTS = [
    ("jinja", "sure thing {{" + chr(10) + "secret helper phrase" + chr(10) + "}} enjoy"),
    ("shell", "sure thing ${" + chr(10) + "secret helper phrase" + chr(10) + "} enjoy"),
    ("erb", "sure thing <%" + chr(10) + "secret helper phrase" + chr(10) + "%> enjoy"),
    # Opener and closer on their own lines with a two-line body.
    ("jinja block", "ok {{" + chr(10) + "alpha" + chr(10) + "secret helper phrase" + chr(10) + "}} done"),
    ("shell block", "ok ${" + chr(10) + "alpha" + chr(10) + "secret helper phrase" + chr(10) + "} done"),
]


@pytest.mark.parametrize(
    "label, draft",
    _WRAPPED_TEMPLATE_DRAFTS,
    ids=[row[0] for row in _WRAPPED_TEMPLATE_DRAFTS],
)
def test_a_wrapped_template_body_never_reaches_the_sidecar(label, draft):
    """The single-line form of the same content already returned None.

    `_PROTECTED_RE` rejected newlines inside every template alternative, so a
    body that merely wrapped stayed searchable and detector evidence taken from
    inside it could be persisted -- the leak was triggered purely by a newline
    between the delimiters.
    """
    assert build_repeat_signature(
        draft, ["secret", "helper"], language="en"
    ) is None, label


def test_speech_around_stray_delimiters_still_yields_a_signature():
    """The template guard must not swallow ordinary character speech.

    The bounded form is what keeps this true: an unbounded newline-crossing
    match would treat the stray opener and the far-away closer as one container
    and protect everything between them, so no signature could ever be built
    for the catchphrase sitting in the middle.
    """
    # Four newlines apart: past the budget. At three the stray pair is
    # indistinguishable from a real template block and is deliberately
    # protected; what this pins is that the blast radius STOPS growing.
    draft = (
        "那个 ${" + chr(10)
        + "A呢" + chr(10)
        + "我们一起去吃饭吧" + chr(10)
        + "B呢" + chr(10)
        + "最后那个括号 }"
    )

    signature = build_repeat_signature(draft, ["我们一起去吃饭吧"], language="zh-CN")

    assert signature is not None
    assert signature.phrase == "我们一起去吃饭吧"


def test_a_failed_reset_does_not_resurrect_an_evicted_cache(tmp_path):
    """A cloud import evicting mid-reset must stay evicted.

    ``_evict_unlocked`` fences the sequence at ``max(staged, written)``, which
    equals the sequence the reset staged -- so a rollback guarded on "did the
    sequence advance" read "nothing newer" and wrote the pre-reset payload back
    into the cache the import had just dropped. The next decision then flushed
    that resurrected payload over the freshly imported file.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    store.record_decision("Neko", _decision(), now=now)

    original_flush = store._flush_snapshot

    def _evict_then_fail(*_args, **_kwargs):
        store._flush_snapshot = original_flush
        # What import_local_cloudsave_snapshot does under the cloud-apply
        # fence, right before it replaces memory/Neko/.
        store.evict_character("Neko")
        raise OSError("disk full")

    store._flush_snapshot = _evict_then_fail
    try:
        with pytest.raises(OSError):
            store.clear_effects("Neko")
    finally:
        store._flush_snapshot = original_flush

    assert "Neko" not in store._cache, (
        "the failed reset resurrected a cache entry the import had evicted"
    )


def test_a_reset_outrun_by_a_writer_cuts_again(tmp_path):
    """Publishing a cut the racer already overwrote would report a false success.

    A decision that stages AFTER the reset flushed writes the pre-reset payload
    plus its own delta at a higher sequence, so it lands after ours and the cut
    is not durable. Publishing regardless would leave the cache empty while the
    file still held the data -- the reset reporting success having cleared
    nothing.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    store.record_decision("Neko", _decision(), now=now)

    original_flush = store._flush_snapshot
    races = []

    def _flush_then_race(*args, **kwargs):
        original_flush(*args, **kwargs)
        if not races:
            races.append(True)
            # Stages seq+1 from the PRE-RESET payload and writes it out, so
            # the cut we just flushed is already gone from disk.
            store.record_decision("Neko", _decision(), now=now + 1)

    store._flush_snapshot = _flush_then_race
    try:
        store.clear_effects("Neko")
    finally:
        store._flush_snapshot = original_flush

    assert races, "the race never happened; the test proves nothing"
    assert store._cache["Neko"]["daily_buckets"] == {}
    persisted = json.loads(
        (tmp_path / "Neko" / "anti_repeat_effects.json").read_text(encoding="utf-8")
    )
    assert persisted["daily_buckets"] == {}, (
        "the reset reported success while the file still held the old data"
    )


def test_a_reset_abandons_when_the_identity_is_replaced(tmp_path):
    """An import landing mid-flush must not have its file cleared afterwards.

    Eviction fences the write sequence at exactly the value the reset staged,
    so the flush is silently skipped AND "did the sequence advance" reads no.
    Publishing on that basis put an empty payload into the cache the import had
    just dropped, and the next decision flushed it over the imported file --
    while the endpoint reported the reset had succeeded. Re-cutting would be
    worse still: it would clear data the reset never asked about.
    """
    store = _store(tmp_path)
    (tmp_path / "Neko").mkdir()
    now = 1_700_000_000.0
    day = anti_repeat_effects._utc_day(now)
    store.record_decision("Neko", _decision(), now=now)
    persisted_path = tmp_path / "Neko" / "anti_repeat_effects.json"

    original_flush = store._flush_snapshot

    def _import_then_flush(*args, **kwargs):
        store._flush_snapshot = original_flush
        # What import_local_cloudsave_snapshot does under the cloud-apply
        # fence: drop the cache, then replace memory/Neko/ wholesale.
        store.evict_character("Neko")
        imported = json.loads(persisted_path.read_text(encoding="utf-8"))
        imported["daily_buckets"][day]["reason_counts"]["bm25"] = 42
        persisted_path.write_text(
            json.dumps(imported, ensure_ascii=False), encoding="utf-8"
        )
        return original_flush(*args, **kwargs)

    store._flush_snapshot = _import_then_flush
    try:
        with pytest.raises(RuntimeError):
            store.clear_effects("Neko")
    finally:
        store._flush_snapshot = original_flush

    assert "Neko" not in store._cache, (
        "the reset republished a cut into the cache the import had evicted"
    )
    # A different reason, so the imported count stays exactly 42 and the
    # assertion cannot be satisfied by an increment that merely looks intact.
    store.record_decision(
        "Neko",
        AntiRepeatDecision(
            source="proactive",
            reasons=("literal_similarity",),
            action="block",
            outcome="blocked_initial",
        ),
        now=now + 1,
    )
    imported = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert imported["daily_buckets"][day]["reason_counts"]["bm25"] == 42, (
        "the imported file was clobbered by the abandoned reset"
    )
