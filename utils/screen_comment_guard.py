"""Quarantine labelled screen-comment chains without rewriting saved history.

The request view can remove an entire confirmed chain. The streaming guard
cannot retract speech: it emits the first comment and only holds a possible
continuation, with a bounded fail-open buffer. Both use the same incremental
lexer, so Unicode boundaries and protected spans do not depend on chunking.
"""
from __future__ import annotations

import re
from copy import copy

import regex


MAX_PENDING_CHARS = 256
_MIN_PROSE = 16
_LABEL = (
    r"(?:当前)?屏幕(?:搭话|画面|观察|内容|截图|显示)"
    r"|(?:current[ \t]{1,8})?screen[ \t]{1,8}(?:comment|observation|content|display|image)"
)
# Match only through the first separator. No unbounded whitespace lookahead.
# The lexer checks the preceding character; complete and partial matches use
# the same engine (re and regex disagree about Unicode combining characters).
_MARKER = regex.compile(
    rf"(?:[/／][ \t]{{0,8}}(?:{_LABEL})[\s:：/／]"
    rf"|(?:{_LABEL})[ \t]{{0,8}}[/／]"
    r"|(?:屏幕搭话|screen[ \t]{1,8}comment)[\s:：])",
    regex.IGNORECASE,
)
_THINK_TAG = regex.compile(r"</?think(?:ing)?[ \t]{0,8}>", regex.IGNORECASE)
_ACTION = re.compile(r"复述|引用|翻译|回顾|重复|重说|再说|总结|分析|\b(?:repeat|quote|recap|translate|summari\w*|analy[sz]\w*)\b", re.I)
# Match a contiguous reference phrase, not independent keywords in a clause.
# Selectors may modify the conversational object, but cannot skip another
# referent (e.g. "刚才那张截图里的原话"). Bare "原话" is ambiguous.
_REFERENCE_SELECTOR = r"(?:的)?[这那]?(?:[一二三四五六七八九十两0-9]+)?(?:条|段|句|次)?(?:的)?"
_CONVERSATION_OBJECT = r"(?:(?:聊天|对话)(?:历史|记录)|回答|回复|发言|消息|对话|聊天|原话|原文)"
_CHAT_REFERENCE = re.compile(
    r"(?:聊天|对话)(?:历史|记录)"
    rf"|你(?:刚才|之前|以前|先前|上次)?(?:说过|说的){_REFERENCE_SELECTOR}(?:{_CONVERSATION_OBJECT}|话)"
    rf"|(?:刚才|之前|以前|先前|前面|上面|上次|上一|前一|你|助手|这|那){_REFERENCE_SELECTOR}{_CONVERSATION_OBJECT}"
    r"|\b(?:(?:previous|earlier|original|last|your)\s+(?:answers?|responses?|repl(?:y|ies)|messages?)"
    r"|(?:chat|conversation)\s+(?:history|transcript))\b", re.I,
)
# A nested noun phrase such as "截图里的那段对话" belongs to the outer
# source, not this chat. An earlier explicit chat owner ("你说的...")
# can still establish the reference; no list of external-source names is needed.
_REFERENCE_OWNER = re.compile(r"[的里中上内]\s*$")
_NEGATIVE = re.compile(
    r"不要|不许|禁止|停止|无需|不用|不再|(?:^|\s)(?:请)?(?:你)?别"
    r"|\b(?:don['’]t|do not|stop|never)\b", re.I,
)
_QUOTES = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"', "'": "'"}


def requests_history_reference(text: str) -> bool:
    for clause in re.split(r"[。！？.!?;；\n,，]", text):
        if _NEGATIVE.search(clause):
            continue
        if _ACTION.search(clause):
            for reference in _CHAT_REFERENCE.finditer(clause):
                if not _REFERENCE_OWNER.search(clause[:reference.start()]):
                    return True
        # Discussing a *label* is different from asking about the current view.
        if re.search(_LABEL, clause, re.I) and re.search(
            r"这个词|这个说法|一词|这个前缀|前缀的含义|标签的含义|\b(?:term|phrase)\b", clause, re.I,
        ):
            return True
        if re.search(r"屏幕搭话是什么意思|what does [\"']?screen comment.*mean", clause, re.I):
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


