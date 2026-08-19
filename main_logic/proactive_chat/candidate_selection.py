# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Generic candidate rendering and fair selection for proactive chat."""

from __future__ import annotations

from typing import Any

from .decisions import _should_skip_source
from .state import _source_hash


def _format_phase1_link_candidate(index: int, item: dict[str, Any]) -> str:
    """Render useful candidate evidence without leaking bulky raw metadata."""

    envelope = item.get("_openbiliclaw_candidate")
    if envelope is not None:
        from .openbiliclaw_candidate import format_phase1_candidate

        return format_phase1_candidate(index, envelope)

    title = str(item.get("title") or "").strip()
    details: list[str] = []
    field_labels = (
        ("source", "来源"),
        ("author", "作者"),
        ("reason", "推荐依据"),
        ("topic_label", "个性化主题"),
        ("confidence", "置信度"),
        ("description_hint", "简介"),
        ("url", "URL"),
    )
    for field, label in field_labels:
        value = " ".join(str(item.get(field) or "").split())
        if not value:
            continue
        if field == "description_hint":
            value = value[:240]
        details.append(f"{label}: {value}")
    published_at = item.get("published_at")
    if published_at:
        details.append(f"发布时间戳: {published_at}")
    suffix = f" | {' | '.join(details)}" if details else ""
    return f"{index}. {title}{suffix}"


def _phase1_linkless_modes(
    modes: list[str], sources: dict[str, Any]
) -> list[str]:
    """Return formatted-only modes that each need a Phase 1 budget slot."""

    return [
        mode
        for mode in modes
        if not ((sources.get(mode) or {}).get("links") or [])
        and str((sources.get(mode) or {}).get("formatted_content") or "").strip()
    ]


def _round_robin_phase1_links(
    modes: list[str],
    sources: dict[str, Any],
    *,
    total: int,
    reserved_mode: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Give every web mode a turn, reserving at most one OBC slot when present."""

    selected = {mode: [] for mode in modes}
    positions = {mode: 0 for mode in modes}
    links_by_mode = {
        mode: list((sources.get(mode) or {}).get("links", []) or [])
        for mode in modes
    }
    seen_keys: set[str] = set()
    remaining = max(0, total)
    if reserved_mode in links_by_mode and remaining:
        reserved_links = links_by_mode[reserved_mode]
        reserved_limit = min(1, remaining)
        while (
            positions[reserved_mode] < len(reserved_links)
            and len(selected[reserved_mode]) < reserved_limit
        ):
            link = dict(reserved_links[positions[reserved_mode]])
            positions[reserved_mode] += 1
            key = _source_hash(link.get("url", ""), link.get("title", ""))
            if key and (key in seen_keys or _should_skip_source(key)):
                continue
            if key:
                seen_keys.add(key)
            link.setdefault("mode", reserved_mode)
            selected[reserved_mode].append(link)
            remaining -= 1
    while remaining:
        made_progress = False
        for mode in modes:
            if mode == reserved_mode and selected[mode]:
                continue
            links = links_by_mode[mode]
            while positions[mode] < len(links):
                link = dict(links[positions[mode]])
                positions[mode] += 1
                key = _source_hash(link.get("url", ""), link.get("title", ""))
                if key and (key in seen_keys or _should_skip_source(key)):
                    continue
                if key:
                    seen_keys.add(key)
                link.setdefault("mode", mode)
                selected[mode].append(link)
                remaining -= 1
                made_progress = True
                break
            if remaining <= 0:
                break
        if not made_progress:
            break
    return selected
