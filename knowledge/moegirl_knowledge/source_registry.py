"""Source-level public-knowledge policy and display metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    tag: str
    name: str
    homepage: str
    license: str
    supports_sync: bool


SOURCES: dict[str, KnowledgeSource] = {
    "source:chime": KnowledgeSource("source:chime", "CHIME", "https://github.com/yuboxie/chime", "MIT", False),
    "source:geng-guide": KnowledgeSource("source:geng-guide", "梗指南", "local-import://geng-guide-output.md", "User-provided export; license not stated", False),
    "source:moegirl": KnowledgeSource("source:moegirl", "萌娘百科", "https://zh.moegirl.org.cn/", "CC BY-NC-SA 3.0 CN", True),
    "source:geng8": KnowledgeSource("source:geng8", "梗8", "https://www.geng8.com/", "Verify site terms before redistribution", True),
}


def get_source(tag: str) -> KnowledgeSource:
    return SOURCES.get(tag, KnowledgeSource(tag, tag.removeprefix("source:"), "", "Unknown", False))
