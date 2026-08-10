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
import math
from collections.abc import Callable
from typing import Any

from config.prompts.prompts_music import get_music_intent_tool_texts
from config.prompts.prompts_proactive import get_music_request_pending_prompt
from main_logic.agent_event_bus import register_user_utterance_sink
from main_logic.music_command_parser import (
    is_strict_music_cancellation,
    parse_strict_music_command,
)
from main_logic.proactive_delivery import DELIVERY_RETRACTED_KEY
from main_logic.music_requests import (
    MusicRequest,
    fetch_music_request,
    mark_music_request_query,
)
from main_logic.tool_calling import ToolDefinition, register_builtin_tool_factory
from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

_session_manager_getter: Callable[[str], Any | None] | None = None
_PLAYBACK_STATES = frozenset({"playing", "paused", "ended", "error"})
_PLAYBACK_FAILURE_REASONS = frozenset(
    {
        "load_timeout",
        "media_error",
        "missing_audio",
        "player_error",
        "track_too_long",
    }
)
_REPLY_START_GRACE_SECONDS = 1.0
_REPLY_WAIT_TIMEOUT_SECONDS = 5.0
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
    content = str(event.get("content") or "")
    request = parse_strict_music_command(content)
    cancellation = request is None and is_strict_music_cancellation(content)
    strict_handled = request is not None or cancellation
    turn_state = {
        "handled": strict_handled,
        "consumed": strict_handled,
    }
    for owner_key in ("request_id", "turn_id"):
        owner_value = event.get(owner_key)
        if owner_value is not None:
            turn_state[owner_key] = owner_value
    manager._music_intent_turn = turn_state
    if request is None:
        if cancellation:
            _cancel_music_request(manager)
        return
    _start_music_request(manager, request)


def _start_music_request(
    manager: Any,
    request: MusicRequest,
    *,
    enqueue_context: bool = True,
) -> bool:
    fire_task = getattr(manager, "_fire_task", None)
    if not callable(fire_task):
        return False
    previous_task = getattr(manager, "_music_request_task", None)
    if previous_task is not None and not previous_task.done():
        previous_task.cancel()
    epoch = _next_music_request_epoch(manager)
    if enqueue_context:
        _enqueue_music_request_context(manager, epoch)
    manager._music_request_task = fire_task(
        _execute_music_request(manager, request, epoch)
    )
    return True


def _cancel_music_request(manager: Any) -> int:
    previous_task = getattr(manager, "_music_request_task", None)
    if previous_task is not None and not previous_task.done():
        previous_task.cancel()
    epoch = _next_music_request_epoch(manager)
    pending_context = getattr(manager, "_music_request_pending_context", None)
    if isinstance(pending_context, dict):
        pending_context[DELIVERY_RETRACTED_KEY] = True
        manager._music_request_pending_context = None
    manager._music_playback_state = "stopped"
    fire_task = getattr(manager, "_fire_task", None)
    if callable(fire_task):
        fire_task(
            _push_music_payload(
                manager,
                {
                    "type": "music_request_cancelled",
                    "request_id": epoch,
                },
            )
        )
    return epoch


def _clean_music_intent_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= 120 else ""


def _music_request_from_intent(arguments: dict[str, Any]) -> MusicRequest | None:
    target_type = str(arguments.get("target_type") or "").strip().lower()
    song = _clean_music_intent_text(arguments.get("song"))
    artist = _clean_music_intent_text(arguments.get("artist"))
    playlist = _clean_music_intent_text(arguments.get("playlist"))
    query = _clean_music_intent_text(arguments.get("query"))
    if target_type == "song" and song:
        return MusicRequest(
            keyword=" ".join(part for part in (song, artist) if part),
            song_name=song,
            song_artist=artist,
        )
    if target_type == "artist" and artist:
        return MusicRequest(keyword=artist, song_artist=artist)
    if target_type == "playlist" and playlist:
        return MusicRequest(playlist_name=playlist)
    if target_type in {"liked", "daily"}:
        return MusicRequest(personalization_source=target_type)
    if target_type == "query" and query:
        return MusicRequest(keyword=query)
    if target_type == "generic":
        return MusicRequest()
    return None


