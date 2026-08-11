# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Small, fail-closed parser for direct user music commands."""

from __future__ import annotations

import re

from main_logic.music_requests import MusicRequest


_ZH_PREFIX = r"(?:(?:请|請|麻烦|麻煩)\s*)?(?:(?:帮|幫|给|給)我\s*)?"
_ZH_PLAY_COMMAND = re.compile(
    rf"^{_ZH_PREFIX}"
    r"(?P<action>播放(?:一下|下|一首|首)?|放(?:一首|首)|"
    r"放(?=\s*(?:音乐|音樂|歌曲?|歌|[《「『【]))|"
    r"[听聽](?:一首|首)|[来來](?:一首|首))"
    r"\s*(?P<target>.{1,80}?)(?:吧|啊|呀|哦|喔)?$"
)
_ZH_SWITCH_COMMAND = re.compile(
    rf"^{_ZH_PREFIX}(?:换成|換成|切到|切成|改放)"
    r"(?:歌曲?|曲目|音乐|音樂)\s*[:：]\s*"
    r"(?P<target>.{1,60}?)(?:吧|啊|呀|哦|喔)?$"
)
_ZH_STOP_ACTION = r"(?:停止|停掉|暂停|暫停)"
_ZH_PLAYBACK_ACTION = r"(?:播放|放|播|听|聽)"
_ZH_MUSIC_OBJECT = r"(?:音乐|音樂|歌曲?|歌|当前音乐|當前音樂)"
_ZH_MUSIC_SOURCE = r"(?:红心歌单|紅心歌單|每日推荐|每日推薦|日推)"
_ZH_STOP_COMMAND = re.compile(
    rf"^{_ZH_PREFIX}(?:"
    rf"{_ZH_STOP_ACTION}{_ZH_PLAYBACK_ACTION}?{_ZH_MUSIC_OBJECT}"
    rf"|(?:关掉|關掉|关闭|關閉){_ZH_MUSIC_OBJECT}"
    rf"|取消{_ZH_PLAYBACK_ACTION}?{_ZH_MUSIC_OBJECT}"
    rf"|{_ZH_STOP_ACTION}{_ZH_PLAYBACK_ACTION}?(?:我的)?{_ZH_MUSIC_SOURCE}"
    rf"|(?:不要|别|別)(?:再)?{_ZH_PLAYBACK_ACTION}{_ZH_MUSIC_OBJECT}"
    r")(?:了|吧)?$"
)

_EN_PLAY_COMMAND = re.compile(
    r"^(?:(?:(?:please\s+)?|(?:can|could|would)\s+you\s+(?:please\s+)?)play"
    r"|(?:please\s+)?listen\s+to"
    r"|i\s+(?:want|would\s+like)\s+to\s+(?:play|listen\s+to))"
    r"\s+(?P<target>.{1,100})$",
    re.IGNORECASE,
)
_EN_PREFIX = r"(?:(?:can|could|would)\s+you\s+(?:please\s+)?|(?:please\s+)?)"
_EN_MUSIC_OBJECT = r"(?:music|playback|songs?|tracks?)"
_EN_STOP_COMMAND = re.compile(
    rf"^(?:{_EN_PREFIX}(?:stop|pause|cancel)\s+(?:playing\s+)?"
    rf"(?:(?:this|that|the)\s+)?{_EN_MUSIC_OBJECT}"
    rf"|{_EN_PREFIX}(?:turn|shut)\s+off\s+(?:the\s+)?(?:music|playback)"
    rf"|{_EN_PREFIX}(?:turn|shut)\s+(?:the\s+)?{_EN_MUSIC_OBJECT}\s+off"
    rf"|{_EN_PREFIX}(?:do\s+not|don't|don’t|dont)\s+(?:play|listen\s+to)\s+"
    r"(?:(?:this|that|the)\s+)?(?:music|songs?))$",
    re.IGNORECASE,
)
_EN_SECOND_COMMAND = re.compile(
    r"(?:[.!]\s+|\b(?:and|but)\s+)"
    r"(?:please\s+)?(?:(?:do\s+not|don't|don’t|dont)\s+play|"
    r"play|listen\s+to|stop|pause|cancel)\b",
    re.IGNORECASE,
)

