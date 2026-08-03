from concurrent.futures import ThreadPoolExecutor
import json

from main_logic.proactive_recommendation.domain_models import (
    PendingRecommendationFeedback,
)
from main_logic.proactive_recommendation.feedback.service import (
    PendingFeedbackRegistry,
)
from main_logic.proactive_recommendation.persistence import AtomicJsonStore
from main_logic.proactive_recommendation.persistence import JsonlStore


def _sanitize_mapping(value):
    return dict(value) if isinstance(value, dict) else {}


def test_atomic_json_store_serializes_full_read_modify_write(tmp_path):
    store = AtomicJsonStore(
        tmp_path / "counter.json",
        default_factory=lambda: {"count": 0},
        sanitizer=_sanitize_mapping,
    )

    def increment(_):
        def mutate(state):
            state["count"] += 1

        store.update(mutate)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(increment, range(80)))

    assert store.read() == {"count": 80}


def test_jsonl_store_concurrent_append_keeps_every_record_valid(tmp_path):
    store = JsonlStore(tmp_path / "events.jsonl", sanitizer=_sanitize_mapping)

    def append(index):
        assert store.append({"index": index}, rotate_bytes=1024 * 1024)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(80)))

    rows = store.load()
    assert sorted(row["index"] for row in rows) == list(range(80))


def test_jsonl_rotation_and_append_share_one_lock(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"seed": "x" * 1024}) + "\n", encoding="utf-8")
    store = JsonlStore(path, sanitizer=_sanitize_mapping)

    def append(index):
        assert store.append({"index": index}, rotate_bytes=512)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(20)))

    rotated = path.with_name(path.name + ".1")
    assert rotated.exists()
    assert json.loads(rotated.read_text(encoding="utf-8"))["seed"].startswith("x")
    assert sorted(row["index"] for row in store.load()) == list(range(20))


def test_pending_feedback_claim_is_atomic_across_threads():
    registry = PendingFeedbackRegistry(reply_window_seconds=600)
    registry.register(
        PendingRecommendationFeedback(
            lanlan_name="neko",
            turn_id="turn-1",
            source_type="music",
            candidate_id="music:1",
        )
    )

    def claim(_):
        return registry.claim_event(
            "neko",
            "turn-1",
            event_type="music_hard_skip",
            state_group="source:music:music",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(claim, range(40)))

    assert sum(not claim.duplicate_event for claim in claims) == 1
    assert sum(not claim.duplicate_group for claim in claims) == 1