def _has_active_music(manager: Any) -> bool:
    task = getattr(manager, "_music_request_task", None)
    if task is not None:
        try:
            if not task.done():
                return True
        except Exception:
            pass
    return getattr(manager, "_music_playback_state", "") in {"playing", "paused"}


def _music_tool_result(
    manager: Any,
    status: str,
    **details: str,
) -> dict[str, str]:
    result = {"status": status, **details}
    log = logger.info if status == "accepted" else logger.debug
    log(
        "[%s] music tool %s: %s",
        getattr(manager, "lanlan_name", "") or "unknown",
        status,
        details.get("action") or details.get("reason") or "unknown",
    )
    return result


async def handle_music_intent_tool(
    manager: Any,
    arguments: dict[str, Any],
) -> dict[str, str]:
    """Execute at most one model-reported music action for the latest user turn."""
    turn = getattr(manager, "_music_intent_turn", None)
    if not isinstance(turn, dict):
        return _music_tool_result(manager, "ignored", reason="no_current_user_turn")
    if turn.get("handled"):
        return _music_tool_result(
            manager, "ignored", reason="already_handled_by_strict_command"
        )
    if turn.get("consumed"):
        return _music_tool_result(
            manager, "ignored", reason="music_intent_already_reported"
        )

    if not isinstance(arguments, dict):
        return _music_tool_result(manager, "ignored", reason="invalid_arguments")
    action = str(arguments.get("action") or "").strip().lower()
    if action == "stop":
        if not _has_active_music(manager):
            return _music_tool_result(manager, "ignored", reason="no_active_music")
        _cancel_music_request(manager)
        turn["consumed"] = True
        return _music_tool_result(
            manager, "accepted", action="stop", playback_state="stopped"
        )
    if action != "play":
        return _music_tool_result(manager, "ignored", reason="invalid_action")

    request = _music_request_from_intent(arguments)
    if request is None:
        return _music_tool_result(manager, "ignored", reason="invalid_target")
    if not _start_music_request(manager, request, enqueue_context=False):
        return _music_tool_result(manager, "ignored", reason="playback_unavailable")
    turn["consumed"] = True
    return _music_tool_result(
        manager, "accepted", action="play", playback_state="searching"
    )


def _build_music_intent_tool(manager: Any) -> ToolDefinition:
    texts = get_music_intent_tool_texts(getattr(manager, "user_language", None))
    return ToolDefinition(
        name="request_music_playback",
        description=texts["description"],
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "stop"],
                    "description": texts["action"],
                },
                "target_type": {
                    "type": "string",
                    "enum": [
                        "song", "artist", "playlist", "liked",
                        "daily", "query", "generic",
                    ],
                    "description": texts["target_type"],
                },
                "song": {"type": "string", "description": texts["song"]},
                "artist": {
                    "type": "string",
                    "description": texts["artist"],
                },
                "playlist": {
                    "type": "string",
                    "description": texts["playlist"],
                },
                "query": {"type": "string", "description": texts["query"]},
            },
            "required": ["action", "target_type"],
        },
        handler=lambda arguments: handle_music_intent_tool(manager, arguments),
        metadata={"source": "builtin"},
    )


def _next_music_request_epoch(manager: Any) -> int:
    epoch = int(getattr(manager, "_music_request_epoch", 0) or 0) + 1
    manager._music_request_epoch = epoch
    return epoch


def _is_current_music_request(manager: Any, epoch: int) -> bool:
    return int(getattr(manager, "_music_request_epoch", 0) or 0) == epoch


