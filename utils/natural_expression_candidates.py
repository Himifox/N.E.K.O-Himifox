#!/usr/bin/env python3
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic natural-expression candidate analysis shared by local tools.

The analysis is pure and review-only. It never discovers conversation files,
calls a model or network service, edits the runtime rule table, or activates a
candidate. Its output is intentionally incompatible with
``config.prompts.prompts_slop.SLOP_RULES``.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_VERSION = "natural-expression-candidates/v1"
ARTIFACT_TYPE = "maintainer_review_candidates"
USER_REVIEW_ARTIFACT_TYPE = "user_review_candidates"
DEFAULT_THRESHOLD = 3
DEFAULT_MESSAGE_COUNT_THRESHOLD = 3
DEFAULT_WORD_NGRAM_MIN = 2
DEFAULT_WORD_NGRAM_MAX = 5
DEFAULT_CJK_NGRAM_MIN = 4
DEFAULT_CJK_NGRAM_MAX = 8
DEFAULT_MIN_LENGTH = 4
USER_REVIEW_MAX_INPUT_CHARACTERS = 128 * 1024
USER_REVIEW_MAX_OCCURRENCES = 100_000
USER_REVIEW_MAX_CANDIDATES = 200
# Below this, halving a single reply has stopped being narrowing and the
# budget itself is the problem. Mining generates a fixed number of n-grams
# per character -- about five for CJK -- so 512 characters is roughly 2,500
# occurrences, far inside any sane limit. Reaching the floor means the limit
# was configured too small to analyse anything, which is worth surfacing.
_USER_REVIEW_MIN_TRUNCATED_CHARACTERS = 512
# How far above the AVERAGE OF THE REST a message has to sit before it is
# worth cutting its body rather than evicting messages. "Longer than all the
# others combined" was too strict: it catches exactly one outlier, so two
# comparably heavy replies evaded it and the eviction that followed threw
# away the ordinary replies carrying the repeated phrase -- the very defect
# the single-outlier clip was added to stop. At a ratio of 1 a uniformly
# large window would qualify and every body would be cut, which is the
# behaviour ``test_a_uniformly_large_window_narrows_without_cutting_a_body``
# exists to forbid; 2 is the smallest integer that separates the two.
_USER_REVIEW_OUTLIER_RATIO = 2

_LANGUAGE_ALIASES = {
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "es": "es",
    "es-es": "es",
    "es-mx": "es",
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
    "ru": "ru",
    "ru-ru": "ru",
    "ja": "ja",
    "ja-jp": "ja",
    "ko": "ko",
    "ko-kr": "ko",
    "zh": "zh",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
}
_WHITESPACE_LANGUAGES = frozenset({"en", "es", "pt", "ru"})
_TEXT_BOUNDARY_RE = re.compile(r"[\r\n.!?。！？；;:：,，、]+")
# A URL ends at CJK punctuation. The old tail excluded only whitespace and
# brackets, and CJK prose has neither -- so a reply reading
# "请看https://a.com。我们一起去吃饭吧！" protected the sentence terminator AND
# every following sentence on the line, deleting the catchphrase after the URL.
# ``re`` reads the ``\uXXXX`` escapes itself, so these stay raw and legible:
# general CJK punctuation, then the fullwidth ASCII punctuation blocks.
_URL_STOP = (
    r"\u2018\u2019\u201c\u201d\u2026"
    r"\u3000-\u303f\uff01-\uff0f\uff1a-\uff20"
    r"\uff3b-\uff40\uff5b-\uff65"
)
_URL_ATOM = r"[^\s<>()" + _URL_STOP + r"]"
# An address is not a URL tail. ``_URL_ATOM`` admits "@" and ".", so the two
# internationalized address branches below -- written with it -- let the
# local part, the domain and every label compete for the same characters:
# "a@a@a@a..." went quadratic, 0.73s at 16 KB against an accepted reply size
# of 128 KiB, and on the live turn path since the effects sidecar masks
# every draft. Without those branches the same input is linear, so this is
# their cost and theirs to pay.
#
# The local part keeps "." -- it is the identifying half and matching only
# part of it is worse than not matching at all -- but excludes "@", and is
# ATOMIC: it stops at the first "@", and a character handed back is by
# construction not one, so backtracking into it can never help. Each domain
# LABEL excludes both, which is what makes the dotted tail unambiguous.
_EMAIL_LOCAL = r"(?>[^\s<>()@" + _URL_STOP + r"]+)"
_EMAIL_LABEL = r"(?>[^\s<>()@." + _URL_STOP + r"]+)"
_EMAIL_ADDRESS = _EMAIL_LOCAL + r"@" + _EMAIL_LABEL + r"(?:\." + _EMAIL_LABEL + r")+"
_URL_ATOM_RE = re.compile(_URL_ATOM)
# Parentheses are NOT in the pattern. A path nests them to any depth
# (``/f(g(x))``), and one level encoded here stopped at the inner ``(``,
# leaving the rest of the path minable -- and minable means persisted to the
# effects sidecar for 120 days, by a module whose whole promise is that it
# never persists a URL. ``_url_spans`` extends each match instead.
_URL_TAIL = _URL_ATOM
_URL_RE = re.compile(
    r"(?i:https?://|www\.)" + _URL_TAIL + "+|"
    # A "mailto:" needs no alternative of its own. The bare-address rule
    # below takes the scheme into its local part and covers every case this
    # one did -- measured over 4913 address-shaped drafts, the only six
    # where the two differ are six where the mailto form protected LESS.
    # A rule that can never be the reason something is protected is a rule
    # no test can hold, which is how its guard came to pass with it deleted.
    # ANY scheme, as a rule rather than a list. A fixed allowlist guarantees
    # another round of "you missed one", and the ones it missed carried real
    # payloads: an otpauth:// TOTP secret, a postgres:// password, an
    # ssh/magnet/sms target and a Windows path all reached the 120-day
    # sidecar verbatim.
    #
    # The two lookaheads are what keep this off speech: after the colon
    # there must be an OPAQUE PART -- at least two atoms, at least one of
    # them alphanumeric -- so "together:D", "3:4" and a CJK sentence after
    # "note:" match nothing. Without them the rule runs to end of text on
    # CJK, which is the one over-protection shape this module refuses.
    #
    # Given up deliberately, since none carries a payload: the degenerate
    # "data:,", "tel:5" and "mailto:a" the old list covered. A real
    # "tel:+1-555-1234", "data:image/png;base64,..." and a real mailto
    # address stay protected.
    # A LEFT BOUNDARY, and it is the difference between linear and quadratic.
    # Without it the engine restarts the scheme scan at every letter of a long
    # colon-free run, scanning the whole remaining suffix each time: measured
    # 4.0x per doubling, 38.8s for finditer alone at the module's own 128 KiB
    # limit, where ordinary prose of the same length takes 0.48s end to end.
    # That is past the router's 30s timeout, and cancelling the request does
    # not stop work already handed to a thread. A scheme cannot begin midway
    # through a run of scheme characters anyway, so refusing to start there
    # costs no match: "ahttps://x" still matches, from the "a".
    r"(?<![A-Za-z0-9+.\-])(?i:[a-z][a-z0-9+.\-]*):"
    r"(?=" + _URL_TAIL + "{2,})(?=" + _URL_TAIL + "*[0-9A-Za-z])" + _URL_TAIL + "+|"
    # A bare address too, which is how one actually appears in a reply. The
    # local part is the identifying half, so matching only from the domain
    # was worse than not matching at all.
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|"
    # The same shape with an UNBOUNDED alphabet, because the rule above is
    # ASCII-only and an internationalized address matched nothing at all --
    # so "用户秘密@例子.公司" had its local part, the identifying half,
    # mined and persisted. The mailto form was fixed first; a bare address
    # is how one actually appears in a reply, and it needed the same
    # treatment.
    #
    # What makes the open alphabet safe is the local@domain.tld SHAPE, and
    # the atom class doing the work here: it stops at whitespace and at CJK
    # punctuation, so a match cannot run past the sentence it sits in. In
    # unspaced CJK it does take the surrounding run with it, which is
    # over-protection of one sentence -- the accepted direction, against a
    # payload persisted for 120 days.
    r"(?<!" + _URL_ATOM + r")" + _EMAIL_ADDRESS + r"|"
    # An ASCII-only lookbehind. ``\w`` counts CJK as a word character, so a bare
    # host written straight after a hanzi never matched at all and its path
    # token was persisted verbatim -- and zh/zh-TW/ja/ko, half the languages
    # this module supports, are written without spaces.
    r"(?<![A-Za-z0-9_-])(?:(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    # A TLD may be all-lower or all-UPPER but never Capitalised. DNS is
    # case-insensitive, so every spelling of a TLD is a real host and has to
    # be protected. The cost is that a missing space after a period reads as
    # one too -- "cute.Nice", "hola.Mi", "fine.Thanks", ordinary en/es/pt
    # model output -- which merely stops those sentences being mined. That is
    # the accepted direction for this module: over-protection loses a
    # catchphrase, under-protection persists a URL for 120 days. An earlier
    # revision tried to separate the two by case SHAPE (all-lower or
    # all-UPPER is a TLD, Capitalised is a resumed sentence); it left
    # "Example.CoM/secret" unprotected, which is the wrong way to be wrong.
    # Punycode FIRST: the generic branch matches the bare "xn" of "xn--p1ai"
    # and the pattern then ends there, because everything after the TLD is
    # optional -- so the rest of the label and the whole path stayed minable.
    r"(?i:xn--[a-z0-9-]{2,59}|[a-z]{2,63})|(?:\d{1,3}\.){3}\d{1,3})"
    # A query or fragment may follow the host with no path at all
    # ("example.com?token=..."), and stopping at the host left the query
    # minable.
    r"(?::\d{1,5})?(?:[/?#]" + _URL_TAIL + "*)?|"
    r"(?<![A-Za-z0-9_-])(?i:localhost)"
    r"(?:(?::\d{1,5})(?:[/?#]" + _URL_TAIL + "*)?|[/?#]" + _URL_TAIL + "*)"
)

