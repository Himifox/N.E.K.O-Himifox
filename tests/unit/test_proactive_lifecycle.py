"""Focused tests for proactive-chat lifecycle finalization."""

from __future__ import annotations

from typing import Any

import pytest

from main_logic.proactive_chat import contracts, service


class _State:
    def __init__(self, *, fire_error: Exception | None = None) -> None:
        self.fire_error = fire_error
        self.events: list[Any] = []

    async def fire(self, event: Any) -> None:
        self.events.append(event)
        if self.fire_error is not None:
            raise self.fire_error


class _Manager:
    def __init__(
        self,
        *,
        fire_error: Exception | None = None,
        cooldown_error: Exception | None = None,
    ) -> None:
        self.state = _State(fire_error=fire_error)
        self.cooldown_error = cooldown_error
        self.cooldown_calls: list[dict[str, Any]] = []

    async def _focus_idle_cooldown(self, **kwargs: Any) -> None:
        self.cooldown_calls.append(kwargs)
        if self.cooldown_error is not None:
            raise self.cooldown_error


class _Log:
    def __init__(self) -> None:
        self.info_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.warning_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.debug_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.info_calls.append((args, kwargs))

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls.append((args, kwargs))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self.debug_calls.append((args, kwargs))


def _lifecycle(
    mgr: _Manager,
    *,
    log: _Log | None = None,
) -> service.ProactiveLifecycle:
    return service.ProactiveLifecycle(
        mgr,
        "PROACTIVE_DONE",
        "兰兰",
        log=log,
    )


@pytest.mark.asyncio
async def test_finalize_emits_done_once_and_copies_the_result_body() -> None:
    mgr = _Manager()
    lifecycle = _lifecycle(mgr)
    original_body = {
        "success": True,
        "action": "pass",
        "message": "本轮没有合适内容",
    }
    original = contracts.ProactiveChatResult(
        body=original_body,
        status_code=202,
    )

    first = await lifecycle.finalize(original)
    second = await lifecycle.finalize(original)

    assert mgr.state.events == ["PROACTIVE_DONE"]
    assert lifecycle.done_emitted is True
    assert first.status_code == second.status_code == 202
    assert (
        first.body
        == second.body
        == {
            "success": True,
            "action": "pass",
            "message": "本轮没有合适内容",
            "reason_code": contracts.PROACTIVE_REASON_PASS_UNSPECIFIED,
            "stage": contracts.PROACTIVE_STAGE_UNKNOWN,
            "next_schedule_fixed_mode": False,
        }
    )
    assert first.body is not original_body
    assert original_body == {
        "success": True,
        "action": "pass",
        "message": "本轮没有合适内容",
    }


@pytest.mark.asyncio
async def test_finalize_uses_setdefault_for_fixed_schedule_mode() -> None:
    injected = _lifecycle(_Manager())
    injected.set_fixed_schedule(True)

    injected_result = await injected.finalize(
        contracts.ProactiveChatResult(
            body={
                "success": True,
                "action": "chat",
                "message": "你好",
            }
        )
    )

    explicit = _lifecycle(_Manager())
    explicit.set_fixed_schedule(False)
    explicit_result = await explicit.finalize(
        contracts.ProactiveChatResult(
            body={
                "success": True,
                "action": "chat",
                "next_schedule_fixed_mode": "keep-me",
            }
        )
    )

    assert injected_result.body["next_schedule_fixed_mode"] is True
    assert explicit_result.body["next_schedule_fixed_mode"] == "keep-me"


@pytest.mark.asyncio
async def test_finalize_logs_non_chat_reason_at_info_level() -> None:
    log = _Log()
    lifecycle = _lifecycle(_Manager(), log=log)

    await lifecycle.finalize(
        contracts.ProactiveChatResult(
            body={
                "success": True,
                "action": "pass",
                "message": "用户仍在活跃",
            }
        )
    )

    assert len(log.info_calls) == 1
    args, kwargs = log.info_calls[0]
    assert kwargs == {}
    assert args[1:] == ("兰兰", "用户仍在活跃")
    assert "主动搭话本轮未发起" in args[0]


@pytest.mark.asyncio
async def test_finalize_does_not_log_a_delivered_chat_as_not_started() -> None:
    log = _Log()
    lifecycle = _lifecycle(_Manager(), log=log)

    await lifecycle.finalize(
        contracts.ProactiveChatResult(
            body={"success": True, "action": "chat", "message": "你好"}
        )
    )

    assert log.info_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_replied"),
    [("chat", True), ("pass", False)],
)
async def test_phase2_mark_enables_focus_cooldown_with_pinned_tokens(
    action: str,
    expected_replied: bool,
) -> None:
    mgr = _Manager()
    lifecycle = _lifecycle(mgr)
    snapshot = {
        "focus_episode_id": "episode-7",
        "focus_turn_count": 3,
    }
    lifecycle.mark_phase2(snapshot)
    snapshot["focus_episode_id"] = "later-episode"
    snapshot["focus_turn_count"] = 99

    await lifecycle.finalize(
        contracts.ProactiveChatResult(
            body={"success": True, "action": action, "message": "result"}
        )
    )

    assert mgr.cooldown_calls == [
        {
            "replied": expected_replied,
            "episode_token": "episode-7",
            "turn_token": 3,
        }
    ]


@pytest.mark.asyncio
async def test_finalize_does_not_apply_focus_cooldown_before_phase2() -> None:
    mgr = _Manager()
    lifecycle = _lifecycle(mgr)

    await lifecycle.finalize(
        contracts.ProactiveChatResult(
            body={"success": True, "action": "chat", "message": "你好"}
        )
    )

    assert mgr.cooldown_calls == []


@pytest.mark.asyncio
async def test_finalize_swallows_done_and_focus_cooldown_errors() -> None:
    mgr = _Manager(
        fire_error=RuntimeError("done failed"),
        cooldown_error=RuntimeError("cooldown failed"),
    )
    log = _Log()
    lifecycle = _lifecycle(mgr, log=log)
    lifecycle.mark_phase2({"focus_episode_id": "episode-1", "focus_turn_count": 8})

    result = await lifecycle.finalize(
        contracts.ProactiveChatResult(
            body={"success": True, "action": "chat", "message": "你好"}
        )
    )

    assert result.body["action"] == "chat"
    assert lifecycle.done_emitted is True
    assert mgr.state.events == ["PROACTIVE_DONE"]
    assert len(mgr.cooldown_calls) == 1
    assert len(log.warning_calls) == 1
    assert len(log.debug_calls) == 1


@pytest.mark.asyncio
async def test_safe_done_only_emits_done() -> None:
    mgr = _Manager()
    lifecycle = _lifecycle(mgr)
    lifecycle.set_fixed_schedule(True)
    lifecycle.mark_phase2({"focus_episode_id": "episode-2", "focus_turn_count": 5})

    await lifecycle.safe_done()

    assert lifecycle.done_emitted is True
    assert mgr.state.events == ["PROACTIVE_DONE"]
    assert mgr.cooldown_calls == []


@pytest.mark.asyncio
async def test_safe_done_after_finalize_does_not_repeat_done() -> None:
    mgr = _Manager()
    lifecycle = _lifecycle(mgr)

    await lifecycle.finalize(
        contracts.ProactiveChatResult(
            body={"success": True, "action": "chat", "message": "你好"}
        )
    )
    await lifecycle.safe_done()

    assert mgr.state.events == ["PROACTIVE_DONE"]
