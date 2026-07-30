# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runtime adapter for user-initiated music playback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from main_logic.agent_event_bus import register_user_utterance_sink
from main_logic.music_requests import (
    MusicRequest,
    fetch_music_request,
    parse_explicit_user_music_request,
)
from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

_session_manager_getter: Callable[[str], Any | None] | None = None
_PLAYBACK_STATES = frozenset({"playing", "paused", "ended", "error"})
_REPLY_START_GRACE_SECONDS = 0.2
_REPLY_WAIT_TIMEOUT_SECONDS = 60.0
_REPLY_WAIT_POLL_SECONDS = 0.05


def register_music_session_manager_getter(
    getter: Callable[[str], Any | None],
) -> None:
    global _session_manager_getter
    _session_manager_getter = getter


def _on_user_utterance(bucket: str, event: dict[str, Any]) -> None:
    lanlan_name = str(event.get("lanlan") or "")
    if not lanlan_name or bucket != lanlan_name or _session_manager_getter is None:
        return
    manager = _session_manager_getter(lanlan_name)
    if manager is None:
        return
    request = parse_explicit_user_music_request(str(event.get("content") or ""))
    if request is None:
        return
    previous_task = getattr(manager, "_music_request_task", None)
    if previous_task is not None and not previous_task.done():
        previous_task.cancel()
    epoch = _next_music_request_epoch(manager)
    _enqueue_music_request_context(manager, request, epoch)
    manager._music_request_task = manager._fire_task(
        _execute_music_request(manager, request, epoch)
    )


def _next_music_request_epoch(manager: Any) -> int:
    epoch = int(getattr(manager, "_music_request_epoch", 0) or 0) + 1
    manager._music_request_epoch = epoch
    return epoch


def _is_current_music_request(manager: Any, epoch: int) -> bool:
    return int(getattr(manager, "_music_request_epoch", 0) or 0) == epoch


def _enqueue_music_request_context(
    manager: Any,
    request: MusicRequest,
    epoch: int,
) -> None:
    enqueue = getattr(manager, "enqueue_agent_callback", None)
    if not callable(enqueue):
        return
    query = _clean_playback_text(request.display_query, 160) or "用户指定的音乐"
    detail = (
        f"音乐模块已接管用户对「{query}」的明确播放请求，正在自动搜索并会在确认可播放后启动播放器。"
        "本轮请只简短表示正在处理；不要询问版本，不要声称已经开始播放，也不要再次调用音乐播放工具。"
    )
    enqueue(
        {
            "event": "agent_task_callback",
            "origin": "event",
            "task_id": f"music_request:{epoch}",
            "channel": "music_playback",
            "status": "in_progress",
            "success": True,
            "summary": detail,
            "detail": detail,
            "source_kind": "music",
            "source_name": "music_request",
            "delivery_mode": "passive",
            "priority": 10,
            "coalesce_key": (
                f"music-playback-state:{getattr(manager, 'lanlan_name', '')}"
            ),
            "metadata": {
                "context_type": "music_request_pending",
                "request_id": epoch,
            },
            "context_type": "music_request_pending",
        }
    )


def _reply_in_progress(manager: Any) -> bool:
    if getattr(manager, "_active_text_request_id", None):
        return True
    if bool(getattr(manager, "_voice_playback_active", False)):
        return True
    session = getattr(manager, "session", None)
    is_active_response = getattr(session, "is_active_response", None)
    if callable(is_active_response):
        try:
            return bool(is_active_response())
        except Exception:
            return False
    return False


async def _wait_for_current_reply(manager: Any, epoch: int) -> None:
    if not _reply_in_progress(manager):
        await asyncio.sleep(_REPLY_START_GRACE_SECONDS)
    deadline = asyncio.get_running_loop().time() + _REPLY_WAIT_TIMEOUT_SECONDS
    while _is_current_music_request(manager, epoch) and _reply_in_progress(manager):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_REPLY_WAIT_POLL_SECONDS, remaining))


