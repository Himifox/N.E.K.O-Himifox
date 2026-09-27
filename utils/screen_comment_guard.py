"""Conservative detection of a leaked *series* of screen-source narrations.

This is not a word blacklist. One label, ordinary mentions, quoted examples and
code are retained. A chain needs two unquoted labels introducing substantial
prose. History callers use a request-only view, never erase the saved transcript.
"""
from __future__ import annotations

import re
from copy import copy

import regex


_LABEL = (
    r"(?:当前)?屏幕(?:搭话|画面|观察|内容|截图|显示)"
    r"|(?:current[ \t]{1,8})?screen[ \t]{1,8}(?:comment|observation|content|display|image)"
)
_MARKER = re.compile(
    rf"(?<![\w])(?:"
    rf"[/／][ \t]{{0,8}}(?:{_LABEL})(?:\s*[:：/／]\s*|\s+)"
    rf"|(?:{_LABEL})[ \t]{{0,8}}[/／]\s*"
    r"|(?:屏幕搭话|screen[ \t]{1,8}comment)(?:[ \t]*[:：][ \t]*|\s+)"
    r")",
    re.IGNORECASE,
)
_PARTIAL_MARKER = regex.compile(_MARKER.pattern, regex.IGNORECASE)
# Fail open for requests about earlier wording. This also covers explanations
# of the labels themselves; those must not be damaged by an output filter.
_REFERENCE = re.compile(
    rf"{_LABEL}|复述|原话|原文|引用|翻译|回顾|再说|刚才.*说"
    r"|^(?:请|帮我|麻烦你?)?(?:再|重新)?重复"
    r"|\b(?:repeat|quote|recap|translate)\b",
    re.IGNORECASE,
)
_PROTECTED = re.compile(
    r"<think>[\s\S]*?(?:</think>|$)"
    r"|```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)"
    r"|`[^`]*(?:`|$)|“[^”]*(?:”|$)|「[^」]*(?:」|$)"
    r"|『[^』]*(?:』|$)|‘[^’]*(?:’|$)|(?<!\w)'(?:\\[\s\S]?|[^'\\])*(?:'|$)"
    r'|"(?:\\[\s\S]?|[^"\\])*(?:"|$)|(?m:^\s*>[^\n]*)',
    re.IGNORECASE,
)
_MIN_PROSE = 16


def requests_history_reference(text: str) -> bool:
    # A request to STOP replaying is not permission to replay. Keep the
    # conservative quote exemption local to affirmative clauses.
    for clause in re.split(r"[。！？.!?;；\n,，]", text):
        if re.search(r"别|不要|不许|禁止|停止|无需|不用|不再|\b(?:don['’]t|do not|stop|never)\b", clause, re.I):
            continue
        if _REFERENCE.search(clause):
            return True
        if (re.search(r"总结|分析|\b(?:summari\w*|analy[sz]\w*)\b", clause, re.I)
                and re.search(r"之前|刚才|上一|前面|上面|历史|\b(?:previous|earlier|last|history)\b", clause, re.I)):
            return True
    return False


def screen_guard_enabled(messages) -> bool:
    for message in reversed(messages):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "type", None)
        if role not in {"user", "human"}:
            continue
        content = message.get("content") if isinstance(message, dict) else message.content
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return not requests_history_reference(content if isinstance(content, str) else "")
    return True


def project_screen_history(messages, *, guard_enabled: bool | None = None):
    """Return a request-only view; keep saved transcripts and tool metadata."""
    if guard_enabled is None:
        guard_enabled = screen_guard_enabled(messages)
    if not guard_enabled:
        return messages
    projected = []
    changed = False
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "type", None)
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        cut = screen_chain_start(content) if role in {"assistant", "ai"} and isinstance(content, str) else None
        if cut is not None:
            message = copy(message)
            if isinstance(message, dict):
                message["content"] = content[:cut]
            else:
                message.content = content[:cut]
            changed = True
        projected.append(message)
    return projected if changed else messages


def _markers(text: str):
    protected = [(match.start(), match.end()) for match in _PROTECTED.finditer(text)]
    return [
        match for match in _MARKER.finditer(text)
        if not any(start <= match.start() < end for start, end in protected)
    ]


def screen_chain_start(text: str) -> int | None:
    """Find a source-labelled narration suffix, not a mention of a label."""
    markers = _markers(text)
    for index, (first, second) in enumerate(zip(markers, markers[1:])):
        between = text[first.end():second.start()].strip()
        end = markers[index + 2].start() if index + 2 < len(markers) else len(text)
        after = text[second.end():end].strip()
        # Count completed prose only: an incomplete *next* marker must not
        # temporarily make a short second item look substantial at a split.
        if all(max((m.end() for m in re.finditer(r"[。！？.!?～~…]", part)), default=0) >= _MIN_PROSE
               for part in (between, after)):
            return first.start()
    return None


class ScreenCommentChainFilter:
    """Hold a possible chain until its second narration is confirmed.

    No committed text is retracted. Partial labels alone are buffered across
    provider chunk boundaries; ordinary text streams immediately. A candidate is flushed at
    end-of-stream; a confirmed chain suppresses the remaining narration suffix.
    """

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled
        self.reset()

    def reset(self) -> None:
        self._text = ""
        self._emitted = 0
        self.blocked = False

    def feed(self, text: str) -> str:
        if not self.enabled:
            return text
        if self.blocked:
            return ""
        self._text += text
        cut = screen_chain_start(self._text)
        if cut is not None:
            self.blocked = True
            visible = self._text[self._emitted:cut]
            self._text = ""
            return visible
        markers = _markers(self._text)
        end = markers[0].start() if markers else len(self._text)
        if not markers:
            protected = [(m.start(), m.end()) for m in _PROTECTED.finditer(self._text)]
            for match in _PARTIAL_MARKER.finditer(self._text, partial=True):
                if match.partial and not any(start <= match.start() < stop for start, stop in protected):
                    end = match.start()
                    break
        visible = self._text[self._emitted:end]
        self._emitted = end
        return visible

    def finalize(self) -> str:
        if self.blocked or not self.enabled:
            return ""
        visible = self._text[self._emitted:]
        self._text = ""
        self._emitted = 0
        return visible
