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
_ZH_STOP_COMMAND = re.compile(
    rf"^{_ZH_PREFIX}(?:"
    r"(?:停止|停掉|暂停|暫停)(?:播放|音乐|音樂|歌曲?|歌|当前音乐|當前音樂)"
    r"|(?:关掉|關掉|关闭|關閉)(?:音乐|音樂|歌曲?|歌)"
    r"|取消(?:播放)?(?:音乐|音樂|歌曲?|歌)"
    r"|(?:停止|停掉|暂停|暫停)(?:播放)?(?:我的)?"
    r"(?:红心歌单|紅心歌單|每日推荐|每日推薦|日推)"
    r"|(?:不要|别|別)(?:再)?(?:播放|放|播|听|聽)(?:音乐|音樂|歌曲?|歌)?"
    r")(?:了|吧)?$"
)
_ZH_NON_MUSIC_TARGET = re.compile(
    r"^(?:(?:这个|這個|那个|那個|一个|一個|一段)\s*)?"
    r"(?:按钮|按鈕|功能|代码|代碼|教程|视频|視頻|影片|游戏|遊戲|电影|電影|"
    r"电视剧|電視劇|动画|動畫|动漫|動漫|播客|有声书|有聲書)"
)

_EN_PLAY_COMMAND = re.compile(
    r"^(?:(?:(?:please\s+)?|(?:can|could|would)\s+you\s+(?:please\s+)?)play"
    r"|(?:please\s+)?listen\s+to"
    r"|i\s+(?:want|would\s+like)\s+to\s+(?:play|listen\s+to))"
    r"\s+(?P<target>.{1,100})$",
    re.IGNORECASE,
)
_EN_STOP_COMMAND = re.compile(
    r"^(?:(?:please\s+)?(?:stop|pause|cancel)\s+(?:(?:this|that|the)\s+)?"
    r"(?:music|playback|songs?|tracks?)"
    r"|(?:please\s+)?(?:turn|shut)\s+off\s+(?:the\s+)?(?:music|playback)"
    r"|(?:please\s+)?(?:turn|shut)\s+(?:the\s+)?"
    r"(?:music|playback|songs?|tracks?)\s+off"
    r"|(?:please\s+)?(?:do\s+not|don't|don’t|dont)\s+play\s+"
    r"(?:(?:this|that|the)\s+)?(?:music|songs?))$",
    re.IGNORECASE,
)
_EN_NON_MUSIC_TARGET = re.compile(
    r"^(?:(?:(?:a|an|the|this|that|some)\s+)?"
    r"(?:games?|videos?|movies?|films?|shows?|podcasts?|audiobooks?)|"
    r"(?:with|along|around|outside|inside)\b)",
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
_ZH_AMBIGUOUS_ARTISTS = frozenset(
    {
        "我", "你", "妳", "他", "她", "它", "咱", "我们", "我們", "咱们", "咱們",
        "你们", "你們",
        "他们", "他們", "她们", "她們", "它们", "它們", "自己", "本人",
    }
)


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


def _strip_zh_song_suffix(value: str) -> str:
    return re.sub(r"(?:这首歌曲|這首歌曲|这首歌|這首歌|歌曲)$", "", value).rstrip()


def _parse_zh_target(action: str, target: str) -> MusicRequest | None:
    target = target.strip()
    if not target or _ZH_NON_MUSIC_TARGET.search(target):
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
            return MusicRequest(
                keyword=" ".join(part for part in (song, artist) if part),
                song_name=song,
                song_artist=artist,
            )

    labeled_song = re.fullmatch(r"(?:歌曲?|曲目)\s*[:：]\s*(.{1,60})", target)
    if labeled_song:
        song = _strip_target(labeled_song.group(1))
        return MusicRequest(keyword=song, song_name=song)

    artist_music = re.fullmatch(r"(.{1,40}?)的(?:歌|歌曲|音乐|音樂)", target)
    if artist_music:
        artist = _strip_target(artist_music.group(1))
        if artist in _ZH_AMBIGUOUS_ARTISTS:
            return None
        return MusicRequest(keyword=artist, song_artist=artist)

    artist_song = re.fullmatch(r"(.{1,30}?)的(.{1,60})", target)
    if artist_song:
        artist = _strip_target(artist_song.group(1))
        if artist in _ZH_AMBIGUOUS_ARTISTS:
            return None
        song = _strip_target(_strip_zh_song_suffix(artist_song.group(2)))
        if not song:
            return None
        return MusicRequest(
            keyword=f"{song} {artist}",
            song_name=song,
            song_artist=artist,
        )

    if any(token in action for token in ("一首", "首")):
        return MusicRequest(keyword=target, song_name=target)
    return MusicRequest(keyword=target)


def _parse_en_target(target: str) -> MusicRequest | None:
    target = _strip_target(target)
    if not target or _EN_NON_MUSIC_TARGET.search(target):
        return None
    if re.fullmatch(
        r"(?:(?:me|us)\s+)?(?:"
        r"(?:(?:some|any|the)\s+)?music|"
        r"(?:(?:a|any|some|the)\s+)?(?:songs?|tracks?|tunes?)|something)"
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
    return MusicRequest(keyword=target)


def parse_strict_music_command(text: str) -> MusicRequest | None:
    """Parse only complete, direct playback commands; ambiguity fails closed."""
    normalized = _normalize_command(text)
    if normalized:
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