def _clean_playback_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def handle_music_playback_state(manager: Any, event: dict[str, Any]) -> bool:
    """Feed a player-confirmed state into the existing callback delivery path."""
    state = _clean_playback_text(event.get("state"), 16).lower()
    if state not in _PLAYBACK_STATES:
        return False

    track = event.get("track")
    track = track if isinstance(track, dict) else {}
    name = _clean_playback_text(track.get("name"), 120)
    artist = _clean_playback_text(track.get("artist"), 120)
    playback_id = _clean_playback_text(event.get("playback_id"), 512)
    request_id = _clean_playback_text(event.get("request_id"), 64)
    source = _clean_playback_text(event.get("source"), 16).lower()
    event_key = (playback_id, request_id, state)
    if getattr(manager, "_music_playback_event_key", None) == event_key:
        return False
    manager._music_playback_event_key = event_key

    title = f"《{name}》" if name else "所选歌曲"
    by_artist = f"（{artist}）" if artist else ""
    facts = {
        "playing": f"播放器已确认开始播放{title}{by_artist}。",
        "paused": f"播放器当前已暂停{title}{by_artist}。",
        "ended": f"播放器已结束播放{title}{by_artist}。",
        "error": f"播放器未能正常播放{title}{by_artist}。",
    }
    detail = facts[state]
    acknowledge_key = (playback_id, request_id)
    should_respond = (
        state == "playing"
        and source == "user"
        and getattr(manager, "_music_playback_acknowledged_key", None)
        != acknowledge_key
    )
    if should_respond:
        manager._music_playback_acknowledged_key = acknowledge_key
        detail += " 请简短自然地确认已经开始播放，不要再次调用音乐播放工具。"

    callback = {
        "event": "agent_task_callback",
        "origin": "event",
        "task_id": playback_id or request_id or "music_playback",
        "channel": "music_playback",
        "status": "completed",
        "success": state != "error",
        "summary": detail,
        "detail": detail,
        "source_kind": "music",
        "source_name": "music_player",
        "delivery_mode": "proactive" if should_respond else "passive",
        "priority": 10,
        "coalesce_key": f"music-playback-state:{getattr(manager, 'lanlan_name', '')}",
        "metadata": {
            "context_type": "music_playback",
            "state": state,
            "playback_id": playback_id,
            "request_id": request_id,
        },
        "context_type": "music_playback",
    }

    if should_respond and callable(getattr(manager, "submit_proactive_callback", None)):
        manager.submit_proactive_callback(
            callback,
            priority=callback["priority"],
            coalesce_key=callback["coalesce_key"],
        )
        return True

    enqueue = getattr(manager, "enqueue_agent_callback", None)
    if not callable(enqueue):
        return False
    enqueue(callback)
    if should_respond:
        trigger = getattr(manager, "trigger_agent_callbacks", None)
        fire_task = getattr(manager, "_fire_task", None)
        if callable(trigger) and callable(fire_task):
            fire_task(trigger())
    return True


async def _execute_music_request(
    manager: Any,
    request: MusicRequest,
    epoch: int,
) -> dict:
    result = await fetch_music_request(
        request,
        limit=5,
        source_locale=getattr(manager, "user_language", None),
        include_failure=True,
    )
    if not _is_current_music_request(manager, epoch):
        return {"status": "superseded"}

    tracks = result.get("data", []) if result else []
    candidates = [
        {
            "name": track.get("name", ""),
            "artist": track.get("artist", ""),
            "url": track.get("url", ""),
            "cover": track.get("cover", ""),
        }
        for track in tracks
        if isinstance(track, dict) and track.get("url")
    ][:3]
    if not candidates:
        error_code = str((result or {}).get("error_code") or "track_not_found")
        await _send_music_request_failure(
            manager,
            request.display_query,
            error_code,
        )
        return {
            "status": "failed",
            "reason": error_code,
            "query": request.display_query,
        }

    payload = {
        "type": "music_play_candidates",
        "request_id": epoch,
        "tracks": candidates,
    }
    await _wait_for_current_reply(manager, epoch)
    if not _is_current_music_request(manager, epoch):
        return {"status": "superseded"}
    if await _push_music_payload(manager, payload):
        return {
            "status": "queued",
            "candidates": len(candidates),
        }
    return {"status": "playback_unavailable"}


async def _send_music_request_failure(
    manager: Any,
    query: str,
    error_code: str,
) -> None:
    await _push_music_payload(
        manager,
        {
            "type": "music_request_failed",
            "query": query,
            "error_code": error_code,
        },
    )


async def _push_music_payload(manager: Any, payload: dict[str, Any]) -> bool:
    try:
        websocket = getattr(manager, "websocket", None)
        if websocket is None or not hasattr(websocket, "send_json"):
            return False
        ws_state = getattr(websocket, "client_state", None)
        if ws_state is not None and ws_state != ws_state.CONNECTED:
            return False
        await websocket.send_json(payload)
        manager.sync_message_queue.put({"type": "json", "data": payload})
        return True
    except Exception as exc:
        logger.warning(
            "[%s] user music payload push failed: %s",
            getattr(manager, "lanlan_name", ""),
            exc,
        )
        return False


register_user_utterance_sink(_on_user_utterance)