class _ScreenLexer:
    """Incremental source-marker lexer; emitted text is never retained.

    Quotes/escapes, code delimiter lengths and line starts are state, not a
    regex over the accumulated reply. Tokens held for lookahead are bounded by
    the marker/tag grammar. Delimiter runs are counted, not buffered.
    """

    def __init__(self):
        self.position = 0
        self.previous = ""
        self.indent = 0
        self.line_prefix = True
        self.pending = ""
        self.pending_kind = ""
        self.quote = ""
        self.escaped = False
        self.thinking = False
        self.blockquote = False
        self.indented_code = False
        self.code = ""
        self.code_length = 0
        self.fence = False
        self.fence_tail = False
        self.run = ""
        self.run_length = 0
        self.run_opening = False
        self.run_at_start = False

    def _emit(self, text, marker=False):
        start = self.position
        self.position += len(text)
        for char in text:
            if char == "\n":
                self.indent, self.line_prefix = 0, True
            elif char in " \t\r" and self.line_prefix:
                self.indent = min(4, self.indent + (4 if char == "\t" else 1))
            else:
                self.indent, self.line_prefix = 4, False
        self.previous = text[-1:]
        return text, marker, start

    def _end_run(self):
        if self.run_opening:
            if self.run == "`" or self.run_length >= 3:
                self.code = self.run
                self.code_length = self.run_length
                self.fence = self.run_at_start and self.run_length >= 3
        elif self.fence:
            self.fence_tail = self.run_length >= self.code_length
        elif self.run_length == self.code_length:
            self.code = ""
        self.run = ""

    def _accept(self, char):
        if self.pending:
            candidate = self.pending + char
            pattern = _MARKER if self.pending_kind == "marker" else _THINK_TAG
            match = pattern.fullmatch(candidate, partial=True)
            if match is not None:
                self.pending = candidate if match.partial else ""
                if match.partial:
                    return []
                if self.pending_kind == "tag":
                    self.thinking = not candidate.startswith("</")
                return [self._emit(candidate, self.pending_kind == "marker")]
            self.pending = ""
            result = [self._emit(candidate[0])]
            for rest in candidate[1:]:
                result.extend(self._accept(rest))
            return result

        if self.run:
            if char == self.run:
                self.run_length += 1
                return [self._emit(char)]
            self._end_run()
        if self.fence_tail:
            if char == "\n":
                self.code = ""
                self.fence_tail = False
                return [self._emit(char)]
            if char in " \t\r":
                return [self._emit(char)]
            self.fence_tail = False
        if self.code:
            if char == self.code and (not self.fence or self.indent <= 3):
                self.run, self.run_length = char, 1
                self.run_opening = False
            return [self._emit(char)]
        if self.blockquote:
            # An unprefixed continuation of the same paragraph is still a
            # Markdown quote. Fail open until a blank line ends that paragraph.
            if char == "\n" and self.line_prefix:
                self.blockquote = False
            return [self._emit(char)]
        if self.indented_code or (self.line_prefix and self.indent >= 4):
            self.indented_code = char != "\n"
            return [self._emit(char)]
        if self.escaped:
            self.escaped = False
            return [self._emit(char)]
        if self.quote:
            if char == "\\":
                self.escaped = True
            elif char == self.quote:
                self.quote = ""
            return [self._emit(char)]
        if char == "<":
            self.pending, self.pending_kind = char, "tag"
            return []
        if self.thinking:
            return [self._emit(char)]
        if char == "\\":
            self.escaped = True
        elif char == ">" and self.indent <= 3:
            self.blockquote = True
        elif char in "`~" and (char == "`" or self.indent <= 3):
            self.run, self.run_length = char, 1
            self.run_opening, self.run_at_start = True, self.indent <= 3
        elif char in _QUOTES:
            # ASCII apostrophes/inch marks attached to words/numbers are not
            # quote openers. A real quote opened earlier still closes normally.
            attached = self.previous.isalnum() or self.previous == "_"
            if not ((char == '"' and self.previous.isdigit()) or (char == "'" and attached)):
                self.quote = _QUOTES[char]
        elif char in "/／屏当sScC" and not (self.previous.isalnum() or self.previous == "_"):
            self.pending, self.pending_kind = char, "marker"
            return []
        return [self._emit(char)]

    def feed(self, text):
        for char in text:
            yield from self._accept(char)

    def finalize(self):
        pending, self.pending = self.pending, ""
        if pending:
            yield self._emit(pending)
        if self.run:
            self._end_run()


class _ChainTracker:
    def __init__(self):
        self.start = None
        self.previous_start = None
        self.length = 0
        self.complete = False

    def accept(self, text, marker, start):
        if marker:
            self.previous_start = self.start if self.complete else None
            self.start, self.length, self.complete = start, 0, False
        elif self.start is not None:
            for char in text:
                if self.length or not char.isspace():
                    self.length = min(_MIN_PROSE, self.length + 1)
                if char in "。！？.!?～~…" and self.length >= _MIN_PROSE:
                    self.complete = True
            if self.complete:
                return self.previous_start
        return None


def screen_chain_start(text: str) -> int | None:
    """Find an entire chain in a finished transcript, including its first item."""
    lexer, tracker = _ScreenLexer(), _ChainTracker()
    for token in lexer.feed(text):
        cut = tracker.accept(*token)
        if cut is not None:
            return cut
    for token in lexer.finalize():
        cut = tracker.accept(*token)
        if cut is not None:
            return cut
    return None


class ScreenCommentChainFilter:
    """Emit the first comment; suppress only confirmed continuations.

    A potential second comment is held for at most MAX_PENDING_CHARS. Longer
    ambiguous content fails open; subsequent markers can still be checked.
    No emitted text is retracted, retained, or scanned again.
    """

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled
        self.reset()

    def reset(self) -> None:
        self._lexer = _ScreenLexer()
        self._tracker = _ChainTracker()
        self._pending = []
        self._pending_length = 0
        self._holding = False
        self.blocked = False

    @property
    def pending_chars(self):
        return self._pending_length + len(self._lexer.pending)

    def _release(self):
        text = "".join(self._pending)
        self._pending.clear()
        self._pending_length = 0
        self._holding = False
        return text

    def _consume(self, tokens):
        output = []
        for text, marker, start in tokens:
            cut = self._tracker.accept(text, marker, start)
            if marker:
                output.append(self._release())
                self._holding = self._tracker.previous_start is not None
            if self._holding:
                self._pending.append(text)
                self._pending_length += len(text)
                if self._pending_length > MAX_PENDING_CHARS:
                    output.append(self._release())
                elif cut is not None:
                    self.blocked = True
                    self._release()
                    break
            else:
                output.append(text)
        return "".join(output)

    def feed(self, text: str) -> str:
        if not self.enabled:
            return text
        if self.blocked:
            return ""
        output = []
        for char in text:
            output.append(self._consume(self._lexer.feed(char)))
            # Include incomplete lexical tokens in the cap, and apply it at
            # character boundaries rather than provider-dependent chunk ends.
            if self.pending_chars > MAX_PENDING_CHARS:
                output.append(self._release())
            if self.blocked:
                break
        return "".join(output)

    def finalize(self) -> str:
        if self.blocked or not self.enabled:
            return ""
        return self._consume(self._lexer.finalize()) + self._release()
