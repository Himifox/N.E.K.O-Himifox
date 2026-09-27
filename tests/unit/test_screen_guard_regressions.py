"""Review regressions: conservative syntax, chunk invariance and bounded state."""

import pytest

from utils import screen_comment_guard as guard_module


LABEL = "屏幕搭话 "
PROSE = (
    "花园中央的喷泉正在缓缓流水，水面映出了树叶的影子。",
    "远处的小桥旁边停着一辆蓝色自行车，车篮里面放着鲜花。",
    "道路两旁的灯光渐渐亮起，晚归的行人走过安静的街角。",
)
FIRST = LABEL + PROSE[0]
SECOND = LABEL + PROSE[1]
CHAIN = FIRST + SECOND


def filtered(chunks):
    guard = guard_module.ScreenCommentChainFilter()
    return "".join(guard.feed(chunk) for chunk in chunks) + guard.finalize()


def assert_at_every_split(text, expected):
    assert filtered([text]) == expected
    assert filtered(list(text)) == expected
    for split in range(len(text) + 1):
        assert filtered([text[:split], text[split:]]) == expected


@pytest.mark.parametrize("user_text", [
    "现在的屏幕画面怎么样？",
    "看看屏幕内容，接下来该怎么操作？",
    "请帮我翻译菜单。",
    "Translate the menu on this screen.",
    "Describe the current screen image.",
    "屏幕画面上那些标签是什么？",
    "帮我分析刚才那波团战",
    "总结上一局的战术",
    "翻译刚才那张截图里的菜单",
    "分析刚才的内容",
    "分析历史战役的战术",
    "Analyze the previous battle.",
    "Summarize the earlier game.",
    "Translate the original menu on this screen.",
])
def test_current_screen_and_translation_requests_do_not_exempt_history(user_text):
    history = [{"role": "assistant", "content": "好的。" + CHAIN},
               {"role": "user", "content": user_text}]
    assert guard_module.screen_guard_enabled(history)
    projected = guard_module.project_screen_history(history)
    assert projected[0]["content"] == "好的。"
    assert history[0]["content"] == "好的。" + CHAIN


@pytest.mark.parametrize("user_text", [
    "请分别复述刚才的两段原话。",
    "请翻译上一条回答。",
    "Please quote the previous response.",
    "分析刚才的回答",
    "总结之前的聊天记录",
    "请引用你说过的话",
    "翻译你上一条回复的原文",
    "Please analyze your answer.",
    "Summarize our chat history.",
    "Translate the original response.",
])
def test_explicit_history_reference_is_not_mistaken_for_a_stop_request(user_text):
    history = [{"role": "assistant", "content": CHAIN},
               {"role": "user", "content": user_text}]
    assert not guard_module.screen_guard_enabled(history)
    assert guard_module.project_screen_history(history) is history


def test_measurement_quote_does_not_protect_the_remaining_reply():
    prefix = '这是一个6"显示屏。'
    text = prefix + CHAIN
    assert guard_module.screen_chain_start(text) == len(prefix)
    assert_at_every_split(text, prefix + FIRST)


@pytest.mark.parametrize("text", [
    "示例：``" + CHAIN + "``，以上是代码。",
    "````markdown\n```\n" + CHAIN + "\n```\n````\n结束。",
    '他说"' + CHAIN + '"，以上是引用。',
    "    " + CHAIN + "\n\n正常结尾。",
    "\t" + CHAIN + "\n\n正常结尾。",
    "> 示例：\n" + CHAIN + "\n\n正常结尾。",
])
def test_matching_markdown_delimiter_lengths_preserve_code(text):
    assert guard_module.screen_chain_start(text) is None
    assert_at_every_split(text, text)


@pytest.mark.parametrize("prefix", ["❤️", "a\u0301", "🙂\u200d"])
@pytest.mark.parametrize("suffix,expected", [(FIRST, FIRST), (CHAIN, FIRST)])
def test_unicode_boundaries_never_duplicate_or_leak_partial_labels(prefix, suffix, expected):
    assert_at_every_split(prefix + suffix, prefix + expected)


def test_first_comment_is_streamed_before_completion_and_before_finalize():
    guard = guard_module.ScreenCommentChainFilter()
    opening = LABEL + PROSE[0][:8]
    assert guard.feed(opening) == opening
    assert guard.feed(PROSE[0][8:]) == PROSE[0][8:]
    assert guard.finalize() == ""