def _enqueue_music_request_context(
    manager: Any,
    epoch: int,
) -> None:
    enqueue = getattr(manager, "enqueue_agent_callback", None)
    if not callable(enqueue):
        return
    detail = get_music_request_pending_prompt(
        getattr(manager, "user_language", None)
    )
    callback = {
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
    enqueue(callback)
    manager._music_request_pending_context = callback


def _enqueue_music_request_failure_context(
    manager: Any,
    epoch: int,
    query: str,
    error_code: str,
) -> None:
    enqueue = getattr(manager, "enqueue_agent_callback", None)
    if not callable(enqueue):
        return
    detail = f"音乐请求未能完成（{error_code}）"
    if query:
        detail += f"：{query}"
    enqueue(
        {
            "event": "agent_task_callback",
            "origin": "event",
            "task_id": f"music_request:{epoch}",
            "channel": "music_playback",
            "status": "failed",
            "success": False,
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
                "context_type": "music_request_failed",
                "request_id": epoch,
                "error_code": error_code,
            },
            "context_type": "music_request_failed",
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


async def _wait_for_current_reply(
    manager: Any,
    epoch: int,
    search_elapsed_seconds: float,
) -> None:
    if not _reply_in_progress(manager):
        grace = _REPLY_START_GRACE_SECONDS - search_elapsed_seconds
        if grace > 0:
            await asyncio.sleep(grace)

    deadline = asyncio.get_running_loop().time() + _REPLY_WAIT_TIMEOUT_SECONDS
    while (
        _is_current_music_request(manager, epoch)
        and _reply_in_progress(manager)
    ):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_REPLY_WAIT_POLL_SECONDS, remaining))


def _clean_playback_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_playback_started_at(value: Any) -> float | None:
    try:
        started_at = float(value)
    except (TypeError, ValueError):
        return None
    return started_at if math.isfinite(started_at) and started_at > 0 else None


def _clean_music_request_id(value: Any) -> int | None:
    try:
        request_id = int(value)
    except (TypeError, ValueError):
        return None
    return request_id if request_id > 0 else None


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
    playback_window_id = _clean_playback_text(
        event.get("playback_window_id"), 128
    )
    playback_started_at = _clean_playback_started_at(
        event.get("playback_started_at")
    )
    request_id = _clean_playback_text(event.get("request_id"), 64)
    source = _clean_playback_text(event.get("source"), 16).lower()
    failure_reason = _clean_playback_text(event.get("reason"), 32).lower()
    if state != "error":
        failure_reason = ""
    elif failure_reason not in _PLAYBACK_FAILURE_REASONS:
        failure_reason = "unknown"
    if not playback_id or not playback_window_id or playback_started_at is None:
        return False

    owner_key = (playback_window_id, playback_id)
    current_owner_key = getattr(manager, "_music_playback_owner_key", None)
    current_started_at = getattr(manager, "_music_playback_owner_started_at", None)
    is_current_owner = (
        owner_key == current_owner_key
        and playback_started_at == current_started_at
    )
    current_request_epoch = getattr(manager, "_music_request_epoch", None)
    if request_id and current_request_epoch is not None:
        if request_id != str(current_request_epoch) and not is_current_owner:
            return False
    elif source == "user":
        return False

    if current_started_at is not None and (
        playback_started_at < current_started_at
        or (
            playback_started_at == current_started_at
            and owner_key != current_owner_key
        )
    ):
        return False
    if playback_started_at > (current_started_at or 0):
        manager._music_playback_owner_key = owner_key
        manager._music_playback_owner_started_at = playback_started_at

    event_key = (playback_id, request_id, state, playback_started_at)
    if getattr(manager, "_music_playback_event_key", None) == event_key:
        return False
    manager._music_playback_event_key = event_key
    manager._music_playback_state = state

    if state == "error":
        logger.warning(
            "[%s] 音乐播放器报告失败: reason=%s",
            getattr(manager, "lanlan_name", "") or "unknown",
            failure_reason,
        )

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
    pending_context = getattr(manager, "_music_request_pending_context", None)
    pending_request_id = None
    if isinstance(pending_context, dict):
        metadata = pending_context.get("metadata")
        if isinstance(metadata, dict):
            pending_request_id = _clean_music_request_id(metadata.get("request_id"))
    numeric_request_id = _clean_music_request_id(request_id)
    pending_was_injected = bool(
        pending_request_id is not None
        and pending_request_id == numeric_request_id
        and not pending_context.get(DELIVERY_RETRACTED_KEY)
        and not any(
            callback is pending_context
            for callback in (getattr(manager, "pending_agent_callbacks", None) or [])
        )
    )
    should_respond = (
        state == "playing"
        and source == "user"
        and not pending_was_injected
        and getattr(manager, "_music_playback_acknowledged_key", None)
        != acknowledge_key
    )
    if pending_request_id is not None and pending_request_id == numeric_request_id:
        manager._music_request_pending_context = None
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
            "playback_window_id": playback_window_id,
            "playback_started_at": playback_started_at,
            "request_id": request_id,
            "failure_reason": failure_reason,
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