_TRAILING_STATEMENT_MARKS = "。.!！"
_CLAUSE_MARKS = frozenset("，,；;、")
_ZH_QUOTES = (("《", "》"), ("「", "」"), ("『", "』"), ("【", "】"))


def _normalize_command(text: str, *, allow_polite_question: bool = False) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized or len(normalized) > 120:
        return ""
    if any(mark in normalized for mark in _CLAUSE_MARKS):
        return ""
    if "?" in normalized or "？" in normalized:
        if not allow_polite_question or normalized.count("?") + normalized.count("？") != 1:
            return ""
        if normalized[-1] not in "?？":
            return ""
        normalized = normalized[:-1].rstrip()
    return normalized.rstrip(_TRAILING_STATEMENT_MARKS).rstrip()


def _strip_target(value: str) -> str:
    return value.strip(" \t'\"“”‘’《》「」『』【】")


def _parse_zh_target(action: str, target: str) -> MusicRequest | None:
    target = target.strip()
    if not target:
        return None

    compact = target.replace(" ", "")
    if compact in {
        "红心", "紅心", "红心歌单", "紅心歌單", "我的红心歌单", "我的紅心歌單",
        "我喜欢的", "我喜歡的", "我喜欢的歌", "我喜歡的歌", "收藏歌曲", "收藏的歌",
    }:
        return MusicRequest(personalization_source="liked")
    if compact in {
        "日推", "每日推荐", "每日推薦", "网易云日推", "網易雲日推",
        "网易云的日推", "網易雲的日推",
    }:
        return MusicRequest(personalization_source="daily")
    if compact in {
        "音乐", "音樂", "歌曲", "歌", "一首歌", "首歌",
        "我的歌单", "我的歌單", "我的播放列表", "我的播放清单", "我的播放清單",
    }:
        return MusicRequest()

    playlist_match = re.fullmatch(
        r"(?:我的)?(.{1,40}?)(?:歌单|歌單|播放列表|播放清单|播放清單)",
        target,
    )
    if playlist_match:
        playlist = _strip_target(playlist_match.group(1))
        return MusicRequest(playlist_name=playlist) if playlist else MusicRequest()

    for opening, closing in _ZH_QUOTES:
        quoted = re.fullmatch(
            rf"(?:(.{{1,30}}?)的)?{re.escape(opening)}(.{{1,60}}){re.escape(closing)}",
            target,
        )
        if quoted:
            artist = _strip_target(quoted.group(1) or "")
            song = _strip_target(quoted.group(2))
            if not song:
                return None
            return MusicRequest(
                keyword=" ".join(part for part in (song, artist) if part),
                song_name=song,
                song_artist=artist,
            )

    labeled_song = re.fullmatch(r"(?:歌曲?|曲目)\s*[:：]\s*(.{1,60})", target)
    if labeled_song:
        song = _strip_target(labeled_song.group(1))
        return MusicRequest(keyword=song, song_name=song) if song else None

    artist_song = re.fullmatch(
        r"(.{1,30}?)的(.{1,60}?)(?:这首歌曲|這首歌曲|这首歌|這首歌|歌曲)",
        target,
    )
    if artist_song:
        artist = _strip_target(artist_song.group(1))
        song = _strip_target(artist_song.group(2))
        if not artist or not song:
            return None
        return MusicRequest(
            keyword=f"{song} {artist}",
            song_name=song,
            song_artist=artist,
        )

    if any(token in action for token in ("一首", "首")):
        if re.fullmatch(r".{1,40}?的(?:歌|歌曲|音乐|音樂)", target):
            return None
        return MusicRequest(keyword=target, song_name=target)
    return None


