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

"""Runtime playback adapter shared by normal chat and model tool calls."""

from __future__ import annotations

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
    turn_id = str(getattr(manager, "current_speech_id", "") or "")
    if turn_id and getattr(manager, "_music_request_handled_turn_id", "") == turn_id:
        return
    manager._music_request_handled_turn_id = turn_id
    epoch = _next_music_request_epoch(manager)
    manager._fire_task(_execute_music_request(manager, request, epoch))


def _next_music_request_epoch(manager: Any) -> int:
    epoch = int(getattr(manager, "_music_request_epoch", 0) or 0) + 1
    manager._music_request_epoch = epoch
    return epoch


def _is_current_music_request(manager: Any, epoch: int) -> bool:
    return int(getattr(manager, "_music_request_epoch", 0) or 0) == epoch


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