_TEMPLATE_RE = re.compile(
    # The bodies are ATOMIC. Each is a tempered token that stops only at a
    # newline or at its own closer, so giving a character back can never
    # let the closer match -- the character handed back is by construction
    # neither. Without that, an opener with no closer backtracked through
    # the whole line one character at a time, and a reply full of unmatched
    # openers paid it once per opener: 2.6s at 3000 characters of "{{ ",
    # on the live turn path, since the effects sidecar masks every draft.
    # Still quadratic in the opener count -- the forward scan per opener
    # remains -- but 2.5x cheaper, and the bounded-lookahead form that
    # would make it linear cannot be used: it stops finding a closer past
    # its bound, which unmasks a long template body rather than merely
    # costing time.
    # Delimited containers may wrap: multiline Jinja/Handlebars, shell and JS
    # interpolation and ERB scriptlets are ordinary shapes, and a body that
    # merely spanned a newline was left unprotected while its single-line twin
    # was masked. The line budget is the point -- an unbounded newline-crossing
    # match turns one stray delimiter in prose into a span that swallows the
    # rest of the reply.
    # The body forbids only the CLOSER, not every brace: a template body may
    # legitimately hold one -- {% set config = {"token": "..."} %} -- and a class
    # of [^{}] made all three containers miss it and mine the payload. The
    # tempered form keeps the same bound as before, since the closer is still
    # required and the line budget is unchanged; kaomoji like {^_^} cannot match
    # because the two-character opener is still required.
    r"\{\{(?>(?:(?!\}\})[^\r\n])*)(?:\r?\n(?>(?:(?!\}\})[^\r\n])*)){0,3}\}\}|"
    r"\{%(?>(?:(?!%\})[^\r\n])*)(?:\r?\n(?>(?:(?!%\})[^\r\n])*)){0,3}%\}|"
    r"\{\#(?>(?:(?!\#\})[^\r\n])*)(?:\r?\n(?>(?:(?!\#\})[^\r\n])*)){0,3}\#\}|"
    # Jinja statement and comment blocks, on the same line budget: a
    # ``{% set api_key = "..." %}`` carried its payload straight past the brace
    # pattern, which only knew the expression form.
    # TEMPERED, like the three brace containers above, and for the reason
    # already written there: a body may legitimately hold the character
    # its class excluded. "${ {"k": "SECRET"} }" stopped at the inner "{"
    # and "<% rate = 50% ... %>" at the inner "%", so in both cases the
    # alternative failed entirely and nothing else picked the payload up.
    # The sibling "{% ... %}" holding the same inner brace was masked
    # fine, which is what showed the class rather than the shape was
    # deciding.
    r"\$\{(?>(?:(?!\})[^\r\n])*)(?:\r?\n(?>(?:(?!\})[^\r\n])*)){0,3}\}|"
    r"<%(?>(?:(?!%>)[^\r\n])*)(?:\r?\n(?>(?:(?!%>)[^\r\n])*)){0,3}%>|"
    # `<...>` must LOOK LIKE A TAG, and stays strictly line-bounded. It carries
    # by far the highest false-positive density in this project's character
    # speech -- `>_<`, `<3`, `->`, `3 < 5`. Line-bounding stopped the tail of
    # one emoticon pairing with the head of another on the NEXT line, but two
    # emoticons on ONE line is the commoner way a model writes them, and
    # `<3 you are so cute >_<` still lost the phrase between them. Requiring a
    # leading letter (or `/`) costs nothing: real HTML code containers are
    # `_html_raw_text_spans`' job, and this alternative only ever existed for
    # placeholder-shaped text.
    # The attribute run is LINE-BOUNDED, not capped at 80 characters. A
    # real tag carrying long class/style/data-* attributes busted that cap,
    # so the whole tag failed to match and its attributes -- including
    # data-key="..." payloads -- were mined and persisted. The cap never
    # bought what it looked like it bought either: "a <b and c> d" matched
    # under it just as well, because the leading-letter requirement, not
    # the length, is what keeps this off "<3", ">_<" and "->". So the cost
    # of removing it is one line of speech between a tag-shaped opener and
    # a later ">" -- over-protection, which this module accepts, against a
    # leak, which it does not.
    #
    # Atomic for the same reason as the containers above: the run stops
    # only at "<", ">" or a newline, so a character handed back can never
    # let the ">" match, and an opener with no closer now fails in one
    # pass instead of one per character.
    # The bracketed placeholder loses its length cap for exactly the reason
    # the tag run above lost its own: a 65-character name failed the whole
    # alternative, so nothing was protected and the identifier was mined,
    # while its 64-character twin in the same sentence was masked.
    r"</?[A-Za-z](?>[^<>\r\n]*)>|\[[A-Z](?>[A-Z0-9_-]+)\]"
)


class CandidateMinerError(ValueError):
    """A safe, content-free error suitable for CLI output."""


class CandidateBudgetExceededError(CandidateMinerError):
    """The input busts a local analysis budget; retrying with fewer messages helps.

    Distinct from the other miner errors precisely so the user-facing report can
    narrow its window and try again instead of failing the whole request. The CLI
    still surfaces it as an ordinary ``CandidateMinerError``.
    """


@dataclass(frozen=True)
class MiningConfig:
    """Deterministic mining parameters recorded in the output artifact."""

    threshold: int = DEFAULT_THRESHOLD
    word_ngram_min: int = DEFAULT_WORD_NGRAM_MIN
    word_ngram_max: int = DEFAULT_WORD_NGRAM_MAX
    cjk_ngram_min: int = DEFAULT_CJK_NGRAM_MIN
    cjk_ngram_max: int = DEFAULT_CJK_NGRAM_MAX
    min_length: int = DEFAULT_MIN_LENGTH
    exclude_covered: bool = False

    def validate(self) -> None:
        for name in (
            "threshold",
            "word_ngram_min",
            "word_ngram_max",
            "cjk_ngram_min",
            "cjk_ngram_max",
            "min_length",
        ):
            if getattr(self, name) < 1:
                raise CandidateMinerError(f"{name} must be at least 1")
        if self.word_ngram_min > self.word_ngram_max:
            raise CandidateMinerError("word_ngram_min cannot exceed word_ngram_max")
        if self.cjk_ngram_min > self.cjk_ngram_max:
            raise CandidateMinerError("cjk_ngram_min cannot exceed cjk_ngram_max")


@dataclass(frozen=True)
class SourceMessage:
    """The only source data retained during mining."""

    language: str
    content: str
    source_line: int


@dataclass(frozen=True)
class _CandidateOccurrence:
    normalized: str
    phrase: str
    coverage_text: str
    start: int
    end: int


@dataclass
class _CandidateStats:
    occurrence_count: int
    source_lines: set[int]
    phrases: set[str]
    occurrences: list[_CandidateOccurrence]


def normalize_language(raw: str) -> str:
    """Normalize an explicit locale tag without guessing from message text."""
    if not isinstance(raw, str) or not raw.strip():
        raise CandidateMinerError("language must be a non-empty string")
    normalized = raw.strip().replace("_", "-").casefold()
    try:
        return _LANGUAGE_ALIASES[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(set(_LANGUAGE_ALIASES.values())))
        raise CandidateMinerError(
            f"unsupported language tag; supported languages: {supported}"
        ) from exc


def read_jsonl(
    input_path: Path,
    *,
    language_override: str | None = None,
) -> tuple[list[SourceMessage], int]:
    """Read the documented JSONL contract and retain assistant text only."""
    if not input_path.is_file():
        raise CandidateMinerError(f"input file does not exist: {input_path}")
    override = normalize_language(language_override) if language_override else None
    messages: list[SourceMessage] = []
    record_count = 0

    try:
        handle = input_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CandidateMinerError(f"unable to open input file: {input_path}") from exc

    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise CandidateMinerError(f"line {line_number}: invalid JSON") from exc
            if not isinstance(record, dict):
                raise CandidateMinerError(
                    f"line {line_number}: each JSONL record must be an object"
                )
            record_count += 1

            role = record.get("role")
            content = record.get("content")
            if not isinstance(role, str) or not role:
                raise CandidateMinerError(
                    f"line {line_number}: role must be a non-empty string"
                )
            if not isinstance(content, str):
                raise CandidateMinerError(
                    f"line {line_number}: content must be a string"
                )
            conversation_id = record.get("conversation_id")
            if conversation_id is not None and not isinstance(conversation_id, str):
                raise CandidateMinerError(
                    f"line {line_number}: conversation_id must be a string when present"
                )
            if role != "assistant":
                continue

            raw_language = override or record.get("lang")
            if raw_language is None:
                raise CandidateMinerError(
                    f"line {line_number}: assistant records require lang or --language"
                )
            language = override or normalize_language(raw_language)
            messages.append(
                SourceMessage(
                    language=language,
                    content=content,
                    source_line=line_number,
                )
            )

    return messages, record_count


