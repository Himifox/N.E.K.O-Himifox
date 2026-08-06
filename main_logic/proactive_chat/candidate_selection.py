# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Generic candidate rendering and fair selection for proactive chat."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .decisions import _should_skip_source
from .preference_recommendation import (
    calculate_pool_probabilities,
    select_preference_candidate_batch,
)
from .state import _source_hash


def _format_phase1_link_candidate(index: int, item: dict[str, Any]) -> str:
    """Render useful candidate evidence without leaking bulky raw metadata."""

    title = str(item.get("title") or "").strip()
    details: list[str] = []
    field_labels = (
        ("source", "来源"),
        ("author", "作者"),
        ("reason", "推荐依据"),
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
) -> dict[str, list[dict[str, Any]]]:
    """Give every enabled web mode candidates before any one mode can dominate."""

    selected = {mode: [] for mode in modes}
    positions = {mode: 0 for mode in modes}
    links_by_mode = {
        mode: list((sources.get(mode) or {}).get("links", []) or [])
        for mode in modes
    }
    seen_keys: set[str] = set()
    remaining = max(0, total)
    while remaining:
        made_progress = False
        for mode in modes:
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


@dataclass(frozen=True, slots=True)
class PreferencePhase1PoolSelection:
    """The concrete cross-media candidate batch handed to Phase 1."""

    links_by_mode: dict[str, list[dict[str, Any]]]
    music_allocated: bool
    meme_allocated: bool
    pool_probabilities: dict[str, float]
    selected_counts: dict[str, int]
    exploration_slots: int
    available_candidates: int


def _preference_weighted_phase1_pool(
    modes: list[str],
    sources: dict[str, Any],
    *,
    total: int,
    preference_scores: dict[str, float],
    source_weights: dict[str, float],
    include_music: bool,
    include_meme: bool,
    rng: random.Random | None = None,
) -> PreferencePhase1PoolSelection:
    """Build one shared web/music/meme pool and reserve uniform exploration."""

    web_candidates: list[dict[str, Any]] = []
    positions = {mode: 0 for mode in modes}
    links_by_mode = {
        mode: list((sources.get(mode) or {}).get("links", []) or [])
        for mode in modes
    }
    seen_keys: set[str] = set()
    while True:
        made_progress = False
        for mode in modes:
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
                web_candidates.append(link)
                made_progress = True
                break
        if not made_progress:
            break

    task_candidates: list[dict[str, Any]] = []
    if include_music:
        task_candidates.append(
            {
                "title": "music recommendation task",
                "mode": "music",
                "_phase1_task": "music",
                "_preference_pool": "media/music",
            }
        )
    if include_meme:
        task_candidates.append(
            {
                "title": "meme recommendation task",
                "mode": "meme",
                "_phase1_task": "meme",
                "_preference_pool": "media/meme",
            }
        )

    candidates = task_candidates + web_candidates
    selection = select_preference_candidate_batch(
        candidates,
        preference_scores,
        total=max(0, total),
        rng=rng,
        source_weights=source_weights,
    )
    selected = selection.items
    selected_by_mode = {mode: [] for mode in modes}
    selected_tasks: set[str] = set()
    selected_counts: dict[str, int] = {}
    for item in selected:
        task = str(item.get("_phase1_task", ""))
        mode = str(item.get("mode", ""))
        if task:
            selected_tasks.add(task)
        elif mode in selected_by_mode:
            selected_by_mode[mode].append(item)
        selected_counts[mode] = selected_counts.get(mode, 0) + 1

    probabilities = calculate_pool_probabilities(
        candidates,
        preference_scores,
        source_weights=source_weights,
    )
    return PreferencePhase1PoolSelection(
        links_by_mode=selected_by_mode,
        music_allocated="music" in selected_tasks,
        meme_allocated="meme" in selected_tasks,
        pool_probabilities=probabilities,
        selected_counts=selected_counts,
        exploration_slots=selection.exploration_slots,
        available_candidates=len(candidates),
    )
