"""Public behavior coverage migrated from the removed legacy music parser tests."""

from __future__ import annotations

import pytest

from main_logic.music_command_parser import (
    is_strict_music_cancellation,
    parse_strict_music_command,
)


def _request_shape(text: str) -> tuple[str, bool, bool, bool, bool] | None:
    request = parse_strict_music_command(text)
    if request is None:
        return None
    return (
        request.personalization_source,
        bool(request.playlist_name),
        bool(request.song_name),
        bool(request.song_artist),
        bool(request.keyword),
    )


@pytest.mark.parametrize(
    ("simplified", "traditional", "expected_shape"),
    (
        ("听一首晴天", "聽一首晴天", ("auto", False, True, False, True)),
        (
            "播放周杰伦的晴天这首歌",
            "播放周杰倫的晴天這首歌",
            ("auto", False, True, True, True),
        ),
        (
            "播放我的红心歌单",
            "播放我的紅心歌單",
            ("liked", False, False, False, False),
        ),
        (
            "播放网易云的日推",
            "播放網易雲的日推",
            ("daily", False, False, False, False),
        ),
        (
            "播放《告白气球》",
            "播放《告白氣球》",
            ("auto", False, True, False, True),
        ),
        (
            "播放我的健身歌单",
            "播放我的健身歌單",
            ("auto", True, False, False, False),
        ),
    ),
)
def test_direct_music_commands_keep_simplified_traditional_parity(
    simplified: str,
    traditional: str,
    expected_shape: tuple[str, bool, bool, bool, bool],
) -> None:
    assert _request_shape(simplified) == expected_shape
    assert _request_shape(traditional) == expected_shape


@pytest.mark.parametrize(
    ("simplified", "traditional"),
    (
        ("请停止播放音乐", "請停止播放音樂"),
        ("暂停当前音乐", "暫停當前音樂"),
        ("关掉音乐", "關掉音樂"),
        ("别再放歌", "別再放歌"),
    ),
)
def test_direct_music_stops_keep_simplified_traditional_parity(
    simplified: str,
    traditional: str,
) -> None:
    assert is_strict_music_cancellation(simplified) is True
    assert is_strict_music_cancellation(traditional) is True


@pytest.mark.parametrize(
    ("simplified", "traditional"),
    (
        ("播放一下这个视频", "播放一下這個影片"),
        ("播放这个游戏", "播放這個遊戲"),
        ("别人都在听音乐", "別人都在聽音樂"),
        ("别致的音乐", "別緻的音樂"),
        ("别播放视频", "別播放影片"),
        ("要不要停止播放", "要不要停止播放"),
        ("我想停止播放吗？", "我想停止播放嗎？"),
    ),
)
def test_non_commands_fail_closed_in_both_scripts(
    simplified: str,
    traditional: str,
) -> None:
    for text in (simplified, traditional):
        assert parse_strict_music_command(text) is None
        assert is_strict_music_cancellation(text) is False


@pytest.mark.parametrize(
    ("simplified", "traditional"),
    (
        ("播放晴天", "播放晴天"),
        ("播放林俊杰的音乐", "播放林俊傑的音樂"),
        ("播放轻松的音乐", "播放輕鬆的音樂"),
        ("播放一段你说话的声音", "播放一段你說話的聲音"),
        ("我想听晴天", "我想聽晴天"),
        ("来点摇滚", "來點搖滾"),
        ("从我的健身歌单里随机放一首", "從我的健身歌單裡隨機放一首"),
    ),
)
def test_non_strict_requests_are_left_for_the_model_tool(
    simplified: str,
    traditional: str,
) -> None:
    assert parse_strict_music_command(simplified) is None
    assert parse_strict_music_command(traditional) is None


@pytest.mark.parametrize(
    "text",
    (
        "播放我的歌",
        "播放你的晴天",
        "播放妳的晴天",
        "播放咱的晴天",
        "播放咱们的晴天",
        "播放咱們的晴天",
        "放假",
        "放大一点",
        "play with me",
        "play with us",
        "play Yellow",
        "play chess",
        "play football",
        "play soccer",
        "play basketball",
    ),
)
def test_ambiguous_or_non_music_play_phrases_fail_closed(text: str) -> None:
    assert parse_strict_music_command(text) is None


@pytest.mark.parametrize(
    ("text", "song", "artist"),
    (
        ("播放周杰伦的晴天这首歌", "晴天", "周杰伦"),
        ("播放周杰伦的晴天歌曲", "晴天", "周杰伦"),
        ("play the song Yellow by Coldplay", "Yellow", "Coldplay"),
    ),
)
def test_explicit_song_wrappers_are_removed(
    text: str,
    song: str,
    artist: str,
) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.song_name == song
    assert request.song_artist == artist