# Markdown blockquote containers: ``>`` optionally followed by one space, and
# nestable. Stripped before fence detection so a fence quoted inside a reply
# still opens and closes. Without this the ``>`` prefix defeats the fence
# match, only the delimiter lines end up protected by the inline-code pass,
# and the code body between them leaks into candidates and the export.
# A TAB may pad the marker, and the ambiguity it creates is settled at the
# END of the scan rather than here. "\t> ```" is four columns of indent, so
# CommonMark calls it indented code -- but if a matching delimiter follows,
# reading the pair as a quoted fence protects the body between them, which
# refusing the marker outright did not: the body sits at a shallower indent,
# so it was neither fence content nor indented code and an API key in it was
# mined and persisted. Refusing also put a fence PAIR out of step whenever
# only one side carried the tab, and the surviving delimiter then opened a
# fence nothing could close.
#
# So the marker is honoured, and ``_fenced_code_spans`` instead DISCARDS an
# unclosed fence whose opener was tab-indented. Whether a closer exists is
# exactly the information that disambiguates the two readings, and it is not
# available until the scan is over.
_BLOCKQUOTE_PREFIX_RE = re.compile(r"(?:[ \t]{0,3}>[ \t]?)+")
# A list item is also a container: a fence written directly after its marker
# ("- ```") never matched, so the block stayed unprotected whenever the inline
# scanner did not happen to cover it — measured leaking for an unclosed list
# fence, and for one whose closer sat in another paragraph. Stripped for fence
# detection only; the closing rule still keys on BLOCKQUOTE depth, since that is
# the container that decides whether a closer belongs to this fence.
# A tab may pad this marker too, settled the same way as the blockquote
# pattern above.
# ONE space after the marker, not the whole run -- the rule its twin
# ``_LIST_MARKER_COLUMN_RE`` already states: the greedy form is right when a
# fence opener follows and wrong when INDENTATION follows. A line that is
# BOTH is the case that comment did not cover. Eating all five spaces of
# "-     ```" measured the residual indent as zero, so it opened a fence
# that never closes and silenced the rest of the reply -- where CommonMark
# reads it as one line of indented code inside the item, which is what
# ``_indented_code_spans`` says as well. With one space stripped the column
# guard in ``_fenced_code_spans`` sees the remaining four and declines,
# while a genuine "- ```" still opens.
_LIST_MARKER_PREFIX_RE = re.compile(r"[ \t]{0,3}(?:[-+*]|\d{1,9}[.)])[ \t]")
# The same markers, minus the leading padding and consuming only ONE space
# after the marker. The greedy form above is right when a fence opener
# follows and wrong when INDENTATION follows: "-     code" is a marker, its
# single padding space, and then a four-column indented code block -- eating
# all five spaces measured it as zero columns and mined the code as prose.
# The padding is stripped separately, and by COLUMN, because a tab is worth
# four of them; see ``_strip_containers_by_column``.
_LIST_MARKER_COLUMN_RE = re.compile(r"(?:[-+*]|\d{1,9}[.)])[ \t]")
_BLOCKQUOTE_COLUMN_RE = re.compile(r">[ \t]?")
# The tick characters a fence may be built from, as opposed to the tilde
# forms: only these carry the CommonMark info-string restriction.
_FENCE_TICKS = "`｀"


def _strip_blockquote_prefix(line: str) -> str:
    return _split_blockquote_prefix(line)[0]


def _split_blockquote_prefix(line: str) -> tuple[str, int]:
    """Return the line without its blockquote markers, plus the depth stripped."""
    match = _BLOCKQUOTE_PREFIX_RE.match(line)
    if not match:
        return line, 0
    return line[match.end() :], match.group(0).count(">")


def _indent_columns(body: str) -> int:
    """Leading indentation in COLUMNS, expanding a tab to the next multiple of 4."""
    columns = 0
    for character in body:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - (columns % 4)
        else:
            break
    return columns


def _strip_containers_by_column(body: str) -> tuple[str, bool, int]:
    r"""Strip container markers whose own padding is worth at most three columns.

    The two marker patterns above both open with ``[ \t]{0,3}``, and a TAB
    matched there is worth FOUR columns, not one. So a tab-indented code block
    whose first content character happened to be ``-`` or ``>`` had that
    character stripped as if it were a container marker, the residual indent
    measured zero, and the line was mined as prose -- and persisted. Only
    SPACES, at most three, can pad a marker.

    Also reports whether a LIST marker was consumed and how deep the
    blockquote prefix ran, which is what tells the caller a new block starts
    here -- a list marker or a fresh quote level interrupts the paragraph
    above it, while the same quote prefix repeated just continues one.
    """
    list_opened = False
    quote_depth = 0
    while True:
        lead = len(body) - len(body.lstrip(" "))
        if lead > 3:
            return body, list_opened, quote_depth
        rest = body[lead:]
        quote = _BLOCKQUOTE_COLUMN_RE.match(rest)
        if quote is not None:
            quote_depth += 1
            body = rest[quote.end() :]
            continue
        marker = _LIST_MARKER_COLUMN_RE.match(rest)
        if marker is None:
            return body, list_opened, quote_depth
        list_opened = True
        body = rest[marker.end() :]


def _fence_open(stripped: str) -> re.Match[str] | None:
    """Match a fence opener, rejecting a backtick fence with a backtick info string.

    CommonMark forbids a backtick anywhere in the info string of a backtick
    fence, so a line reading three backticks, a letter and a fourth backtick is
    a paragraph. Reading it as an opener paired it with the NEXT fence line,
    which put every later delimiter one out of step: the real code block went
    unprotected, and in the other direction the paragraph it actually is got
    protected through to the end of the text.
    """
    match = _FENCE_OPEN_RE.match(stripped)
    if match is None:
        return None
    marker = match.group(1)
    if marker[0] in _FENCE_TICKS and marker[0] in stripped[match.end() :]:
        return None
    return match


def _fenced_code_spans(text: str) -> list[tuple[int, int]]:
    """Return Markdown fenced-code spans, including an unclosed final fence.

    Blockquote depth participates. A closer must sit at exactly the depth the
    fence opened at: a DEEPER marker is code content, while a SHALLOWER one means
    the blockquote ended -- which implicitly ends the inner fence -- and opens a
    new outer fence, so the region stays contiguously protected.

    Deliberate simplification: a shallower NON-marker line does not end the
    fence, so such a block over-protects rather than under-protects. That is the
    right direction to fail for a guard whose job is keeping code out of the
    report, the export and the persisted signature.
    """
    spans: list[tuple[int, int]] = []
    fence_start: int | None = None
    fence_char = ""
    fence_len = 0
    fence_depth = 0
    # Whether the line that OPENED the current fence was itself indented far
    # enough to be code. Measured on the raw line, before any container
    # marker is stripped, because that is the reading being weighed against.
    fence_opener_is_indented_code = False
    offset = 0
    for line in text.splitlines(keepends=True):
        body, depth = _split_blockquote_prefix(line)
        if fence_start is None:
            # Only an OPENING fence may sit behind a container marker.
            # Stripping on every line let a content line that happens to read
            # "- ```" be rewritten into a bare run and close the active fence,
            # exposing the rest of the block.
            #
            # Containers alternate -- "- > ```", "> - ```", "- > > ```" -- so
            # one blockquote pass followed by one list pass finds the opener
            # only for the orders it happens to be written in. Loop instead,
            # accumulating depth, so the closer at the same nesting matches.
            #
            # The loop has to continue while EITHER container consumed
            # something. Breaking when no blockquote followed the list marker
            # stripped exactly one marker, so "- - ```" never opened and the
            # block leaked, while the single-marker form protected fine.
            while True:
                list_marker = _LIST_MARKER_PREFIX_RE.match(body)
                rest = body if list_marker is None else body[list_marker.end() :]
                inner_body, inner_depth = _split_blockquote_prefix(rest)
                if list_marker is None and inner_depth == 0:
                    break
                body = inner_body
                depth += inner_depth
        stripped = body.lstrip(" \t")
        # COLUMNS, not characters. ``_indented_code_spans`` expands a tab to
        # four and this counted it as one, so a tab-indented fence line passed
        # the opener test here while being code CONTENT there -- and an opener
        # with no closer protects to end of text, so the disagreement cost the
        # whole remainder of the reply.
        if _indent_columns(body) <= 3:
            if fence_start is None:
                opening = _fence_open(stripped)
                if opening:
                    marker = opening.group(1)
                    fence_start = offset
                    fence_char = marker[0]
                    fence_len = len(marker)
                    fence_depth = depth
                    fence_opener_is_indented_code = _indent_columns(line) >= 4
            else:
                closing = re.match(
                    rf"{re.escape(fence_char)}{{{fence_len},}}[ \t]*(?:\r?\n)?\Z",
                    stripped,
                )
                reopening = _fence_open(stripped)
                if closing and depth == fence_depth:
                    # Depth-matched closer: an ordinary close.
                    spans.append((fence_start, offset + len(line)))
                    fence_start = None
                    fence_opener_is_indented_code = False
                    fence_char = ""
                    fence_len = 0
                    fence_depth = 0
                elif reopening and depth < fence_depth:
                    # Leaving the blockquote implicitly ends the inner fence, and
                    # this shallower marker opens a NEW outer fence. Ending the
                    # old span here and starting a new one at the same offset
                    # keeps the region contiguously protected. Merely IGNORING
                    # the line left the inner fence open, so a later
                    # depth-matched marker closed it and exposed everything from
                    # that point on.
                    spans.append((fence_start, offset))
                    marker = reopening.group(1)
                    fence_start = offset
                    fence_char = marker[0]
                    fence_len = len(marker)
                    fence_depth = depth
                    fence_opener_is_indented_code = _indent_columns(line) >= 4
                # depth > fence_depth: the marker is code content; ignore it.
        offset += len(line)
    # An unclosed fence protects to end of text, which is the accepted cost of
    # a real one. A TAB-INDENTED opener with no closer is not a real one: it
    # is indented code that happens to be delimiter-shaped, and
    # ``_indented_code_spans`` already covers that line. Honouring it here
    # silenced the whole rest of the reply.
    if fence_start is not None and not fence_opener_is_indented_code:
        spans.append((fence_start, len(text)))
    return spans


# CRLF too: persisted replies are whatever the model emitted, and an LF-only
# pattern silently skips a CRLF blank line — the paragraph then runs to the end
# of the text and a later backtick gets mistaken for this run's closer, which
# swallows real prose and drops the candidates it should have produced.
# Fence and inline-code delimiters, including the FULLWIDTH forms a CJK IME
# produces (U+FF40 GRAVE ACCENT, U+FF5E TILDE). Markdown proper only knows the
# ASCII ones, so by spec this text is prose -- but the guard's job is keeping
# code out of the report, the export and the persisted signature, and a reply
# whose code fence was typed in Chinese input mode still contains code.
# Mixed delimiters cannot pair: the fence tracks the exact opening character
# and the inline scanner matches the exact opening run.
# INLINE code delimiters are the ASCII backtick ONLY. Markdown has no tilde
# inline code (``~~~`` is a FENCE delimiter), and BOTH fullwidth marks are
# ordinary punctuation in this project's character speech:
#   U+FF5E TILDE is the commonest elongation mark -- "そうですね～また明日ね～"
#     lost "また明日ね" and "好呀～我们一起去吧～" lost "我们一起去吧".
#   U+FF40 GRAVE ACCENT is a kaomoji face part -- （｀・ω・´）, (*･ω｀*),
#     ヾ(｀Д´)ノ -- so it turns up in MATCHED PAIRS across a sentence even
#     more reliably than the tilde did. Measured over 20k code-free replies
#     it fired on 49.8% of them; dropping it takes protected characters from
#     16.0% to 7.5% and whole-catchphrase loss from 19.8% to 9.0%. A ｀...｀
#     pair and a kaomoji pair are structurally identical, so there is no
#     tiebreaker worth keeping -- demanding code-ish content in the body was
#     measured at 0.24pp.
# Fences keep ｀｀｀ and ~~~: a run of three at the START OF A LINE is not
# something either mark produces mid-word. ～～～ does not survive that
# argument -- a line of fullwidth tildes is a decorative section divider in
# casual zh/ja chat, and since an unclosed fence protects to end of text,
# one such line deleted the whole rest of the reply.
_CODE_DELIMITERS = "`"
_FENCE_OPEN_RE = re.compile(r"(`{3,}|~{3,}|｀{3,})")


