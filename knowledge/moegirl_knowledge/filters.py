"""Deterministic text handling for untrusted encyclopedia content."""

from __future__ import annotations

import re
import unicodedata


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CHATML_RE = re.compile(r"<\|(?:im_start|im_end|endoftext)\|>", re.IGNORECASE)
_ROLE_MARKER_RE = re.compile(r"(?im)^\s*(?:system|developer|assistant|user)\s*[:：].*$")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_NEWLINES_RE = re.compile(r"\n{3,}")
_FTS_TOKEN_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_MEME_FILLER_RE = re.compile(r"(?:这是|这个是|就是|是|吧|啊|呀|呢|了|的|嘛|啦|么|吗)")
_MEME_PRONOUN_RE = re.compile(r"[我你他她它]")


def sanitize_external_text(value: str, *, max_chars: int = 80_000) -> str:
    """Normalize externally sourced text without interpreting it as instructions."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _CONTROL_CHARS_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CHATML_RE.sub("", text)
    text = _ROLE_MARKER_RE.sub("", text)
    text = "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _NEWLINES_RE.sub("\n\n", text).strip()
    return text[:max_chars].strip()


def normalize_search_text(value: str) -> str:
    """Return a comparison-friendly value for title, alias, and tag matching."""
    return "".join(_FTS_TOKEN_RE.split(unicodedata.normalize("NFKC", str(value or "")).casefold()))


def normalize_meme_phrase(value: str) -> str:
    """Normalize a conversational rendering of a known meme title.

    This is deliberately narrower than semantic search: it removes common
    sentence glue and maps Chinese personal pronouns to one placeholder.  It
    lets a title such as ``他在 CPU 你`` match ``他这是在 CPU 我吧`` without
    treating unrelated prose or source content as a meme alias.
    """
    normalized = normalize_search_text(value)
    normalized = _MEME_FILLER_RE.sub("", normalized)
    return _MEME_PRONOUN_RE.sub("人", normalized)


def is_relevant_source_page(query: str, *, title: str, content: str) -> bool:
    """Require the requested term to occur in a discovered public-source page.

    MediaWiki's generator search can return weak or unrelated candidates.  The
    synchronizer therefore treats search as discovery only, never as proof that
    the result represents the requested meme.
    """
    normalized_query = normalize_search_text(query)
    if len(normalized_query) < 2:
        return False
    return normalized_query in normalize_search_text(title) or normalized_query in normalize_search_text(content)


def make_fts_query(value: str) -> str:
    """Build a conservative FTS5 query from user-supplied text.

    Quoted tokens keep FTS operators in the input from changing query semantics.
    """
    tokens = [token for token in _FTS_TOKEN_RE.split(sanitize_external_text(value, max_chars=200)) if token]
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
