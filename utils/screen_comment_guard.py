"""Keep chained screen-source narration out of the request view.

The request view replaces the whole body of any assistant message that carries
a confirmed chain with the session locale's ``SCREEN_HISTORY_PLACEHOLDER`` row
(``config/prompts/prompts_screen_history.py``). A saved transcript is
never rewritten, and no user wording restores a quarantined body. This removes
the sample the model would otherwise copy from, which is what feeds the
observed propagation: polluted history in, imitated chains out.

Detection needs a labelled, multi-item chain whose comments reach
``MIN_PROSE``. Unlabelled history and comments below that threshold pass
through unchanged and silently; see the incident record's applicability
and failure-boundary section for the measured limits.

This module deliberately does **not** filter this turn's own output. The
measured propagation stops once the stimulus is removed, so an output-side
guard is a fallback, not the causal fix; adding one without a correct
boundary story would trade a visible chain for truncated legitimate text.
"""
from __future__ import annotations

import os
from copy import copy
from functools import lru_cache

import regex

from config.prompts.prompts_screen_history import SCREEN_HISTORY_PLACEHOLDER


MIN_PROSE = 16
# Operator kill switch, read per request: a false positive in the field can be
# stopped without a release. Not reachable from anything a user says.
SCREEN_GUARD_ENV = "NEKO_SCREEN_HISTORY_GUARD"
_USER_ROLES = {"user", "human"}
_ASSISTANT_ROLES = {"assistant", "ai"}
_LABEL = (
    r"(?:当前)?屏幕(?:搭话|画面|观察|内容|截图|显示)"
    r"|(?:current[ \t]{1,8})?screen[ \t]{1,8}(?:comment|observation|content|display|image)"
)
# Match only through the first separator. No unbounded whitespace lookahead.
# The lexer checks the preceding character; complete and partial matches use
# the same engine (re and regex disagree about Unicode combining characters).
# The bare English label needs a colon: "screen comment " followed by a space
# is ordinary English grammar, while "屏幕搭话" is almost never followed by one.
_MARKER = regex.compile(
    rf"(?:[/／][ \t]{{0,8}}(?:{_LABEL})[\s:：/／]"
    rf"|(?:{_LABEL})[ \t]{{0,8}}[/／]"
    r"|屏幕搭话[\s:：]"
    r"|screen[ \t]{1,8}comment[:：])",
    regex.IGNORECASE,
)
_THINK_TAG = regex.compile(r"</?think(?:ing)?[ \t]{0,8}>", regex.IGNORECASE)
_QUOTES = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"', "'": "'"}