_BLANK_LINE_RE = re.compile(r"\r?\n[ \t]*\r?\n")


def _paragraph_end(text: str, start: int) -> int:
    """Offset of the blank line that ends the paragraph containing ``start``."""
    match = _BLANK_LINE_RE.search(text, start)
    return match.start() if match else len(text)


def _inline_code_spans(
    text: str,
    block_spans: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return backtick code spans outside block code.

    A code span may cross newlines but not a blank line — CommonMark keeps it
    inside one paragraph — so the closing delimiter is searched to the end of
    the paragraph. Stopping at the end of the LINE protected only the first line
    of a multi-line span and left the rest mineable, right through to a
    persisted signature.

    An unmatched run still falls back to protecting the rest of its own line
    rather than the paragraph: without a closer the backtick is literal text per
    CommonMark, so the following lines really are prose and swallowing them
    would drop real candidates.
    """
    spans: list[tuple[int, int]] = []
    index = 0
    block_index = 0
    while index < len(text):
        while block_index < len(block_spans) and block_spans[block_index][1] <= index:
            block_index += 1
        if (
            block_index < len(block_spans)
            and block_spans[block_index][0] <= index < block_spans[block_index][1]
        ):
            index = block_spans[block_index][1]
            continue
        delimiter_char = text[index]
        if delimiter_char not in _CODE_DELIMITERS:
            index += 1
            continue

        run_end = index + 1
        while run_end < len(text) and text[run_end] == delimiter_char:
            run_end += 1
        delimiter = text[index:run_end]
        newline = text.find("\n", run_end)
        line_end = len(text) if newline < 0 else newline
        # The closer must be a run of EXACTLY the opening length; a longer run
        # is content. `find` accepted the opening-length PREFIX of a longer
        # run, so a span ended mid-run and, once a second shorter run paired
        # up with the leftovers, the body after it was mined as prose. Harmless
        # while the search was bounded to one line -- the leftovers re-opened
        # and the coverage merged -- but this file now searches to the end of
        # the paragraph, which turned it into a real leak that also reaches the
        # persisted signature.
        limit = _paragraph_end(text, run_end)
        closing = -1
        cursor = run_end
        while cursor < limit:
            if text[cursor] != delimiter_char:
                cursor += 1
                continue
            candidate_end = cursor
            while candidate_end < limit and text[candidate_end] == delimiter_char:
                candidate_end += 1
            if candidate_end - cursor == len(delimiter):
                closing = cursor
                break
            cursor = candidate_end
        end = line_end if closing < 0 else closing + len(delimiter)
        spans.append((index, end))
        index = max(end, run_end)
    return spans


def _indented_code_spans(text: str) -> list[tuple[int, int]]:
    """Return Markdown code lines indented by at least four columns.

    An indented code block cannot INTERRUPT a paragraph, so the previous line
    decides. Without that rule every indented continuation line -- centred
    ASCII art, an aligned lyric, a clause wrapped for width -- was deleted from
    mining, on 6% of a code-free speech corpus.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    paragraph_open = False
    quote_depth = 0
    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        body, list_opened, line_quote_depth = _strip_containers_by_column(line)
        if list_opened or line_quote_depth != quote_depth:
            # A list marker or a new quote level opens a block, and nothing
            # can be a continuation of a paragraph that is not in it.
            paragraph_open = False
        quote_depth = line_quote_depth
        is_code = (
            _indent_columns(body) >= 4 and bool(body.strip()) and not paragraph_open
        )
        if is_code:
            spans.append((offset, line_end))
        # The CONTAINER-STRIPPED body decides, not the raw line: inside a
        # blockquote a bare ">" is a blank line, and reading it as prose kept
        # the paragraph open so the indented code after it was mined.
        paragraph_open = bool(body.strip()) and not is_code
        offset = line_end
    return spans


# ``<pre>`` / ``<code>`` mark code as explicitly as a Markdown fence does, and
# ``<script>`` / ``<style>`` are raw-text elements whose bodies are code by
# definition. The close tag is a backreference so containers cannot cross-match.
# The generic ``<...>`` template pattern only covers the TAGS, which protected
# the delimiters while leaving the code between them mineable — worse than not
# handling it, because it looks handled.
#
# An UNMATCHED opening container protects through the end of the text, exactly
# as ``_fenced_code_spans`` treats an unclosed final fence. A reply truncated
# mid-code-block otherwise leaks its body all the way into a persisted
# ``RepeatSignature`` — measured: ``build_repeat_signature`` returned the code
# identifier verbatim for an unclosed ``<code>`` and ``None`` for the closed
# form and for an unclosed fence. The pattern requires the literal ``<code`` /
# ``<pre`` tag plus a word boundary, not a bare ``<``, so ordinary prose
# containing comparisons or words like "decode" is unaffected.
_HTML_RAW_TEXT_TAGS = ("pre", "code", "script", "style", "textarea")
# HTML RAW-TEXT elements: their content is text by definition, so a
# start-tag-shaped string inside the body is a STRING, not a nested
# element, and the first matching close tag ends them. Depth-counting them
# ran the span past the real closer -- `<script>const m = "<script>";
# </script>` then protected to END OF TEXT, across a blank line, eating the
# reply after it. `pre` and `code` are ordinary elements that really do
# nest, so they keep the counter.
_HTML_NON_NESTING_TAGS = frozenset({"script", "style", "textarea"})
_HTML_RAW_TEXT_OPEN_RE = re.compile(
    r"<(pre|code|script|style|textarea)\b[^>]*>",
    re.IGNORECASE,
)


def _url_spans(text: str) -> list[tuple[int, int]]:
    """Return URL spans, extended through balanced parentheses to any depth.

    A path nests parentheses -- ``/f(g(x))`` -- and encoding ONE level in the
    pattern simply stopped at the inner ``(``, leaving the rest of the path
    minable and, because this feeds ``build_repeat_signature``, persistable.
    Same reason ``_markdown_link_target_spans`` is a scanner and not a pattern.

    An UNBALANCED ``(`` extends nothing. Running to the end of the paragraph is
    exactly how a URL in speech came to swallow the sentence after it, and a
    group whose body hits a stop character is not part of the URL either.
    """
    matches = list(_URL_RE.finditer(text))
    if not matches:
        return []
    tail_end = _url_tail_ends(text)
    return [(match.start(), tail_end[match.end()]) for match in matches]


def _url_tail_ends(text: str) -> list[int]:
    """For every offset, where a URL tail starting there ends.

    Two linear passes, because walking forward from each match was O(n^2): a
    failed opener scanned to the stop character and the next match started one
    token later and scanned the same tail again. Measured on ``"a.com(" * n``
    before this: 25 s at 48 KB, 99 s at 96 KB, against a 128 KB accepted reply.

    Pass one pairs parentheses on a stack; a stop character clears the stack,
    because a parenthetical holding whitespace or CJK punctuation is prose, not
    a path segment. Pass two resolves each opener to the end of its whole chain
    of groups and trailing atoms, right to left, so every answer it needs is
    already computed.
    """
    length = len(text)
    closer_of: dict[int, int] = {}
    stack: list[int] = []
    for index, character in enumerate(text):
        if character == "(":
            stack.append(index)
        elif character == ")":
            if stack:
                closer_of[stack.pop()] = index + 1
        elif not _URL_ATOM_RE.match(text, index):
            stack.clear()

    atom_run_end = [length] * (length + 1)
    for index in range(length - 1, -1, -1):
        atom_run_end[index] = (
            atom_run_end[index + 1] if _URL_ATOM_RE.match(text, index) else index
        )

    tail_end = list(range(length + 1))
    for index in range(length - 1, -1, -1):
        closed = closer_of.get(index)
        if closed is not None:
            tail_end[index] = tail_end[atom_run_end[closed]]
    return tail_end


def _paragraph_bounds(text: str, start: int) -> tuple[int, int]:
    """Return the end of the paragraph at ``start`` and the start of the next."""
    match = _BLANK_LINE_RE.search(text, start)
    if match is None:
        return len(text), len(text) + 1
    return match.start(), match.end()


def _markdown_link_target_spans(
    text: str, ignore: Sequence[tuple[int, int]] = ()
) -> list[tuple[int, int]]:
    """Return the TARGET half of Markdown links -- ``](`` through its close.

    Never the link text: that is prose a character may legitimately repeat.
    A bare path in prose stays minable too -- this scanner's job is link
    TARGETS, and a general path rule is a separate one nobody has asked for.
    Not for the reason once recorded here ("would eat dates and fractions"):
    an anchored rule sees neither, because in ``2026/08/27``, ``1/2`` and
    ``and/or`` the slash follows an alphanumeric and the anchor rejects it.
    Measured collateral is only the emote forms ``/shrug`` and ``/me``.

    A scanner rather than a pattern because targets nest parentheses to any
    depth (``/api/f(g(x))``), and a regex that allows one level simply fails
    to match deeper ones -- leaving the target mined as if there were no rule.
    An unbalanced ``](`` protects NOTHING: running to end of text would be the
    over-protection this module keeps having to undo.

    ONE pass per paragraph, matching every ``(`` to its closer on a stack.
    Rescanning the paragraph tail for each failed opener was quadratic: 16 KiB
    of repeated ``](`` took seconds, and a persisted reply may be 128 KiB --
    long enough for the analysis thread to outlive the router's own timeout
    and hold a shared worker while it does.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor <= len(text):
        limit, next_paragraph = _paragraph_bounds(text, cursor)
        _collect_link_targets(text, cursor, limit, ignore, spans)
        cursor = next_paragraph
    return spans


def _collect_link_targets(
    text: str,
    start: int,
    limit: int,
    ignore: Sequence[tuple[int, int]],
    spans: list[tuple[int, int]],
) -> None:
    """Append every balanced link target in one paragraph to ``spans``.

    TWO passes. Parenthesis matching does not depend on links at all, while
    label matching depends on where the targets are, so the parens go first
    and the label scan then jumps over any target that actually closes.

    Carrying an "inside a target" depth through a single pass instead let an
    UNCLOSED target poison the rest of the paragraph: the depth went up and
    never came down, so every later "[" stopped counting and a genuine link
    after it went unprotected. That is a LEAK, traded for the
    over-protection the depth was added to stop -- the wrong direction, and
    the reason this is structured as two passes rather than patched.
    """
    # Pass one: balanced parentheses. Nothing here knows about links.
    #
    # A DISPLAYED parenthesis opens and closes nothing, the same rule both
    # bracket arms below already follow. Without it a ")" shown inside a code
    # span closed the target early: "[docs](/api/`)`/keys/secret)" protected
    # only as far as the quoted ")" and mined the rest of the destination.
    open_parens: list[int] = []
    closer_of: dict[int, int] = {}
    cursor = start
    while cursor < limit:
        character = text[cursor]
        if character == chr(92):
            cursor += 2
            continue
        if character == "(" and not _starts_inside(cursor, ignore):
            open_parens.append(cursor)
        elif character == ")" and not _starts_inside(cursor, ignore):
            if open_parens:
                closer_of[open_parens.pop()] = cursor + 1
        cursor += 1

    # Pass two: labels.
    openers: list[int] = []
    # A COUNT, not a paragraph-wide flag. As a flag, the first "[" anywhere
    # in the paragraph made every later "](" a link closer, so
    # "[LAUGHS] okay](please remember to rest)" protected the whole
    # parenthetical -- the "]" after LAUGHS had already closed the only
    # label, and no reader renders that as a link. A repeated catchphrase
    # sitting there was silently never mined, on this path and on the
    # runtime one that shares it.
    open_labels = 0
    cursor = start
    while cursor < limit:
        character = text[cursor]
        if character == chr(92):
            cursor += 2
            continue
        if character == "[":
            # A DISPLAYED bracket opens nothing -- the same rule the opener
            # acceptance below already follows. Counting it let "`[LAUGHS`
            # okay](...)" hand a label to the stray closer after it, so the
            # parenthetical was protected and a repeated catchphrase in it
            # was never mined.
            if not _starts_inside(cursor, ignore):
                open_labels += 1
        elif (
            character == "]"
            and open_labels
            # BOTH sides skip displayed brackets, not just the opener. A "]"
            # shown inside a code span consuming a real label let the label
            # look already closed, so the genuine "](" after it opened
            # nothing and a real target went unprotected.
            and not _starts_inside(cursor, ignore)
        ):
            # This "]" closes a label either way; whether it also opens a
            # TARGET depends on the "(" right after it AND on that "(" being
            # closed. A link needs a LABEL, too: without the opening-bracket
            # requirement any "](" started a target scan, so
            # "好呀](我们一起去公园散步吧)" -- ordinary CJK punctuation, and no
            # <a> element by any reader -- lost the whole parenthetical.
            open_labels -= 1
            if cursor + 1 < limit and text[cursor + 1] == "(":
                end = closer_of.get(cursor + 1)
                if end is not None:
                    openers.append(cursor)
                    # The DESTINATION is not label text. "[docs](/api/[v1)"
                    # put destination punctuation where a label would go, so
                    # the stray "](" after it was accepted as a second link
                    # and ordinary speech was protected. Skipping the target
                    # whole is what stops that, and unlike a depth counter it
                    # cannot outlive a target that never closes.
                    cursor = end
                    continue
        cursor += 1
    for opener in openers:
        end = closer_of.get(opener + 1)
        # A DISPLAYED opener opens nothing, the same rule the two HTML scanners
        # already follow: "](", shown inside a code span, still ran its
        # close-paren search out of the code block and into the speech after it.
        if end is not None and not _starts_inside(opener, ignore):
            spans.append((opener, end))


# ``[label]: destination`` at line start, the reference form of a link. The
# label may carry anything; only the destination is protected, and only when it
# looks like one -- see ``_reference_definition_spans``.
_REFERENCE_DEFINITION_RE = re.compile(
    # The space after the colon is OPTIONAL -- "[cfg]:/api/token" is a valid
    # definition, and requiring one left its destination minable. The
    # destination-shape check in the scanner is what keeps this off ordinary
    # speech, not the space.
    # The <...> form FIRST, and it runs to the closing bracket. A
    # whitespace-delimited capture stopped at the first space, so
    # "[cfg]: <../secret helper phrase>" yielded "<../secret"; the
    # destination-shape check then rejected that fragment for having no
    # closing ">", and the whole destination stayed minable.
    #
    # A BACKSLASH-ESCAPED ">" does not close it, which is the same
    # truncation one level down: "[cfg]: <../a\> secret phrase>" cut the
    # capture at the escaped bracket and left the rest of the destination
    # minable. Escapes are consumed as a unit, so a trailing lone
    # backslash still cannot swallow the newline.
    r"^[ \t]{0,3}\[[^\]\r\n]*\]:[ \t]*"
    # THREE forms, and the middle one is a fallback rather than a rule: when
    # the escape-aware form finds no LATER unescaped ">" it fails outright,
    # the whitespace-delimited form then stops at the first space, and the
    # shape check rejects that fragment for having no ">" -- so
    # "[cfg]: <../my notes/SECRET\>" ended up with no span at all, where
    # before the escape handling it had a full one. Keeping the old
    # truncating form behind the new one makes this monotone: never less
    # protected than it was.
    r"(?P<target><(?:\\[^\r\n]|[^>\r\n\\])*>|<[^>\r\n]*>|[^ \t\r\n]+)",
    re.MULTILINE,
)


def _starts_inside(position: int, spans: Sequence[tuple[int, int]]) -> bool:
    """True when ``position`` sits inside one of ``spans``.

    ``spans`` must be sorted and non-overlapping -- every caller passes the
    output of ``_merge_spans``. Binary search rather than a scan because the
    container scanners call this once per candidate opener AND closer, so a
    linear probe is quadratic in exactly the input that motivates it: a long
    reply with many delimiters and few real containers.
    """
    index = bisect.bisect_right(spans, position, key=lambda span: span[0]) - 1
    return index >= 0 and spans[index][0] <= position < spans[index][1]


def _reference_definition_spans(
    text: str, ignore: Sequence[tuple[int, int]] = ()
) -> list[tuple[int, int]]:
    """Return the DESTINATION half of a Markdown reference definition.

    ``[label]: /srv/keys/token`` puts a path where the inline form puts it
    behind ``](``, so the inline scanner never saw it and the destination was
    mined -- and persisted. Same rule as that scanner: the label half is prose
    a character may repeat, only the destination is protected.

    The destination has to LOOK like one, or an ordinary line of speech that
    happens to start with a bracketed name -- a script beat, a footnote-ish
    aside -- would be protected to end of line. It qualifies when it is a
    closed angle-bracket pair, or starts with ``/``, ``./`` or ``../``, or the
    module's own ``_URL_RE`` matches it from offset zero. No new list: the
    URL rule is reused rather than restated.
    """
    spans: list[tuple[int, int]] = []
    for match in _REFERENCE_DEFINITION_RE.finditer(text):
        start = match.start("target")
        if _starts_inside(match.start(), ignore):
            continue
        target = match.group("target")
        if target.startswith("<"):
            if not target.endswith(">") or len(target) < 2:
                continue
        elif not (
            target.startswith(("/", "./", "../"))
            or _URL_RE.match(target)
        ):
            continue
        spans.append((start, start + len(target)))
    return spans


def _html_comment_spans(
    text: str, ignore: Sequence[tuple[int, int]] = ()
) -> list[tuple[int, int]]:
    """Return ``<!-- ... -->`` spans, including ones that wrap.

    A single-line comment was masked only incidentally, by the generic
    ``<...>`` template alternative; the moment it wrapped, that alternative
    stopped matching and the body was mined -- and it reached the persisted
    signature too. No line budget is needed here, unlike the brace and ERB
    containers: ``<!--`` and ``-->`` are four and three characters, so a
    stray opener in speech is not a realistic accident. An unterminated
    comment runs to end of text, matching the other containers.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    while True:
        start = text.find("<!--", position)
        if start < 0:
            return spans
        if _starts_inside(start, ignore):
            # Being DISPLAYED as code, not opening anything. Honouring it ran
            # the container past the code block and ate the prose after it --
            # over-protection, which for this module is the worse direction:
            # it deletes the catchphrases the feature exists to surface.
            position = start + 4
            continue
        closing = text.find("-->", start + 4)
        while closing >= 0 and _starts_inside(closing, ignore):
            # A displayed CLOSER closes nothing either. Only the opener was
            # checked, so "<!-- a `-->` SECRET -->" ended the comment at the
            # quoted arrow and left the real body minable -- and persisted.
            closing = text.find("-->", closing + 3)
        end = len(text) if closing < 0 else closing + 3
        spans.append((start, end))
        position = end


def _html_raw_text_spans(
    text: str, ignore: Sequence[tuple[int, int]] = ()
) -> list[tuple[int, int]]:
    """Return raw-text element spans, counting NESTED same-tag opens.

    A non-greedy regex stopped at the FIRST closing tag, so
    ``<code>a <code>b</code> SECRET</code>`` ended its span early and leaked the
    rest of the outer element. Depth counting is why this is a scanner rather
    than one pattern.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    while True:
        opening = _HTML_RAW_TEXT_OPEN_RE.search(text, position)
        while opening is not None and _starts_inside(opening.start(), ignore):
            # Displayed as code, or sitting inside an HTML COMMENT -- either
            # way it opens nothing. A tag in a comment body was read as an
            # unterminated container and protected to end of text.
            opening = _HTML_RAW_TEXT_OPEN_RE.search(text, opening.end())
        if opening is None:
            return spans
        tag = opening.group(1).lower()
        open_re = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
        close_re = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
        depth = 1
        cursor = opening.end()
        end = len(text)
        while depth:
            next_close = close_re.search(text, cursor)
            while next_close is not None and _starts_inside(
                next_close.start(), ignore
            ):
                # Displayed closers close nothing; see _html_comment_spans.
                next_close = close_re.search(text, next_close.end())
            if next_close is None:
                # Unterminated: protect through the end of the text, the same
                # way an unclosed fence behaves.
                end = len(text)
                break
            next_open = (
                None
                if tag in _HTML_NON_NESTING_TAGS
                else open_re.search(text, cursor)
            )
            while next_open is not None and _starts_inside(
                next_open.start(), ignore
            ):
                # ...and the dual: a displayed nested opener must not deepen
                # the count, or the element runs past its real closer.
                next_open = open_re.search(text, next_open.end())
            if next_open is not None and next_open.start() < next_close.start():
                depth += 1
                cursor = next_open.end()
                continue
            depth -= 1
            cursor = next_close.end()
            end = cursor
        spans.append((opening.start(), end))
        position = max(end, opening.end())


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Return merged spans for code, URLs, and obvious template placeholders."""
    runtime = _runtime_protected_spans(text)
    spans = list(runtime)
    # A template delimiter DISPLAYED as code opens nothing. This pattern is
    # independent of the scanners above, so an opener sitting in one code span
    # paired with a closer in another and erased the prose between them.
    # Resume just after a DISCARDED opener, not after the whole match.
    # ``finditer`` skips to the end of every match it yields, including the
    # ones dropped here, so everything such a match covered was never
    # offered to the other alternatives. That window used to be at most 80
    # characters; once the tag run became line-bounded instead it grew to a
    # whole line, and a "${...}" payload sitting after a tag-shaped opener
    # inside a code span stopped being matched at all.
    position = 0
    while True:
        match = _TEMPLATE_RE.search(text, position)
        if match is None:
            break
        if _starts_inside(match.start(), runtime):
            # Resume past the whole SPAN that discarded it, not one
            # character on. Every match starting inside that span would be
            # discarded too, and retrying each offset in a long code block
            # is what turns this loop quadratic.
            index = (
                bisect.bisect_right(runtime, match.start(), key=lambda s: s[0])
                - 1
            )
            position = max(match.start() + 1, runtime[index][1])
            continue
        spans.append(match.span())
        position = match.end()
    return _merge_spans(spans)


def _runtime_protected_spans(text: str) -> list[tuple[int, int]]:
    """Return fenced, indented, inline-code, URL and HTML-container spans.

    Shared with ``build_repeat_signature``, so anything missing here can be
    persisted to the effects sidecar, not merely mined into a report.
    """
    fenced = _fenced_code_spans(text)
    block_code = _merge_spans(fenced + _indented_code_spans(text))
    spans = block_code + _inline_code_spans(text, block_code)
    # An HTML opener that is itself displayed as code opens nothing, and the
    # two scanners below run to a closer that may be far away -- so honouring
    # such an opener ran the container past the code block and ate the prose
    # after it. The bounded regexes need no such guard.
    code_spans = _merge_spans(spans)
    spans.extend(_url_spans(text))
    spans.extend(_markdown_link_target_spans(text, code_spans))
    spans.extend(_reference_definition_spans(text, code_spans))
    # Comments first: a tag inside a comment BODY is not a container either,
    # so the raw-text scanner has to see the comment spans as well.
    comment_spans = _html_comment_spans(text, code_spans)
    spans.extend(comment_spans)
    spans.extend(
        _html_raw_text_spans(text, _merge_spans(code_spans + comment_spans))
    )
    return _merge_spans(spans)


def _merge_spans(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def _unprotected_segments(text: str) -> Iterator[tuple[str, int]]:
    """Yield text and offsets outside protected spans without bridging them."""
    cursor = 0
    for start, end in _protected_spans(text):
        if cursor < start:
            yield text[cursor:start], cursor
        cursor = end
    if cursor < len(text):
        yield text[cursor:], cursor


def _text_segments(text: str, base_offset: int) -> Iterator[tuple[str, int]]:
    """Yield punctuation-bounded mining segments with original-text offsets."""
    cursor = 0
    for match in _TEXT_BOUNDARY_RE.finditer(text):
        if cursor < match.start():
            yield text[cursor : match.start()], base_offset + cursor
        cursor = match.end()
    if cursor < len(text):
        yield text[cursor:], base_offset + cursor


_T = TypeVar("_T")


def _bounded_ngrams(
    values: Sequence[_T],
    minimum: int,
    maximum: int,
) -> Iterator[tuple[_T, ...]]:
    upper = min(maximum, len(values))
    for size in range(minimum, upper + 1):
        for start in range(0, len(values) - size + 1):
            yield tuple(values[start : start + size])


def _is_meaningful(value: str, min_length: int) -> bool:
    compact = "".join(value.split())
    return len(compact) >= min_length and any(char.isalpha() for char in compact)


def _word_tokens(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield NFKC-normalized word tokens with spans in the original text."""
    index = 0
    while index < len(text):
        if not text[index].isalnum() or text[index] == "_":
            index += 1
            continue
        start = index
        index += 1
        while index < len(text):
            char = text[index]
            if char.isalnum() and char != "_":
                index += 1
                continue
            if unicodedata.category(char).startswith("M"):
                index += 1
                continue
            if (
                char in {"'", "\u2019"}
                and index + 1 < len(text)
                and text[index + 1].isalnum()
                and text[index + 1] != "_"
            ):
                index += 1
                continue
            break
        yield unicodedata.normalize("NFKC", text[start:index]), start, index


def _word_candidates(
    text: str,
    config: MiningConfig,
) -> Iterator[_CandidateOccurrence]:
    for unprotected, unprotected_start in _unprotected_segments(text):
        for segment, segment_start in _text_segments(unprotected, unprotected_start):
            token_run: list[tuple[str, int, int]] = []
            for token, start, end in _word_tokens(segment):
                if not any(char.isalpha() for char in token):
                    yield from _word_run_candidates(
                        token_run,
                        config,
                        text,
                    )
                    token_run = []
                    continue
                token_run.append((token, segment_start + start, segment_start + end))
            yield from _word_run_candidates(token_run, config, text)


def _word_run_candidates(
    token_run: Sequence[tuple[str, int, int]],
    config: MiningConfig,
    coverage_text: str,
) -> Iterator[_CandidateOccurrence]:
    for gram in _bounded_ngrams(
        token_run,
        config.word_ngram_min,
        config.word_ngram_max,
    ):
        phrase = " ".join(token for token, _, _ in gram)
        normalized = " ".join(token.casefold() for token, _, _ in gram)
        if _is_meaningful(normalized, config.min_length):
            yield _CandidateOccurrence(
                normalized=normalized,
                phrase=phrase,
                coverage_text=coverage_text,
                start=gram[0][1],
                end=gram[-1][2],
            )


def _is_han(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
    )


def _is_japanese(char: str) -> bool:
    codepoint = ord(char)
    return (
        _is_han(char)
        or codepoint == 0x3005
        or 0x3031 <= codepoint <= 0x3035
        or codepoint == 0x303B
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0xFF66 <= codepoint <= 0xFF9D
    )


def _is_hangul(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xD7B0 <= codepoint <= 0xD7FF
    )


def _is_hangul_jamo(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xD7B0 <= codepoint <= 0xD7FF
    )


def _normalized_characters(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield normalized characters mapped to their original source spans."""
    index = 0
    while index < len(text):
        start = index
        index += 1
        if _is_hangul_jamo(text[start]):
            while index < len(text) and _is_hangul_jamo(text[index]):
                index += 1
        while index < len(text) and (
            unicodedata.category(text[index]).startswith("M")
            or text[index] in {"\uff9e", "\uff9f"}
        ):
            index += 1
        normalized = unicodedata.normalize("NFKC", text[start:index])
        for char in normalized:
            yield char, start, index


def _script_runs(
    text: str,
    predicate,
) -> Iterator[list[tuple[str, int, int]]]:
    run: list[tuple[str, int, int]] = []
    for char, start, end in _normalized_characters(text):
        if predicate(char):
            run.append((char, start, end))
        elif run:
            yield run
            run = []
    if run:
        yield run


def _character_candidates(
    text: str,
    config: MiningConfig,
    predicate,
) -> Iterator[_CandidateOccurrence]:
    for unprotected, unprotected_start in _unprotected_segments(text):
        for segment, segment_start in _text_segments(unprotected, unprotected_start):
            for run in _script_runs(segment, predicate):
                upper = min(config.cjk_ngram_max, len(run))
                for size in range(config.cjk_ngram_min, upper + 1):
                    for start in range(0, len(run) - size + 1):
                        gram = run[start : start + size]
                        phrase = "".join(char for char, _, _ in gram)
                        normalized = phrase.casefold()
                        if _is_meaningful(normalized, config.min_length):
                            yield _CandidateOccurrence(
                                normalized=normalized,
                                phrase=phrase,
                                coverage_text=text,
                                start=segment_start + gram[0][1],
                                end=segment_start + gram[-1][2],
                            )


def _message_candidates(
    message: SourceMessage,
    config: MiningConfig,
) -> Iterator[_CandidateOccurrence]:
    language = message.language
    if language in _WHITESPACE_LANGUAGES:
        yield from _word_candidates(message.content, config)
        return
    if language.startswith("zh"):
        yield from _character_candidates(message.content, config, _is_han)
        return
    if language == "ja":
        yield from _character_candidates(message.content, config, _is_japanese)
        return
    if language == "ko":
        # Korean prose is normally space-delimited, but repeated compounds and
        # onomatopoeia often are not. Keep both families, but do not count an
        # identical single-token occurrence once in each strategy.
        overlapping_word_candidates = set()
        for candidate in _word_candidates(message.content, config):
            overlapping_word_candidates.add(
                (
                    candidate.normalized,
                    candidate.coverage_text,
                    candidate.start,
                    candidate.end,
                )
            )
            yield candidate
        for candidate in _character_candidates(message.content, config, _is_hangul):
            overlap_key = (
                candidate.normalized,
                candidate.coverage_text,
                candidate.start,
                candidate.end,
            )
            if overlap_key in overlapping_word_candidates:
                continue
            yield candidate
        return
    raise CandidateMinerError(f"unsupported normalized language: {language}")


def _coverage_language(language: str) -> str:
    return "zh" if language in {"zh", "zh-CN"} else language


def _coverage_result(
    language: str,
    occurrences: Sequence[_CandidateOccurrence],
    rules_by_language: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    compiled_rules_cache: dict[str, tuple[tuple[str, re.Pattern[str]], ...]],
    protected_cache: dict[str, list[tuple[int, int]]],
    match_cache: dict[tuple[str, str, int], tuple[tuple[int, int], ...]],
) -> tuple[list[str], bool]:
    covered: set[str] = set()
    coverage_language = _coverage_language(language)
    compiled_rules = compiled_rules_cache.get(coverage_language)
    if compiled_rules is None:
        pending_rules: list[tuple[str, re.Pattern[str]]] = []
        for rule in rules_by_language.get(coverage_language, ()):
            rule_id = rule.get("id")
            pattern = rule.get("find")
            flags = rule.get("flags", 0)
            if not isinstance(rule_id, str) or not isinstance(pattern, str):
                continue
            try:
                compiled = re.compile(pattern, int(flags))
            except (re.error, TypeError, ValueError) as exc:
                raise CandidateMinerError(
                    f"existing rule {rule_id} has an invalid pattern"
                ) from exc
            pending_rules.append((rule_id, compiled))
        compiled_rules = tuple(pending_rules)
        compiled_rules_cache[coverage_language] = compiled_rules

    all_occurrences_covered = bool(occurrences)
    for occurrence in occurrences:
        occurrence_covered = False
        protected = protected_cache.get(occurrence.coverage_text)
        if protected is None:
            protected = _runtime_protected_spans(occurrence.coverage_text)
            protected_cache[occurrence.coverage_text] = protected
        for rule_index, (rule_id, compiled) in enumerate(compiled_rules):
            cache_key = (coverage_language, occurrence.coverage_text, rule_index)
            match_spans = match_cache.get(cache_key)
            if match_spans is None:
                match_spans = tuple(
                    (match.start(), match.end())
                    for match in compiled.finditer(occurrence.coverage_text)
                    if match.start() != match.end()
                    and not any(
                        match.start() < protected_end and match.end() > protected_start
                        for protected_start, protected_end in protected
                    )
                )
                match_cache[cache_key] = match_spans
            if any(
                start <= occurrence.start and occurrence.end <= end
                for start, end in match_spans
            ):
                covered.add(rule_id)
                occurrence_covered = True
        if not occurrence_covered:
            all_occurrences_covered = False
    return sorted(covered), all_occurrences_covered


def load_current_rules() -> Mapping[str, Sequence[Mapping[str, object]]]:
    """Load the curated runtime table for read-only coverage analysis."""
    try:
        from config.prompts.prompts_slop import SLOP_RULES
    except Exception as exc:
        raise CandidateMinerError("unable to load current SLOP_RULES") from exc
    return SLOP_RULES


def build_report(
    messages: Sequence[SourceMessage],
    *,
    input_record_count: int,
    config: MiningConfig,
    rules_by_language: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    message_count_threshold: int = 1,
    max_occurrences: int | None = None,
) -> dict[str, object]:
    """Build a deterministic, review-only candidate report."""
    config.validate()
    if message_count_threshold < 1:
        raise CandidateMinerError("message_count_threshold must be at least 1")
    if max_occurrences is not None and max_occurrences < 1:
        raise CandidateMinerError("max_occurrences must be at least 1")
    current_rules = (
        load_current_rules() if rules_by_language is None else rules_by_language
    )
    stats: dict[tuple[str, str], _CandidateStats] = {}
    retained_occurrence_count = 0

    for message in messages:
        for occurrence in _message_candidates(message, config):
            retained_occurrence_count += 1
            if (
                max_occurrences is not None
                and retained_occurrence_count > max_occurrences
            ):
                raise CandidateBudgetExceededError(
                    "assistant history exceeds local analysis limit"
                )
            key = (message.language, occurrence.normalized)
            candidate_stats = stats.get(key)
            if candidate_stats is None:
                candidate_stats = _CandidateStats(0, set(), set(), [])
                stats[key] = candidate_stats
            candidate_stats.occurrence_count += 1
            candidate_stats.source_lines.add(message.source_line)
            candidate_stats.phrases.add(occurrence.phrase)
            candidate_stats.occurrences.append(occurrence)

    candidates: list[dict[str, object]] = []
    compiled_rules_cache: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {}
    protected_cache: dict[str, list[tuple[int, int]]] = {}
    match_cache: dict[tuple[str, str, int], tuple[tuple[int, int], ...]] = {}
    for (language, normalized), candidate_stats in stats.items():
        if candidate_stats.occurrence_count < config.threshold:
            continue
        if len(candidate_stats.source_lines) < message_count_threshold:
            continue
        covered_by, all_occurrences_covered = _coverage_result(
            language,
            candidate_stats.occurrences,
            current_rules,
            compiled_rules_cache=compiled_rules_cache,
            protected_cache=protected_cache,
            match_cache=match_cache,
        )
        if config.exclude_covered and all_occurrences_covered:
            continue
        candidates.append(
            {
                "covered_by_rule_ids": covered_by,
                "language": language,
                "message_count": len(candidate_stats.source_lines),
                "normalized_phrase": normalized,
                "occurrence_count": candidate_stats.occurrence_count,
                "phrase": min(
                    candidate_stats.phrases, key=lambda item: (item.casefold(), item)
                ),
                "status": "pending",
            }
        )

    candidates.sort(
        key=lambda item: (
            item["language"],
            -item["message_count"],
            -item["occurrence_count"],
            item["normalized_phrase"],
            item["phrase"],
        )
    )
    language_counts = Counter(message.language for message in messages)

    return {
        "artifact_type": ARTIFACT_TYPE,
        "candidates": candidates,
        "parameters": {
            "cjk_ngram_range": [config.cjk_ngram_min, config.cjk_ngram_max],
            "exclude_covered": config.exclude_covered,
            "min_length": config.min_length,
            "occurrence_threshold": config.threshold,
            "word_ngram_range": [config.word_ngram_min, config.word_ngram_max],
        },
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "assistant_message_count": len(messages),
            "candidate_count": len(candidates),
            "input_record_count": input_record_count,
            "language_counts": dict(sorted(language_counts.items())),
            "languages": sorted(language_counts),
        },
    }


def _clip_dominant_message(
    analyzed: list[SourceMessage],
) -> list[SourceMessage] | None:
    """Halve the one message that dominates the window, or None if none does.

    Found by POSITION, not assumed to be the newest. Both budgets clipped
    only the newest and then evicted history, so an oversized reply sitting
    anywhere else took the whole window down with it: three ordinary replies
    sharing a phrase plus one 128 KiB second-newest reply left exactly ONE
    message analyzed -- the evictions threw away the replies that make the
    distinct-message threshold reachable, and then threw away the outlier
    too -- and the panel reported not enough history for a window that had
    plenty.

    "Dominant" is measured against the rest rather than assumed, so a window
    that is merely large as a whole still narrows by dropping messages,
    which is the cheaper cut. Halving also terminates: a message stops being
    dominant after enough halvings, and the caller falls back to evicting.
    """
    if len(analyzed) < 2:
        return None
    index = max(
        range(len(analyzed)), key=lambda position: len(analyzed[position].content)
    )
    longest = analyzed[index]
    others = sum(len(message.content) for message in analyzed) - len(
        longest.content
    )
    # Against the AVERAGE of the rest, not their sum. Integer arithmetic so
    # the comparison cannot drift on a float.
    if (
        len(longest.content) * (len(analyzed) - 1)
        <= _USER_REVIEW_OUTLIER_RATIO * others
        or len(longest.content) <= _USER_REVIEW_MIN_TRUNCATED_CHARACTERS
    ):
        return None
    return (
        analyzed[:index]
        + [
            SourceMessage(
                language=longest.language,
                content=longest.content[: len(longest.content) // 2],
                source_line=longest.source_line,
            )
        ]
        + analyzed[index + 1 :]
    )


def build_user_review_report(
    messages: Sequence[SourceMessage],
    *,
    message_count_threshold: int = DEFAULT_MESSAGE_COUNT_THRESHOLD,
    rules_by_language: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> dict[str, object]:
    """Build the privacy-minimal report exposed by the user review UI.

    The maintainer CLI historically filters by total occurrences.  The user
    workflow is deliberately stricter: a phrase must also occur in at least
    ``message_count_threshold`` distinct assistant messages.

    Budget handling narrows the window instead of failing the request.  The two
    budgets are wildly out of proportion: n-gram expansion scales with the length
    of each punctuation-bounded *segment*, not with the total character count, so
    100 replies of ~280 unbroken Han characters bust
    ``USER_REVIEW_MAX_OCCURRENCES`` at only ~21% of
    ``USER_REVIEW_MAX_INPUT_CHARACTERS``.  Raising there turned an ordinary
    request into a 422 the UI could only render as "please try again", which never
    succeeds.  The oldest messages are dropped until the window fits, and the
    summary reports what was actually analyzed.
    """
    if message_count_threshold < 1:
        raise CandidateMinerError("message_count_threshold must be at least 1")

    analyzed = list(messages)
    content_truncated = False

    # Clip the NEWEST reply BEFORE narrowing, and leave room behind it.
    #
    # Narrowing first meant an oversized newest reply kept the total over
    # budget until every older reply had been dropped, and only the survivor
    # was then clipped. A character with plenty of ordinary history plus one
    # very long latest reply was therefore analysed as a single message, the
    # distinct-message threshold removed every candidate, and the panel told
    # the user there was not enough history -- while their history sat right
    # there.
    #
    # Clipping it to the WHOLE budget would not have helped: it would still
    # fill the window on its own and the loop below would still drop the
    # rest. So when there is history behind it, the newest reply may take at
    # most half, and the loop fills the remainder with as many preceding
    # replies as fit.
    #
    # Cutting mid-container is safe in the protective direction: an
    # unterminated fence or <code>/<pre> protects through the end of the
    # text, so a severed block stays protected rather than exposed.
    if analyzed:
        newest_budget = (
            USER_REVIEW_MAX_INPUT_CHARACTERS // 2
            if len(analyzed) > 1
            else USER_REVIEW_MAX_INPUT_CHARACTERS
        )
        newest = analyzed[-1]
        if len(newest.content) > newest_budget:
            analyzed = analyzed[:-1] + [
                SourceMessage(
                    language=newest.language,
                    content=newest.content[:newest_budget],
                    source_line=newest.source_line,
                )
            ]
            content_truncated = True

    while (
        len(analyzed) > 1
        and sum(len(message.content) for message in analyzed)
        > USER_REVIEW_MAX_INPUT_CHARACTERS
    ):
        # Clip a dominant message before evicting any. Evicting from the
        # front first discarded the replies that carry the repeated phrase
        # and then discarded the outlier anyway, so the window shrank to
        # one message and the panel reported not enough history.
        clipped = _clip_dominant_message(analyzed)
        if clipped is not None:
            analyzed = clipped
            content_truncated = True
            continue
        # Drop the oldest message that is ITSELF over its fair share of
        # the budget, not simply the oldest. A short old reply is not what
        # busted the budget and dropping it buys almost nothing, so with
        # four budget-sized replies in the window the three short ones
        # ahead of them were thrown away one by one and the window
        # collapsed to a single message -- the repeated phrase went with
        # them, and the panel reported not enough history.
        #
        # Order among the survivors is untouched: exactly one message
        # leaves. When nothing is over its share the oldest goes, which is
        # the behaviour a uniformly large window still gets -- narrowing
        # rather than cutting bodies, as the sibling test pins.
        share = USER_REVIEW_MAX_INPUT_CHARACTERS // len(analyzed)
        victim = next(
            (
                index
                for index, message in enumerate(analyzed)
                if len(message.content) > share
            ),
            0,
        )
        analyzed = analyzed[:victim] + analyzed[victim + 1 :]

    # A lone survivor can still be over the budget if it was never the
    # newest -- it cannot be, since narrowing keeps the newest, but the
    # single-message case takes the full budget above and needs no second cut.
    if analyzed and len(analyzed[0].content) > USER_REVIEW_MAX_INPUT_CHARACTERS:
        oversized = analyzed[0]
        analyzed = [
            SourceMessage(
                language=oversized.language,
                content=oversized.content[:USER_REVIEW_MAX_INPUT_CHARACTERS],
                source_line=oversized.source_line,
            )
        ] + analyzed[1:]
        content_truncated = True

    config = MiningConfig(threshold=DEFAULT_THRESHOLD)
    while True:
        try:
            maintainer_report = build_report(
                analyzed,
                input_record_count=len(analyzed),
                config=config,
                rules_by_language=rules_by_language,
                message_count_threshold=message_count_threshold,
                max_occurrences=USER_REVIEW_MAX_OCCURRENCES,
            )
            break
        except CandidateBudgetExceededError:
            # When the NEWEST reply is the outlier, halve ITS body first.
            #
            # Halving the window instead discarded the very history that
            # makes the distinct-message threshold reachable: three ordinary
            # replies sharing a phrase plus one very long reply narrowed to
            # that one reply, and the repeated phrase went with the messages
            # that carried it. The panel then reported not enough history.
            # Same ordering fault the character budget had, in the path that
            # actually binds: a reply long enough to matter passes the
            # occurrence budget long before the character one.
            #
            # "Outlier" is measured against the rest rather than assumed, so
            # a window that is merely large as a whole still narrows by
            # dropping messages, which is the cheaper cut.
            if len(analyzed) > 1:
                # By POSITION, not newest-only: this path had the same
                # blind spot the character budget did, and an outlier
                # sitting anywhere but last took the history with it.
                clipped = _clip_dominant_message(analyzed)
                if clipped is not None:
                    analyzed = clipped
                    content_truncated = True
                    continue
                analyzed = analyzed[-(len(analyzed) // 2):]
                continue
            # One reply left and it still busts the budget. Dropping whole
            # messages floors at one, so rethrowing here turned every
            # selection containing that reply into a 422 the panel can only
            # render as "please try again" -- and retrying never helps,
            # because the same reply is still the newest one. Halve its BODY
            # instead, which is the move the character cap above already
            # makes for the same reason.
            #
            # Not an exotic input: mining generates a fixed number of n-grams
            # per character, so an uninterrupted Chinese reply stops fitting
            # at about 20k characters -- a sixth of the 128 KiB the character
            # limit advertises. ASCII reaches roughly twice as far. Cutting
            # mid-container stays safe in the protective direction, for the
            # same reason the character cap gives above.
            remaining = analyzed[0]
            if len(remaining.content) <= _USER_REVIEW_MIN_TRUNCATED_CHARACTERS:
                raise
            analyzed = [
                SourceMessage(
                    language=remaining.language,
                    content=remaining.content[: len(remaining.content) // 2],
                    source_line=remaining.source_line,
                )
            ]
            content_truncated = True
    all_candidates = maintainer_report["candidates"]
    candidates = all_candidates[:USER_REVIEW_MAX_CANDIDATES]
    parameters = dict(maintainer_report["parameters"])
    parameters["message_count_threshold"] = message_count_threshold
    parameters["input_character_limit"] = USER_REVIEW_MAX_INPUT_CHARACTERS
    parameters["occurrence_retention_limit"] = USER_REVIEW_MAX_OCCURRENCES
    parameters["candidate_output_limit"] = USER_REVIEW_MAX_CANDIDATES

    return {
        "artifact_type": USER_REVIEW_ARTIFACT_TYPE,
        "candidates": candidates,
        "parameters": parameters,
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "assistant_message_count": len(messages),
            "analyzed_message_count": len(analyzed),
            # Two distinct mechanisms, reported separately: whole messages
            # dropped off the front (derivable from the two counts) versus one
            # oversized reply's BODY cut short (not derivable at all). Collapsing
            # them made the panel say "only the latest 1 of 1 fit".
            "messages_truncated": len(analyzed) < len(messages),
            "content_truncated": content_truncated,
            "candidate_count": len(all_candidates),
            "returned_candidate_count": len(candidates),
            "candidates_truncated": len(candidates) < len(all_candidates),
        },
    }


def serialize_report(report: Mapping[str, object]) -> str:
    """Serialize with stable key ordering and a single trailing newline."""
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_report(output_path: Path, report: Mapping[str, object]) -> None:
    """Atomically write a report using stable LF newlines."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(serialize_report(report))
        os.replace(temporary_name, output_path)
    except OSError as exc:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                # Do not mask the primary report write failure with cleanup failure.
                pass
        raise CandidateMinerError(
            f"unable to write output file: {output_path}"
        ) from exc


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mine pending natural-expression candidates from an explicitly provided "
            "local JSONL file. No rules are generated, modified, or activated."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="input JSONL file")
    parser.add_argument("--output", required=True, type=Path, help="review JSON file")
    parser.add_argument(
        "--language",
        help="explicit language/locale for every assistant record; overrides record lang",
    )
    parser.add_argument(
        "--threshold",
        type=_positive_int,
        default=DEFAULT_THRESHOLD,
        help=f"minimum occurrence count (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--word-ngram-min",
        type=_positive_int,
        default=DEFAULT_WORD_NGRAM_MIN,
    )
    parser.add_argument(
        "--word-ngram-max",
        type=_positive_int,
        default=DEFAULT_WORD_NGRAM_MAX,
    )
    parser.add_argument(
        "--cjk-ngram-min",
        type=_positive_int,
        default=DEFAULT_CJK_NGRAM_MIN,
    )
    parser.add_argument(
        "--cjk-ngram-max",
        type=_positive_int,
        default=DEFAULT_CJK_NGRAM_MAX,
    )
    parser.add_argument(
        "--min-length",
        type=_positive_int,
        default=DEFAULT_MIN_LENGTH,
        help=f"minimum non-space character length (default: {DEFAULT_MIN_LENGTH})",
    )
    parser.add_argument(
        "--exclude-covered",
        action="store_true",
        help="omit candidates matched by a current curated SLOP_RULES pattern",
    )
    parser.add_argument(
        "--debug-candidates",
        action="store_true",
        help="explicitly print candidate phrases; may expose assistant text",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if input_path == output_path:
        parser.error("--input and --output must be different files")

    config = MiningConfig(
        threshold=args.threshold,
        word_ngram_min=args.word_ngram_min,
        word_ngram_max=args.word_ngram_max,
        cjk_ngram_min=args.cjk_ngram_min,
        cjk_ngram_max=args.cjk_ngram_max,
        min_length=args.min_length,
        exclude_covered=args.exclude_covered,
    )
    try:
        messages, record_count = read_jsonl(
            input_path,
            language_override=args.language,
        )
        report = build_report(
            messages,
            input_record_count=record_count,
            config=config,
        )
        write_report(output_path, report)
    except CandidateMinerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary = report["summary"]
    languages = ", ".join(summary["languages"]) or "none"
    print(f"input: {input_path}")
    print(f"output: {output_path}")
    print(
        "assistant_messages="
        f"{summary['assistant_message_count']} candidates={summary['candidate_count']} "
        f"languages={languages}"
    )
    if args.debug_candidates:
        for candidate in report["candidates"]:
            print(f"[debug candidate] {candidate['language']}: {candidate['phrase']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
