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
            "请帮我播放林俊杰的音乐",
            "請幫我播放林俊傑的音樂",
            ("auto", False, False, True, True),
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
        ("请停止播放", "請停止播放"),
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
