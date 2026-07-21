# -*- coding: utf-8 -*-
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

"""Framework-independent orchestration stages for proactive chat."""

import logging
from dataclasses import dataclass
from typing import Any

from config.prompts.prompts_proactive import build_proactive_action_note

from .contracts import (
    PROACTIVE_REASON_DELIVERY_FAILED,
    PROACTIVE_REASON_DELIVERY_PREEMPTED,
    ProactiveChatResult,
    _proactive_pass_body,
)
from .decisions import build_proactive_response
from .music_recommendation import _append_music_recommendations


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommittedDelivery:
    """Delivery facts that may be recorded only after a successful commit."""

    primary_channel: str
    source_links: list[dict[str, Any]]
    delivered_tag: str
    delivered_music_link: dict[str, Any] | None
    is_music_used: bool
    action_note: str
    vision_screenshot_b64: str | None


@dataclass(frozen=True, slots=True)
class DeliveryCommit:
    """Either a terminal pass result or successfully committed delivery facts."""

    result: ProactiveChatResult | None
    delivery: CommittedDelivery | None


async def _commit_proactive_delivery(
    *,
    mgr: Any,
    proactive_sid: Any,
    lanlan_name: str,
    response_text: str,
    source_tag: str,
    active_channels: list[str],
    selected_web_link: dict[str, Any] | None,
    selected_music_link: dict[str, Any] | None,
    selected_meme_link: dict[str, Any] | None,
    music_content: dict[str, Any] | None,
    is_music_used: bool,
    is_playing_music: bool,
    music_cooldown: bool,
    vision_content: dict[str, Any] | None,
    phase2_use_vision: bool,
    screenshot_b64: str | None,
    proactive_lang: str,
    master_name: str,
    log: logging.Logger | None = None,
) -> DeliveryCommit:
    """Build, feed, and atomically finish one proactive delivery."""
    active_logger = log or logger
    has_music_topic = "music" in active_channels
    primary_channel, source_links = build_proactive_response(
        source_tag,
        {
            "lanlan_name": lanlan_name,
            "is_music_used": is_music_used,
            "selected_web_link": selected_web_link,
            "selected_music_link": selected_music_link,
            "selected_meme_link": selected_meme_link,
            "vision_content": vision_content,
        },
    )

    should_try_music_fallback = (
        not is_playing_music
        and not music_cooldown
        and (
            primary_channel == "music"
            or (
                has_music_topic
                and not any(
                    channel in ("vision", "web", "meme")
                    for channel in active_channels
                )
            )
        )
    )
    if should_try_music_fallback:
        if source_links is None:
            source_links = []
        if _append_music_recommendations(source_links, music_content) > 0:
            is_music_used = True

    if is_music_used:
        music_already_appended = any(
            link.get("source") == "音乐推荐" for link in source_links
        )
        if not music_already_appended:
            _append_music_recommendations(source_links, music_content)

    if is_music_used or primary_channel == "music":
        delivered_tag = "MUSIC"
    elif primary_channel == "meme" and selected_meme_link is not None:
        delivered_tag = "MEME"
    else:
        delivered_tag = "CHAT"

    delivered_music_link = selected_music_link
    if delivered_tag == "MUSIC" and not delivered_music_link:
        delivered_music_link = next(
            (
                link
                for link in (source_links or [])
                if isinstance(link, dict) and link.get("source") == "音乐推荐"
            ),
            None,
        )

    action_note = build_proactive_action_note(
        primary_channel=primary_channel,
        source_links=source_links,
        language=proactive_lang,
        master_name=master_name,
    )
    staged_screenshot = screenshot_b64 if phase2_use_vision else None
    try:
        await mgr.feed_tts_chunk(
            response_text,
            expected_speech_id=proactive_sid,
        )
        committed = await mgr.finish_proactive_delivery(
            response_text,
            expected_speech_id=proactive_sid,
            action_note=action_note,
            source_tag=delivered_tag,
            vision_screenshot_b64=staged_screenshot,
        )
    except Exception as exc:
        active_logger.warning(
            "[%s] buffered proactive delivery failed: %s",
            lanlan_name,
            exc,
        )
        if not mgr.state.is_proactive_preempted(proactive_sid):
            await mgr.handle_new_message()
        else:
            active_logger.info(
                "[%s] buffered delivery failed after user takeover; "
                "skip TTS cleanup",
                lanlan_name,
            )
        return DeliveryCommit(
            result=ProactiveChatResult(
                body=_proactive_pass_body(
                    PROACTIVE_REASON_DELIVERY_FAILED,
                    message="Phase 2 buffered delivery failed",
                )
            ),
            delivery=None,
        )

    if not committed:
        active_logger.info(
            "[%s] 主动搭话被用户接管，短路下游写入（topic/memory/response）",
            lanlan_name,
        )
        return DeliveryCommit(
            result=ProactiveChatResult(
                body=_proactive_pass_body(
                    PROACTIVE_REASON_DELIVERY_PREEMPTED,
                    message="proactive delivery skipped: user took over turn",
                    lanlan_name=lanlan_name,
                    turn_id=mgr.current_speech_id,
                )
            ),
            delivery=None,
        )

    return DeliveryCommit(
        result=None,
        delivery=CommittedDelivery(
            primary_channel=primary_channel,
            source_links=source_links,
            delivered_tag=delivered_tag,
            delivered_music_link=delivered_music_link,
            is_music_used=is_music_used,
            action_note=action_note,
            vision_screenshot_b64=staged_screenshot,
        ),
    )