def screen_guard_enabled() -> bool:
    """On unless the operator switch ``NEKO_SCREEN_HISTORY_GUARD`` says off.

    Kept as a function because providers thread its result as a per-call
    override, and because turning the guard off is not something any user
    wording is allowed to request.
    """
    raw = os.environ.get(SCREEN_GUARD_ENV, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _role_and_content(message):
    if isinstance(message, dict):
        return message.get("role"), message.get("content")
    return getattr(message, "type", None), getattr(message, "content", None)


def _is_tool_image_turn(messages, index) -> bool:
    """Whether ``messages[index]`` is a user turn the tool loop injected.

    Tool results that carry pictures are followed by ``{"role": "user"}``
    dicts (see ``_append_tool_result_images``). They are not the user
    speaking, and counting one as "the last user turn" would move the
    assistant run off the real one on every request after such a tool.
    A real user turn in the saved transcript is a message object.
    """
    for back in range(index, -1, -1):
        message = messages[back]
        role, _content = _role_and_content(message)
        if back < index and role == "tool":
            return True
        if not (isinstance(message, dict) and role in _USER_ROLES):
            return False
    return False


def _assistant_tail_run(messages) -> tuple[int, int]:
    """Half-open range of the consecutive assistant messages that answer the
    last user turn; ``(0, 0)`` when there is no such run.

    The run must sit immediately before the last user message *and* be
    preceded by a user message. Both halves are load-bearing and were measured
    separately:

    * ``[u, a, a, ask]`` and ``[u, a×7, ask]`` chain (5/5 and 2/2), so a run
      before the current turn does propagate.
    * ``[a×7, u]`` with no preceding user does **not** chain (0/3).
      This positional condition does not identify message origin: independent
      proactive deliveries can also occur between two user turns and then
      satisfy the same rule. Internal proactive source markers split that run
      during projection; unmarked legacy deliveries remain ambiguous.
    * ``[a×7, u1, ask]`` does not chain either (0/2): an intervening user turn
      ends it.
    * A non-assistant message ends the run, which is why the measured
      tool-boundary case does not chain (0/3).

    Image turns injected by the tool loop are not user turns and are skipped
    when looking for the last one.
    """
    last_user = next(
        (
            index for index in range(len(messages) - 1, -1, -1)
            if _role_and_content(messages[index])[0] in _USER_ROLES
            and not _is_tool_image_turn(messages, index)
        ),
        -1,
    )
    if last_user <= 1:
        return 0, 0
    start = last_user
    while start > 0:
        role, _content = _role_and_content(messages[start - 1])
        if role not in _ASSISTANT_ROLES:
            break
        start -= 1
    if start == 0 or start == last_user:
        return 0, 0
    # Require an actual preceding user; position alone does not prove origin.
    preceding_role, _content = _role_and_content(messages[start - 1])
    if preceding_role not in _USER_ROLES:
        return 0, 0
    return start, last_user


def _is_independent_delivery(message) -> bool:
    metadata = (message.get("additional_kwargs", {}) if isinstance(message, dict)
                else getattr(message, "additional_kwargs", {}))
    return isinstance(metadata, dict) and metadata.get("dialog_source") == "proactive"


def project_screen_history(messages, *, guard_enabled: bool | None = None,
                           placeholder: str | None = None, hits: dict | None = None):
    """Return a request-only view; keep saved transcripts and tool metadata.

    A message carrying a confirmed chain loses its whole body, not just the
    suffix: the surviving normal preamble is dropped too, because leaving a
    truncated sentence in the request is the malformed shape this guard
    exists to keep out. Position, role, ``tool_calls`` and every other key
    are preserved so the provider contract and tool-result pairing stay
    intact. Only the copy is touched; ``messages`` is never mutated.

    A chain may also be spread over several messages. When the consecutive
    assistant run immediately before the last user turn carries one, every
    message in that run is quarantined — judging each message alone would miss
    it. Internally marked proactive deliveries split the run and are checked
    individually. Unmarked legacy deliveries remain positionally ambiguous.

    Detection needs labelled, multi-item chains. Unlabelled history and
    comments below ``MIN_PROSE`` are left byte-for-byte alone, silently.

    ``placeholder`` is the locale row the caller resolved (English when
    omitted). ``hits``, when given, receives the count of quarantined messages
    per category: ``"message"`` for a chain inside one message and ``"run"``
    for one spread over the assistant run.
    """
    if guard_enabled is None:
        guard_enabled = screen_guard_enabled()
    if not guard_enabled:
        return messages
    if placeholder is None:
        placeholder = SCREEN_HISTORY_PLACEHOLDER["en"]
    tail_start, tail_end = _assistant_tail_run(messages)
    tail_quarantine: set[int] = set()
    segment_start = tail_start
    for boundary in range(tail_start, tail_end + 1):
        if boundary < tail_end and not _is_independent_delivery(messages[boundary]):
            continue
        texts = [
            content
            for _role, content in map(_role_and_content, messages[segment_start:boundary])
            if isinstance(content, str)
        ]
        if boundary - segment_start >= 2 and _cached_chain_start_across(tuple(texts)) is not None:
            tail_quarantine.update(range(segment_start, boundary))
        segment_start = boundary + 1
    projected = []
    changed = 0
    for index, message in enumerate(messages):
        role, content = _role_and_content(message)
        if role not in _ASSISTANT_ROLES or not isinstance(content, str):
            projected.append(message)
            continue
        if _cached_chain_start_across((content,)) is not None:
            category = "message"
        elif index in tail_quarantine:
            category = "run"
        else:
            projected.append(message)
            continue
        if hits is not None:
            hits[category] = hits.get(category, 0) + 1
        if isinstance(message, dict):
            message = {**message, "content": placeholder}
        else:
            message = copy(message)
            message.content = placeholder
        projected.append(message)
        changed += 1
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
        elif char in "/／屏当sScC" and not _is_ascii_word_char(self.previous):
            # Only an ASCII word character blocks a marker start, so
            # "screenshot" inside an English word stays inert while a label
            # glued to the preceding Chinese sentence ("…喵屏幕搭话 …") counts.
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
                    self.length = min(MIN_PROSE, self.length + 1)
                if char in "。！？.!?～~…" and self.length >= MIN_PROSE:
                    self.complete = True
            if self.complete:
                return self.previous_start
        return None


def _chain_start_across(texts) -> int | None:
    """Find a chain spread over several messages.

    Each message gets a fresh lexer: its lexical state (an open quote or
    ``<think>``, a fence, a quote block, the word before a marker) ends with
    the message, so a reply ending in a CJK letter or an unclosed quote cannot hide
    the next message's label. One tracker spans them all, positions offset by
    the preceding lengths, so the chain itself may cross the boundary.
    """
    tracker = _ChainTracker()
    offset = 0
    for text in texts:
        lexer = _ScreenLexer()
        for tokens in (lexer.feed(text), lexer.finalize()):
            for token_text, marker, start in tokens:
                cut = tracker.accept(token_text, marker, start + offset)
                if cut is not None:
                    return cut
        offset += len(text)
    return None


# The request view is rebuilt for every provider call (each tool iteration and
# the forced-final one) over a history whose messages never change, and the
# lexer is pure Python. Strings cache their own hash, so a hit costs a lookup.
@lru_cache(maxsize=256)
def _cached_chain_start_across(texts: tuple) -> int | None:
    return _chain_start_across(texts)


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def screen_chain_start(text: str) -> int | None:
    """Find an entire chain in a finished transcript, including its first item."""
    return _chain_start_across([text])