def test_second_comment_is_held_then_suppressed_only_after_confirmation():
    guard = guard_module.ScreenCommentChainFilter()
    assert guard.feed(FIRST) == FIRST
    assert guard.feed(LABEL + PROSE[1][:-1]) == ""
    assert not guard.blocked
    assert guard.feed(PROSE[1][-1]) == ""
    assert guard.blocked
    assert guard.feed(LABEL + PROSE[2]) == ""
    assert guard.finalize() == ""


def test_unconfirmed_second_comment_is_preserved_at_end_of_stream():
    text = FIRST + LABEL + "只有一个很短的片段"
    assert_at_every_split(text, text)


def test_closed_code_and_quote_do_not_protect_later_real_chains():
    for prefix in ('引用：“' + CHAIN + '”\n',
                   "````markdown\n```\n" + CHAIN + "\n```\n````\n",
                   "    " + CHAIN + "\n\n",
                   "> 示例：\n" + CHAIN + "\n\n"):
        assert guard_module.screen_chain_start(prefix + CHAIN) == len(prefix)
        assert_at_every_split(prefix + CHAIN, prefix + FIRST)


def test_partial_marker_is_included_in_pending_cap_without_chunk_dependence():
    ambiguous = LABEL + "字" * (guard_module.MAX_PENDING_CHARS - len(LABEL) - 3)
    # No completed second item: reach the cap while the next marker is partial.
    text = FIRST + ambiguous + " screen observation/" + PROSE[1]
    expected = filtered([text])
    guard = guard_module.ScreenCommentChainFilter()
    output = []
    for char in text:
        output.append(guard.feed(char))
        assert guard.pending_chars <= guard_module.MAX_PENDING_CHARS
    assert "".join(output) + guard.finalize() == expected
    assert_at_every_split(text, expected)


def test_pending_comment_fails_open_at_the_cap_and_later_chains_are_detected():
    guard = guard_module.ScreenCommentChainFilter()
    ambiguous = LABEL + "字" * (guard_module.MAX_PENDING_CHARS + 16)
    assert guard.feed(FIRST) == FIRST
    released_chunks = []
    for char in ambiguous:
        released_chunks.append(guard.feed(char))
        assert guard.pending_chars <= guard_module.MAX_PENDING_CHARS
    released = "".join(released_chunks)
    assert released == ambiguous, "a long ambiguous comment must not wait for EOF"
    assert not guard.blocked
    # A short intervening comment cannot confirm a chain. The next two full
    # comments must still be detected after the earlier buffer failed open.
    rest = "\n" + LABEL + "短。" + CHAIN
    visible = "".join(guard.feed(char) for char in rest) + guard.finalize()
    assert visible == "\n" + LABEL + "短。" + FIRST
    assert guard.blocked


def retained_text_size(value, seen=None):
    """Inspect instance state without coupling this check to private field names."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(retained_text_size(item, seen) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return sum(retained_text_size(item, seen) for item in value)
    if not isinstance(value, type) and hasattr(value, "__dict__"):
        return retained_text_size(vars(value), seen)
    return 0


def test_long_plain_stream_retains_bounded_state_and_never_rescans_its_history(monkeypatch):
    scanned_sizes = []
    # If the implementation reuses these batch helpers, they must receive only
    # a bounded pending window, never the previously emitted conversation.
    for name in ("_markers", "screen_chain_start"):
        original = getattr(guard_module, name, None)
        if original is not None:
            def record(text, *args, _original=original, **kwargs):
                scanned_sizes.append(len(text))
                return _original(text, *args, **kwargs)
            monkeypatch.setattr(guard_module, name, record)

    guard = guard_module.ScreenCommentChainFilter()
    chunk = "花园里的树叶随风摇动，小鸟在枝头唱歌。"
    bound = guard_module.MAX_PENDING_CHARS + 128
    for _ in range(2000):
        assert guard.feed(chunk) == chunk
        assert guard.pending_chars <= guard_module.MAX_PENDING_CHARS
        assert retained_text_size(guard) <= bound
    assert guard.finalize() == ""
    assert max(scanned_sizes, default=0) <= bound