def _parse_en_target(target: str) -> MusicRequest | None:
    target = target.strip()
    if not target:
        return None
    if re.fullmatch(
        r"(?:(?:me|us)\s+)?(?:"
        r"(?:(?:some|any|the)\s+)?music|"
        r"(?:(?:a|any|some|the)\s+)?(?:songs?|tracks?|tunes?))"
        r"(?:\s+for\s+(?:me|us))?",
        target,
        re.IGNORECASE,
    ):
        return MusicRequest()
    if re.fullmatch(
        r"(?:(?:a|some)\s+songs?\s+from\s+)?(?:my\s+)?"
        r"(?:liked songs?|favorites?|favourites?|favorite songs?|favourite songs?)",
        target,
        re.IGNORECASE,
    ):
        return MusicRequest(personalization_source="liked")
    if re.fullmatch(
        r"(?:(?:a|some)\s+songs?\s+from\s+)?(?:my\s+)?"
        r"(?:daily mix|daily recommendations?)",
        target,
        re.IGNORECASE,
    ):
        return MusicRequest(personalization_source="daily")

    playlist = re.fullmatch(
        r"(?:(?:a|some)\s+songs?\s+from\s+)?(?:my\s+)?"
        r"(.{1,60}?)\s+playlist",
        target,
        re.IGNORECASE,
    )
    if playlist:
        return MusicRequest(playlist_name=_strip_target(playlist.group(1)))

    quoted = re.fullmatch(r'(?:"([^"\r\n]{1,60})"|“([^”\r\n]{1,60})”)', target)
    if quoted:
        song = _strip_target(next(part for part in quoted.groups() if part))
        return MusicRequest(keyword=song, song_name=song) if song else None

    labeled_song = re.fullmatch(
        r"(?:song|track)\s*:\s*(.{1,60})",
        target,
        re.IGNORECASE,
    )
    if labeled_song:
        song = _strip_target(labeled_song.group(1))
        return MusicRequest(keyword=song, song_name=song) if song else None

    by_artist = re.fullmatch(r"(.{1,60}?)\s+by\s+(.{1,60})", target, re.IGNORECASE)
    if by_artist:
        song = _strip_target(by_artist.group(1))
        artist = _strip_target(by_artist.group(2))
        if song.casefold() in {"song", "a song", "the song"}:
            return MusicRequest(keyword=artist, song_artist=artist)
        song = re.sub(r"^(?:a|the)\s+song\s+", "", song, flags=re.IGNORECASE)
        return MusicRequest(
            keyword=f"{song} {artist}",
            song_name=song,
            song_artist=artist,
        )
    return None


def parse_strict_music_command(text: str) -> MusicRequest | None:
    """Parse only complete, direct playback commands; ambiguity fails closed."""
    normalized = _normalize_command(text)
    if normalized:
        switch = _ZH_SWITCH_COMMAND.fullmatch(normalized)
        if switch:
            song = _strip_target(switch.group("target"))
            return MusicRequest(keyword=song, song_name=song) if song else None
        match = _ZH_PLAY_COMMAND.fullmatch(normalized)
        if match:
            return _parse_zh_target(match.group("action"), match.group("target"))

    normalized = _normalize_command(text, allow_polite_question=True)
    if not normalized or _EN_SECOND_COMMAND.search(normalized):
        return None
    match = _EN_PLAY_COMMAND.fullmatch(normalized)
    if not match:
        return None
    return _parse_en_target(match.group("target"))


def is_strict_music_cancellation(text: str) -> bool:
    """Return True only for a complete, direct stop-music command."""
    normalized = _normalize_command(text)
    return bool(
        normalized
        and (
            _ZH_STOP_COMMAND.fullmatch(normalized)
            or _EN_STOP_COMMAND.fullmatch(normalized)
        )
    )
