"""Source-level public-knowledge policy and display metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .filters import sanitize_external_text


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


def get_source(
    tag: str,
    *,
    database_path: str | Path | None = None,
) -> KnowledgeSource:
    source = SOURCES.get(tag)
    if source is not None:
        return source
    if database_path is not None:
        source = _get_pack_source(tag, Path(database_path).with_name("packs.json"))
        if source is not None:
            return source
    return KnowledgeSource(tag, tag.removeprefix("source:"), "", "Unknown", False)


def _get_pack_source(tag: str, registry_path: Path) -> KnowledgeSource | None:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    packs = payload.get("packs") if isinstance(payload, dict) else None
    if not isinstance(packs, dict):
        return None
    for pack in packs.values():
        if not isinstance(pack, dict) or pack.get("source_tag") != tag:
            continue
        source = pack.get("source")
        if not isinstance(source, dict):
            return None
        return KnowledgeSource(
            tag=tag,
            name=sanitize_external_text(
                str(source.get("name") or tag.removeprefix("source:")),
                max_chars=200,
            ),
            homepage=sanitize_external_text(str(source.get("homepage") or ""), max_chars=2_000),
            license=sanitize_external_text(
                str(source.get("license") or "Unknown"),
                max_chars=500,
            ),
            supports_sync=False,
        )
    return None
