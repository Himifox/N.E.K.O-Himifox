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

"""Shared parsing and resolution for user and proactive music requests."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

MusicFetcher = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class MusicRequest:
    keyword: str = ""
    song_name: str = ""
    song_artist: str = ""
    playlist_name: str = ""
    personalization_source: str = "auto"

    @property
    def strict(self) -> bool:
        return bool(
            self.song_name
            or self.song_artist
            or self.playlist_name
            or self.personalization_source != "auto"
        )

    @property
    def display_query(self) -> str:
        if self.playlist_name:
            return self.playlist_name
        if self.personalization_source == "liked":
            return "liked songs"
        if self.personalization_source == "daily":
            return "daily recommendations"
        if self.song_artist and not self.song_name and self.keyword == self.song_artist:
            return self.song_artist
        return " ".join(
            part for part in (self.song_name or self.keyword, self.song_artist) if part
        )


def parse_music_request(value: str) -> MusicRequest:
    """Parse the controlled directives emitted by proactive chat or a tool."""
    normalized = str(value or "").strip()
    for prefix in ("playlist:", "playlist：", "歌单:", "歌单："):
        if normalized.casefold().startswith(prefix.casefold()):
            name = normalized[len(prefix) :].strip(" '\"「」『』《》")
            return MusicRequest(playlist_name=name)

    for prefix in ("song:", "song：", "歌曲:", "歌曲："):
        if normalized.casefold().startswith(prefix.casefold()):
            payload = normalized[len(prefix) :].strip(" '\"「」『』《》")
            song_name, separator, song_artist = payload.partition("|")
            song_name = song_name.strip(" '\"「」『』《》")
            song_artist = song_artist.strip(" '\"「」『』《》") if separator else ""
            keyword = " ".join(part for part in (song_name, song_artist) if part)
            return MusicRequest(
                keyword=keyword,
                song_name=song_name,
                song_artist=song_artist,
            )

    for prefix in ("source:", "source："):
        if normalized.casefold().startswith(prefix.casefold()):
            source = normalized[len(prefix) :].strip().casefold()
            aliases = {
                "liked": "liked",
                "favorites": "liked",
                "我喜欢": "liked",
                "红心": "liked",
                "daily": "daily",
                "daily recommendations": "daily",
                "日推": "daily",
                "每日推荐": "daily",
            }
            normalized_source = aliases.get(source)
            if normalized_source:
                return MusicRequest(personalization_source=normalized_source)
            logger.warning("未知音乐来源指令: %r", source)
            return MusicRequest()

    if normalized.casefold() in {"personalized", "个性化", "按喜好推荐"}:
        return MusicRequest()
    return MusicRequest(keyword=normalized)


_CLAUSE_SEPARATOR = re.compile(r"[，,。；;！？!?]+")
_ZH_NEGATIVE_MUSIC = re.compile(
    r"(?:不要|别|不想|不听|无需|停止|暂停|关掉|取消).{0,6}(?:播放|放|播|听|音乐|歌)"
)
_EN_NEGATIVE_MUSIC = re.compile(
    r"\b(?:do\s+not|don't|dont|stop|pause|cancel)\b.{0,20}\b(?:play|music|song|listen)\b",
    re.IGNORECASE,
)


def _strip_request_payload(value: str) -> str:
    return value.strip(" \t\r\n'\"“”‘’《》〈〉「」『』【】")


def _parse_explicit_zh_clause(clause: str) -> MusicRequest | None:
    if not clause or _ZH_NEGATIVE_MUSIC.search(clause):
        return None

    if re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:只)?(?:来|放|播放|听)(?:一下)?(?:一首|首|点)?(?:我)?(?:的)?(?:红心|我喜欢|收藏)(?:的)?(?:歌|歌曲|音乐)?",
        clause,
    ):
        return MusicRequest(personalization_source="liked")
    if re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:只)?(?:来|放|播放|听)(?:一下)?(?:一首|首|点)?(?:网易云)?(?:的)?(?:日推|每日推荐)(?:歌|歌曲|音乐)?",
        clause,
    ):
        return MusicRequest(personalization_source="daily")

    playlist_match = re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:从|播放|放|听)(?:网易云)?(?:的)?(?:歌单)?"
        r"[《「『【]?(.{1,40}?)[》」』】]?(?:这个|的)?(?:歌单)?(?:里|中)"
        r"(?:随机)?(?:放|播|听|来)?(?:一首|首|点)?(?:歌|音乐)?",
        clause,
    )
    if playlist_match:
        return MusicRequest(
            playlist_name=_strip_request_payload(playlist_match.group(1))
        )

    quoted_match = re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:我)?(?:想|要)?(?:播放|放|听|来)(?:一下)?(?:一首|首)?"
        r"(?:(.{1,30}?)的)?[《「『【](.{1,60}?)[》」』】](?:这首歌|这首|歌曲|歌)?",
        clause,
    )
    if quoted_match:
        artist = _strip_request_payload(quoted_match.group(1) or "")
        song = _strip_request_payload(quoted_match.group(2))
        return MusicRequest(
            keyword=" ".join(part for part in (song, artist) if part),
            song_name=song,
            song_artist=artist,
        )

    switch_match = re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:我)?(?:想|要)?"
        r"(?:换成|切到|切成|改放)(?:歌曲?|曲目|音乐)\s*[:：]?\s*(.{1,60})",
        clause,
    )
    if switch_match:
        song = _strip_request_payload(switch_match.group(1))
        return MusicRequest(keyword=song, song_name=song)

    artist_match = re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:我)?(?:想|要)?(?:播放|放|听|来点|来一首|来首)(?:一下)?"
        r"(.{1,40}?)的(?:歌|歌曲|音乐)",
        clause,
    )
    if artist_match:
        artist = _strip_request_payload(artist_match.group(1))
        return MusicRequest(keyword=artist, song_artist=artist)

    artist_song_match = re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:我)?(?:想|要)?(?:播放|放|听|来一首)(?:一下)?"
        r"(.{1,30}?)的(.{1,60})",
        clause,
    )
    if artist_song_match:
        artist = _strip_request_payload(artist_song_match.group(1))
        song = _strip_request_payload(artist_song_match.group(2))
        return MusicRequest(
            keyword=f"{song} {artist}",
            song_name=song,
            song_artist=artist,
        )

    generic_match = re.fullmatch(
        r"(?:请|麻烦)?(?:给我|帮我)?(?:我)?(?:想|要)?"
        r"(播放一首|播放首|播放|放一首|放首|听一下|想听|要听|来一首|来首|来点)(.{0,60})",
        clause,
    )
    if not generic_match:
        return None
    _action, payload = generic_match.groups()
    payload = _strip_request_payload(payload)
    if payload in {"", "歌", "歌曲", "音乐", "一首歌", "首歌", "点音乐"}:
        return MusicRequest()
    named_song_match = re.fullmatch(r"(?:歌曲?|曲目)\s*[:：]\s*(.{1,60})", payload)
    if named_song_match:
        song = _strip_request_payload(named_song_match.group(1))
        return MusicRequest(keyword=song, song_name=song)
    if _action in {"播放一首", "播放首", "放一首", "放首", "来一首", "来首"}:
        return MusicRequest(keyword=payload, song_name=payload)
    return MusicRequest(keyword=payload)


def _parse_explicit_en_clause(clause: str) -> MusicRequest | None:
    if not clause or _EN_NEGATIVE_MUSIC.search(clause):
        return None
    normalized = clause.strip()
    match = re.fullmatch(
        r"(?:please\s+)?(?:i\s+(?:want|would like)\s+to\s+)?(?:play|listen\s+to)\s+"
        r"(?:some\s+)?songs?\s+(?:by|from)\s+(.{1,60})",
        normalized,
        re.IGNORECASE,
    )
    if match:
        artist = _strip_request_payload(match.group(1))
        return MusicRequest(keyword=artist, song_artist=artist)
    match = re.fullmatch(
        r"(?:please\s+)?play\s+(.{1,60}?)\s+by\s+(.{1,60})",
        normalized,
        re.IGNORECASE,
    )
    if match:
        song = _strip_request_payload(match.group(1))
        artist = _strip_request_payload(match.group(2))
        return MusicRequest(
            keyword=f"{song} {artist}",
            song_name=song,
            song_artist=artist,
        )
    match = re.fullmatch(
        r"(?:please\s+)?(?:i\s+(?:want|would like)\s+to\s+)?(?:play|listen\s+to)\s+(.{1,80})",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return None
    payload = _strip_request_payload(match.group(1))
    if payload.casefold() in {"music", "a song", "some music", "something"}:
        return MusicRequest()
    return MusicRequest(keyword=payload)


def parse_explicit_user_music_request(text: str) -> MusicRequest | None:
    """Return only high-confidence, user-initiated playback requests."""
    normalized = " ".join(str(text or "").strip().split())
    if not normalized or len(normalized) > 160:
        return None
    for clause in reversed(_CLAUSE_SEPARATOR.split(normalized)):
        clause = clause.strip()
        request = _parse_explicit_zh_clause(clause) or _parse_explicit_en_clause(clause)
        if request is not None:
            return request
    return None


async def fetch_music_request(
    request: MusicRequest,
    *,
    limit: int = 5,
    source_locale: str | None = None,
    fetcher: MusicFetcher | None = None,
    allow_keyword_fallback: bool = False,
    include_failure: bool = False,
) -> dict[str, Any] | None:
    """Resolve a request, falling back only for non-strict keyword searches."""
    if fetcher is None:
        from utils.music_crawlers import fetch_music_content

        fetcher = fetch_music_content

    async def fetch(keyword: str) -> dict[str, Any]:
        try:
            return await fetcher(
                keyword=keyword,
                limit=limit,
                source_locale=source_locale,
                personalized=True,
                playlist_name=request.playlist_name,
                personalization_source=request.personalization_source,
                requested_song=request.song_name,
                requested_artist=request.song_artist,
            )
        except Exception as exc:
            logger.warning("音乐请求获取失败: %s", exc)
            return {
                "success": False,
                "error_code": "upstream_error",
                "error": "Music provider request failed",
                "data": [],
            }

    result = await fetch(request.keyword)
    if result and result.get("success"):
        return result
    if request.strict or not request.keyword or not allow_keyword_fallback:
        return result if include_failure else None

    fallback = await fetch("")
    if fallback and fallback.get("success"):
        return fallback
    return fallback if include_failure else None