@pytest.mark.parametrize(
    "text",
    ("play me a song", "play some music for me", "play music for us"),
)
def test_generic_english_recipient_phrases_do_not_become_searches(text: str) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.keyword == ""


@pytest.mark.parametrize(
    "text",
    (
        "play favorites",
        "play my favourites",
        "play a song from my liked songs",
    ),
)
def test_english_liked_aliases_are_personalized(text: str) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.personalization_source == "liked"


def test_english_playlist_source_wrapper_is_not_part_of_the_name() -> None:
    request = parse_strict_music_command("play a song from my Night Loop playlist")
    assert request is not None
    assert request.playlist_name == "Night Loop"


def test_generic_song_by_artist_becomes_an_artist_request() -> None:
    request = parse_strict_music_command("play a song by Coldplay")
    assert request is not None
    assert request.song_name == ""
    assert request.song_artist == "Coldplay"


@pytest.mark.parametrize("text", ("取消播放音乐", "不要再听歌了", "不要再聽歌了"))
def test_additional_direct_stop_phrases_are_recognized(text: str) -> None:
    assert is_strict_music_cancellation(text) is True


def test_non_music_cancel_phrase_is_not_a_music_stop() -> None:
    assert is_strict_music_cancellation("取消收藏这首歌") is False


@pytest.mark.parametrize(
    "text",
    (
        "play some songs",
        "play the music",
        "play a track",
        "play some tunes for us",
    ),
)
def test_additional_generic_english_targets_do_not_become_searches(text: str) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.keyword == ""


@pytest.mark.parametrize(
    "text",
    ("listen to Yellow", "please listen to Yellow", "I want to play Yellow"),
)
def test_unlabeled_english_targets_are_left_for_the_model_tool(text: str) -> None:
    assert parse_strict_music_command(text) is None


@pytest.mark.parametrize(
    ("text", "song"),
    (
        ('play "Yellow"', "Yellow"),
        ("play song: Yellow", "Yellow"),
        ("play track: Fix You", "Fix You"),
    ),
)
def test_labeled_or_quoted_english_songs_are_strict(text: str, song: str) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.song_name == song
    assert request.keyword == song


@pytest.mark.parametrize(
    ("text", "song"),
    (
        ("换成歌曲：晴天", "晴天"),
        ("換成歌曲：晴天", "晴天"),
        ("切到音乐：稻香", "稻香"),
        ("改放曲目：告白气球", "告白气球"),
    ),
)
def test_labeled_chinese_switch_commands_are_strict(text: str, song: str) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.song_name == song
    assert request.keyword == song


@pytest.mark.parametrize(
    "text",
    (
        "停止播放音乐",
        "暂停播放歌曲",
        "停止播放音樂",
        "暫停播放歌曲",
        "can you stop music",
        "could you pause playback",
        "stop playing music",
        "don't listen to music",
        "do not listen to songs",
    ),
)
def test_composed_music_stop_commands_are_recognized(text: str) -> None:
    assert is_strict_music_cancellation(text) is True


@pytest.mark.parametrize(
    "text",
    ("停止播放", "停止播放视频", "stop playing games", "don't listen to podcasts"),
)
def test_stop_commands_without_a_music_object_are_rejected(text: str) -> None:
    assert is_strict_music_cancellation(text) is False


@pytest.mark.parametrize(
    "text",
    ("turn the music off", "turn the songs off", "shut the playback off"),
)
def test_object_before_off_english_stops_are_recognized(text: str) -> None:
    assert is_strict_music_cancellation(text) is True


@pytest.mark.parametrize(
    "text",
    ("停止播放红心歌单", "停止我的红心歌单", "暂停播放每日推荐"),
)
def test_source_named_chinese_stops_are_recognized(text: str) -> None:
    assert is_strict_music_cancellation(text) is True


@pytest.mark.parametrize(
    "text",
    ("play Yellow and play Fix You", "play Yellow but listen to Fix You"),
)
def test_chained_english_play_commands_fail_closed(text: str) -> None:
    assert parse_strict_music_command(text) is None


@pytest.mark.parametrize(
    "text",
    ("播放我的歌单", "播放我的播放列表", "播放我的播放清單"),
)
def test_bare_possessive_playlists_are_generic(text: str) -> None:
    request = parse_strict_music_command(text)
    assert request is not None
    assert request.playlist_name == ""
    assert request.keyword == ""
