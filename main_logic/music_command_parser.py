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
    r"[听聽](?:一下|下|一首|首)|[来來](?:一首|首))"
    r"\s*(?P<target>.{1,80}?)(?:吧|啊|呀|哦|喔)?$"
)
_ZH_SWITCH_COMMAND = re.compile(
    rf"^{_ZH_PREFIX}(?:换成|換成|切到|切成|改放)"
    r"(?:歌曲?|曲目|音乐|音樂)\s*[:：]\s*"
    r"(?P<target>.{1,60}?)(?:吧|啊|呀|哦|喔)?$"
)
_ZH_STOP_ACTION = r"(?:停止|停掉|暂停|暫停)"
_ZH_CLOSE_ACTION = r"(?:关掉|關掉|关闭|關閉)"
_ZH_PLAYBACK_ACTION = r"(?:播放|放|播|听|聽)"
_ZH_MUSIC_OBJECT = (
    r"(?:(?:当前|當前)?(?:音乐|音樂|歌曲?)|(?:这|這)首歌|"
    r"(?:我的)?(?:歌单|歌單|播放列表|播放清单|播放清單)|播放器|"
    r"正在播放的(?:音乐|音樂))"
)
_ZH_MUSIC_SOURCE = (
    r"(?:红心(?:歌单)?|紅心(?:歌單)?|"
    r"(?:每日推荐|每日推薦|日推)(?:歌曲?|音乐|音樂)?)"
)
_ZH_NEGATIVE_LEAD = r"(?:不要|别|別)(?:(?:再|继续|繼續)|(?:(?:给|給|帮|幫)我))?"
_ZH_STOP_COMMAND = re.compile(
    rf"^{_ZH_PREFIX}(?:"
    rf"{_ZH_STOP_ACTION}{_ZH_PLAYBACK_ACTION}?{_ZH_MUSIC_OBJECT}"
    rf"|{_ZH_CLOSE_ACTION}{_ZH_MUSIC_OBJECT}"
    rf"|把{_ZH_MUSIC_OBJECT}{_ZH_CLOSE_ACTION}"
    rf"|取消{_ZH_PLAYBACK_ACTION}?{_ZH_MUSIC_OBJECT}"
    rf"|{_ZH_STOP_ACTION}{_ZH_PLAYBACK_ACTION}"
    rf"|{_ZH_STOP_ACTION}{_ZH_PLAYBACK_ACTION}?(?:我的)?{_ZH_MUSIC_SOURCE}"
    rf"|{_ZH_NEGATIVE_LEAD}{_ZH_PLAYBACK_ACTION}{_ZH_MUSIC_OBJECT}"
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
_EN_MUSIC_DETERMINER = r"(?:(?:this|that|the|my|our)\s+|(?:the\s+)?current\s+)?"
_EN_STOP_SUFFIX = r"(?:\s+(?:please|now))?"
_EN_GENERIC_MUSIC_TARGET = (
    r"(?:"
    r"(?:(?:some|any|the)\s+)?music"
    r"|(?:(?:a|any|some|the)\s+)?(?:songs?|tracks?|tunes?)"
    r")"
)
_EN_STOP_COMMAND = re.compile(
    rf"^(?:{_EN_PREFIX}(?:stop|pause|cancel)\s+(?:playing\s+)?"
    rf"{_EN_MUSIC_DETERMINER}{_EN_MUSIC_OBJECT}"
    rf"|{_EN_PREFIX}(?:turn|shut)\s+off\s+{_EN_MUSIC_DETERMINER}{_EN_MUSIC_OBJECT}"
    rf"|{_EN_PREFIX}(?:turn|shut)\s+{_EN_MUSIC_DETERMINER}{_EN_MUSIC_OBJECT}\s+off"
    rf"|{_EN_PREFIX}(?:do\s+not|don't|don’t|dont)\s+(?:play|listen\s+to)\s+"
    rf"{_EN_MUSIC_DETERMINER}{_EN_MUSIC_OBJECT}){_EN_STOP_SUFFIX}$",
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
_ZH_QUOTE_MARKS = "".join(mark for pair in _ZH_QUOTES for mark in pair)
_COMMAND_QUOTES = _ZH_QUOTES + (
    ("\"", "\""), ("“", "”"), ("'", "'"), ("‘", "’"),
)
_ZH_POSSESSIVE_REFERENTS = frozenset(
    {
        "我", "你", "妳", "您", "他", "她", "它", "牠", "咱", "自己", "本人",
        "我们", "我們", "你们", "你們", "他们", "他們", "她们", "她們",
        "它们", "它們", "牠们", "牠們", "咱们", "咱們",
    }
)


def _outside_complete_quotes(text: str) -> str:
    masked = list(text)
    for opening, closing in _COMMAND_QUOTES:
        cursor = 0
        while True:
            start = text.find(opening, cursor)
            if start < 0:
                break
            end = text.find(closing, start + len(opening))
            if end < 0:
                break
            for index in range(start, end + len(closing)):
                masked[index] = " "
            cursor = end + len(closing)
    return "".join(masked)


def _normalize_command(text: str, *, allow_polite_question: bool = False) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized or len(normalized) > 120:
        return ""
    outside_quotes = _outside_complete_quotes(normalized)
    if any(mark in outside_quotes for mark in _CLAUSE_MARKS):
        return ""
    question_count = outside_quotes.count("?") + outside_quotes.count("？")
    if question_count:
        if not allow_polite_question or question_count != 1:
            return ""
        if outside_quotes.rstrip()[-1] not in "?？":
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
        "我的收藏歌曲", "我的红心歌曲", "我的紅心歌曲",
    }:
        return MusicRequest(personalization_source="liked")
    if compact in {
        "日推", "每日推荐", "每日推薦", "网易云日推", "網易雲日推",
        "网易云的日推", "網易雲的日推",
        "日推歌曲", "日推音乐", "日推音樂",
        "每日推荐歌曲", "每日推荐音乐", "每日推薦歌曲", "每日推薦音樂",
        "网易云日推歌曲", "网易云日推音乐", "網易雲日推歌曲", "網易雲日推音樂",
        "网易云的日推歌曲", "网易云的日推音乐",
        "網易雲的日推歌曲", "網易雲的日推音樂",
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
            rf"(?:歌曲?|曲目)?\s*{re.escape(opening)}"
            rf"([^{re.escape(_ZH_QUOTE_MARKS)}]{{1,60}}){re.escape(closing)}",
            target,
        )
        if quoted:
            song = _strip_target(quoted.group(1))
            if not song:
                return None
            return MusicRequest(
                keyword=song,
                song_name=song,
            )
        qualified = re.fullmatch(
            rf"(.{{1,30}}?)的\s*{re.escape(opening)}"
            rf"([^{re.escape(_ZH_QUOTE_MARKS)}]{{1,60}}){re.escape(closing)}",
            target,
        )
        if qualified:
            artist = _strip_target(qualified.group(1))
            song = _strip_target(qualified.group(2))
            if not artist or not song or artist in _ZH_POSSESSIVE_REFERENTS:
                return None
            return MusicRequest(
                keyword=f"{song} {artist}",
                song_name=song,
                song_artist=artist,
            )
    if any(mark in target for pair in _ZH_QUOTES for mark in pair):
        return None

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
        if not artist or not song or artist in _ZH_POSSESSIVE_REFERENTS:
            return None
        return MusicRequest(
            keyword=f"{song} {artist}",
            song_name=song,
            song_artist=artist,
        )

    if any(token in action for token in ("一首", "首")):
        if "的" in target:
            return None
        return MusicRequest(keyword=target, song_name=target)
    return None


def _parse_en_target(target: str) -> MusicRequest | None:
    target = target.strip()
    if not target:
        return None
    if re.fullmatch(
        rf"(?:(?:me|us)\s+)?{_EN_GENERIC_MUSIC_TARGET}"
        r"(?:\s+for\s+(?:me|us))?",
        target,
        re.IGNORECASE,
    ):
        return MusicRequest()
    if re.fullmatch(
        r"(?:(?:(?:a|some)\s+songs?\s+from|from)\s+)?(?:my\s+)?"
        r"(?:liked (?:songs?|music)|favorites?|favourites?|"
        r"favou?rite (?:songs?|music))"
        r"(?:\s+playlist)?",
        target,
        re.IGNORECASE,
    ):
        return MusicRequest(personalization_source="liked")
    if re.fullmatch(
        r"(?:(?:a|some)\s+songs?\s+from\s+)?(?:my\s+)?"
        r"daily (?:mix|recommendations?|music|songs?)",
        target,
        re.IGNORECASE,
    ):
        return MusicRequest(personalization_source="daily")

    playlist = re.fullmatch(
        r"(?:(?:(?:a|some)\s+songs?\s+from|from)\s+)?(?:my\s+)?"
        r"(.{1,60}?)\s+playlist",
        target,
        re.IGNORECASE,
    )
    if playlist:
        playlist_name = _strip_target(playlist.group(1))
        if playlist_name.casefold() in {"my", "the", "a", "some"}:
            return None
        return MusicRequest(playlist_name=playlist_name)

    quoted = re.fullmatch(
        r"(?:\"([^\"\r\n]{1,60})\"|“([^”\r\n]{1,60})”|"
        r"'([^'\r\n]{1,60})'|‘([^’\r\n]{1,60})’)",
        target,
    )
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
        if re.fullmatch(_EN_GENERIC_MUSIC_TARGET, song, re.IGNORECASE):
            return MusicRequest(keyword=artist, song_artist=artist)
        song = re.sub(
            r"^(?:(?:a|the)\s+)?song\s+", "", song, flags=re.IGNORECASE
        )
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
    if not normalized or _EN_SECOND_COMMAND.search(_outside_complete_quotes(normalized)):
        return None
    match = _EN_PLAY_COMMAND.fullmatch(normalized)
    if not match:
        return None
    return _parse_en_target(match.group("target"))


def is_strict_music_cancellation(text: str) -> bool:
    """Return True only for a complete, direct stop-music command."""
    normalized = _normalize_command(text)
    if normalized and _ZH_STOP_COMMAND.fullmatch(normalized):
        return True
    english = _normalize_command(text, allow_polite_question=True)
    return bool(english and _EN_STOP_COMMAND.fullmatch(english))