def handle_music_request_playback_failed(
    manager: Any,
    event: dict[str, Any],
) -> bool:
    """Replace a pending request cue after all browser candidates fail."""
    request_id = _clean_music_request_id(event.get("request_id"))
    if request_id is None or not _is_current_music_request(manager, request_id):
        return False
    _enqueue_music_request_failure_context(
        manager,
        request_id,
        "",
        "playback_failed",
    )
    return True


async def _execute_music_request(
    manager: Any,
    request: MusicRequest,
    epoch: int,
) -> dict:
    await _push_music_payload(
        manager,
        {
            "type": "music_request_started",
            "request_id": epoch,
        },
    )
    if not _is_current_music_request(manager, epoch):
        return {"status": "superseded"}
    loop = asyncio.get_running_loop()
    search_started_at = loop.time()
    result = await fetch_music_request(
        request,
        limit=5,
        source_locale=getattr(manager, "user_language", None),
        include_failure=True,
        bypass_recommendation_dedupe=True,
    )
    if not _is_current_music_request(manager, epoch):
        return {"status": "superseded"}
    if result and result.get("success") and result.get("data"):
        mark_music_request_query(getattr(manager, "lanlan_name", ""), request)

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
        _enqueue_music_request_failure_context(
            manager,
            epoch,
            request.display_query,
            error_code,
        )
        await _send_music_request_failure(
            manager,
            request.display_query,
            error_code,
            epoch,
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
    await _wait_for_current_reply(
        manager,
        epoch,
        loop.time() - search_started_at,
    )
    if not _is_current_music_request(manager, epoch):
        return {"status": "superseded"}
    if await _push_music_payload(manager, payload):
        return {
            "status": "queued",
            "candidates": len(candidates),
        }
    _enqueue_music_request_failure_context(
        manager,
        epoch,
        request.display_query,
        "playback_unavailable",
    )
    return {"status": "playback_unavailable"}


async def _send_music_request_failure(
    manager: Any,
    query: str,
    error_code: str,
    request_id: int,
) -> None:
    await _push_music_payload(
        manager,
        {
            "type": "music_request_failed",
            "request_id": request_id,
            "query": query,
            "error_code": error_code,
        },
    )


async def _push_music_payload(manager: Any, payload: dict[str, Any]) -> bool:
    websocket = getattr(manager, "websocket", None)
    targets = [websocket]
    broadcast = payload.get("type") in {
        "music_request_started",
        "music_request_cancelled",
    }
    for candidate in tuple(
        getattr(manager, "_music_playback_websockets", ()) or ()
    ):
        if candidate is not websocket:
            targets.append(candidate)

    delivered = False
    for target in targets:
        if target is None or not hasattr(target, "send_json"):
            continue
        ws_state = getattr(target, "client_state", None)
        if ws_state is not None and ws_state != ws_state.CONNECTED:
            continue
        try:
            await target.send_json(payload)
            delivered = True
            if not broadcast:
                break
        except Exception as exc:
            logger.warning(
                "[%s] user music payload push failed: %s",
                getattr(manager, "lanlan_name", ""),
                exc,
            )

    if delivered:
        manager.sync_message_queue.put({"type": "json", "data": payload})
    return delivered


register_builtin_tool_factory(_build_music_intent_tool)
register_user_utterance_sink(_on_user_utterance)
