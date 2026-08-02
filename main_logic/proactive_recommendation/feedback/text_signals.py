"""Pure explicit-text feedback signal detection."""

from __future__ import annotations

from difflib import SequenceMatcher
import re

from .events import _clean_text, _normalize_source_type

_EXPLICIT_TEXT_PREFERENCE_SOURCE_ALIASES = {
    "news": ("新闻", "新聞", "资讯", "資訊", "news"),
    "meme": ("表情包", "梗图", "梗圖", "meme", "sticker"),
    "vision": ("屏幕内容", "屏幕信息", "窗口内容", "螢幕內容", "視窗內容", "vision"),
    "video": ("视频", "影片", "video"),
}

_EXPLICIT_TEXT_SOURCE_REJECTION_MARKERS = (
    "少推荐",
    "少推薦",
    "别推荐",
    "別推薦",
    "不要推荐",
    "不要推薦",
    "别发",
    "別發",
    "不要发",
    "不要發",
    "不喜欢",
    "不喜歡",
    "不感兴趣",
    "不感興趣",
    "不太感兴趣",
    "不太感興趣",
    "没兴趣",
    "沒興趣",
    "不想看",
    "stop recommending",
    "stop sending",
    "show me less",
    "not interested",
    "don't recommend",
    "do not recommend",
)

_EXPLICIT_TEXT_QUALITY_NEGATIVE_MARKERS = (
    "不好看",
    "不好笑",
    "没意思",
    "沒意思",
    "无聊",
    "無聊",
    "不行",
    "没用",
    "沒用",
    "不相关",
    "不相關",
    "boring",
    "not useful",
    "irrelevant",
)

_EXPLICIT_TEXT_POSITIVE_MARKERS = (
    "多推荐",
    "多推薦",
    "多发",
    "多發",
    "再来点",
    "再來點",
    "喜欢",
    "喜歡",
    "感兴趣",
    "感興趣",
    "想看",
    "爱看",
    "愛看",
    "show me more",
    "more like this",
    "i like",
    "interested in",
)

_EXPLICIT_TEXT_DEICTIC_NEGATIVE = (
    "少推荐这",
    "少推薦這",
    "别推荐这",
    "別推薦這",
    "不要推荐这",
    "不要推薦這",
    "不想看这类",
    "不想看這類",
    "不喜欢这类内容",
    "不喜歡這類內容",
    "不喜欢这种内容",
    "不喜歡這種內容",
)

_EXPLICIT_TEXT_DEICTIC_POSITIVE = (
    "多推荐这",
    "多推薦這",
    "想看这类",
    "想看這類",
    "喜欢这类内容",
    "喜歡這類內容",
    "喜欢这种内容",
    "喜歡這種內容",
)

_EXPLICIT_TEXT_NEGATION_EXCEPTIONS = (
    "不无聊",
    "不無聊",
    "不是没意思",
    "不是沒意思",
    "不是不喜欢",
    "不是不喜歡",
    "没觉得无聊",
    "沒覺得無聊",
    "别不推荐",
    "別不推薦",
)

_EXPLICIT_TEXT_DEICTIC_RE = re.compile(
    r"(?:这个|這個|这种|這種|这类|這類|这条|這條|这张|這張|刚才那个|剛才那個)"
)

_EXPLICIT_TEXT_SWITCH_RE = re.compile(
    r"(?:换一个|換一個|换个|換個|下一个|下一個|来点别的|來點別的|"
    r"聊点别的|聊點別的|跳过|跳過|somethingelse|nextone)"
)

_EXPLICIT_TEXT_FATIGUE_RE = re.compile(
    r"(?:又是|老是|怎么还是|怎麼還是|太多了?|重复了?|重複了?|看腻了?|看膩了?)"
)


def _compact_feedback_text(text: str | None) -> str:
    return "".join(
        character
        for character in _clean_text(text).casefold()[:256]
        if character.isalnum()
    )


def _contains_feedback_phrase(
    normalized: str,
    phrases: Iterable[str],
    *,
    fuzzy: bool = False,
) -> bool:
    for raw_phrase in phrases:
        phrase = _compact_feedback_text(raw_phrase)
        if not phrase:
            continue
        if phrase in normalized:
            return True
        if not fuzzy or len(phrase) < 3:
            continue
        threshold = 0.66 if len(phrase) == 3 else 0.75 if len(phrase) == 4 else 0.82
        for window_size in range(max(2, len(phrase) - 1), len(phrase) + 2):
            for start in range(0, len(normalized) - window_size + 1):
                window = normalized[start : start + window_size]
                if SequenceMatcher(None, window, phrase).ratio() >= threshold:
                    return True
    return False


def _explicit_text_named_source_types(text: str | None) -> tuple[str, ...]:
    normalized = _compact_feedback_text(text)
    if not normalized:
        return ()
    return tuple(
        source_type
        for source_type, aliases in _EXPLICIT_TEXT_PREFERENCE_SOURCE_ALIASES.items()
        if _contains_feedback_phrase(normalized, aliases, fuzzy=True)
    )


def _explicit_text_source_preference_event_type(
    text: str | None,
    source_type: str,
) -> tuple[str, str] | None:
    aliases = _EXPLICIT_TEXT_PREFERENCE_SOURCE_ALIASES.get(source_type)
    normalized = _compact_feedback_text(text)
    if not aliases or not normalized:
        return None
    if _contains_feedback_phrase(normalized, _EXPLICIT_TEXT_NEGATION_EXCEPTIONS):
        return None
    source_named = _contains_feedback_phrase(normalized, aliases, fuzzy=True)
    deictic = bool(_EXPLICIT_TEXT_DEICTIC_RE.search(normalized))
    if source_named and _contains_feedback_phrase(
        normalized,
        _EXPLICIT_TEXT_SOURCE_REJECTION_MARKERS,
    ):
        return "source_not_interested", "explicit_source_rejection"
    if _contains_feedback_phrase(normalized, _EXPLICIT_TEXT_DEICTIC_NEGATIVE):
        return "source_not_interested", "explicit_source_rejection"
    if (source_named or deictic) and _EXPLICIT_TEXT_FATIGUE_RE.search(normalized):
        return "source_fatigue", "explicit_source_fatigue"
    quality_negative = _contains_feedback_phrase(
        normalized,
        _EXPLICIT_TEXT_QUALITY_NEGATIVE_MARKERS,
    )
    if _EXPLICIT_TEXT_SWITCH_RE.search(normalized) or (
        quality_negative and (source_named or deictic)
    ):
        return "candidate_not_interested", "explicit_candidate_rejection"
    if source_named and _contains_feedback_phrase(
        normalized,
        _EXPLICIT_TEXT_POSITIVE_MARKERS,
    ):
        return "source_interested", "explicit_source_interest"
    if _contains_feedback_phrase(normalized, _EXPLICIT_TEXT_DEICTIC_POSITIVE):
        return "source_interested", "explicit_source_interest"
    return None


def detect_source_feedback_signal(
    text: str | None,
    source_type: str,
) -> tuple[str, str] | None:
    return _explicit_text_source_preference_event_type(text, source_type)
