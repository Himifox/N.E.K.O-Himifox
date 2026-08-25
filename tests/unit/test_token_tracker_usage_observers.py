from __future__ import annotations

from utils.token_tracker.recording import (
    notify_usage_observers,
    register_usage_observer,
    unregister_usage_observer,
)


def test_usage_observer_registration_is_idempotent_and_removable() -> None:
    received: list[dict[str, object]] = []

    def observer(record: dict[str, object]) -> None:
        received.append(record)

    register_usage_observer(observer)
    register_usage_observer(observer)
    try:
        notify_usage_observers({"type": "proactive.phase1", "pt": 1})
    finally:
        unregister_usage_observer(observer)

    notify_usage_observers({"type": "proactive.phase2", "pt": 2})
    assert received == [{"type": "proactive.phase1", "pt": 1}]
