from __future__ import annotations

import pytest

from plugin.server.application.messages import context_query_service as query_module
from plugin.server.messaging import plane_bridge


@pytest.mark.plugin_unit
def test_user_context_bridge_targets_memory_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(plane_bridge._bridge, "start", lambda: calls.append(("start", "", {})))
    monkeypatch.setattr(
        plane_bridge._bridge,
        "enqueue_delta",
        lambda *, store, topic, payload: calls.append((store, topic, payload)),
    )
    monkeypatch.setattr(plane_bridge.time, "time", lambda: 123.0)

    plane_bridge.publish_user_context_event(
        "lanlan",
        {"type": "user_message", "content": "hello"},
    )

    assert calls[0][0] == "start"
    assert calls[1] == (
        "memory",
        "lanlan",
        {"type": "user_message", "content": "hello", "_ts": 123.0},
    )


@pytest.mark.plugin_unit
def test_message_plane_user_context_unwraps_payload_and_applies_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query_module.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(
        query_module._message_plane_client,
        "request_sync",
        lambda **_: {
            "ok": True,
            "result": {
                "items": [
                    {
                        "ts": 9_999.0,
                        "payload": {
                            "type": "user_message",
                            "content": "recent",
                            "_ts": 9_999.0,
                        },
                    },
                    {
                        "ts": 1.0,
                        "payload": {
                            "type": "user_message",
                            "content": "expired",
                            "_ts": 1.0,
                        },
                    },
                ]
            },
        },
    )

    assert query_module._message_plane_user_context("default", 20) == [
        {"type": "user_message", "content": "recent", "_ts": 9_999.0}
    ]


@pytest.mark.plugin_unit
def test_user_context_query_merges_remote_and_local_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = {
        "type": "user_message",
        "content": "same",
        "lanlan": "YUI",
        "is_voice": False,
        "_ts": 2.0,
    }
    monkeypatch.setattr(
        query_module,
        "_message_plane_user_context",
        lambda bucket_id, limit: [duplicate, {"content": "remote", "_ts": 3.0}],
    )
    monkeypatch.setattr(
        query_module.state,
        "get_user_context",
        lambda *, bucket_id, limit: [{"content": "local", "_ts": 1.0}, duplicate],
    )

    history = query_module._get_user_context_sync("default", 10)

    assert [item["content"] for item in history] == ["local", "same", "remote"]
