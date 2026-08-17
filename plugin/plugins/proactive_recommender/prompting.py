from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse


_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(\s*https?://[^)\s]+\s*\)", re.IGNORECASE
)
_PLACEHOLDER_RE = re.compile(
    r"[\[【（(][^\]】）)]*(?:url|link|链接|網址|网址)[^\]】）)]*[\]】）)]",
    re.IGNORECASE,
)
_TRAILING_LINK_CUE_RE = re.compile(
    r"(?:链接|連結|網址|网址|url|link)\s*(?:在这|在這|如下|是|为|為)?\s*[:：]?\s*$",
    re.IGNORECASE,
)


def canonical_candidate_url(candidate: Mapping[str, Any]) -> str:
    url = str(candidate.get("url") or "").strip()[:1000]
    if not url or any(character.isspace() for character in url):
        return ""
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    return url


def sanitize_delivery_copy(value: object) -> str:
    text = str(value or "").strip()[:1200]
    if not text:
        return ""
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _PLACEHOLDER_RE.sub("", text)
    text = _HTTP_URL_RE.sub("", text)
    text = _TRAILING_LINK_CUE_RE.sub("", text).strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \t\r\n-—:：")[:600]


def build_delivery_message(
    candidate: Mapping[str, Any], generated_copy: object = ""
) -> str:
    """Build one verbatim chat bubble with exactly one trusted candidate URL."""
    url = canonical_candidate_url(candidate)
    if not url:
        return ""
    copy = sanitize_delivery_copy(generated_copy)
    if not copy:
        title = sanitize_delivery_copy(candidate.get("title"))[:240]
        snippet = sanitize_delivery_copy(candidate.get("snippet"))[:260]
        if not title:
            return ""
        copy = f"我看到一个可能很对你胃口的内容：《{title}》。"
        if snippet:
            copy = f"{copy}\n{snippet}"
    return f"{copy}\n\n[打开内容]({url})"


def build_delivery_context(candidate: Mapping[str, Any], message: str) -> str:
    """Give the next NEKO turn memory of the verbatim recommendation bubble."""
    title = sanitize_delivery_copy(candidate.get("title"))[:240]
    return (
        "The recommendation plugin just displayed the following assistant message "
        "verbatim to {MASTER_NAME}. Treat it as something you already said; if the "
        "user asks about it, continue naturally without claiming you opened or verified "
        f"the content.\nTitle: {title}\nDelivered message:\n{message}"
    )
