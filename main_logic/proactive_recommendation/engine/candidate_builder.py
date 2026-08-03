"""Candidate construction for proactive recommendation sources and materials."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from typing import Any

from main_logic.proactive_recommendation.domain_models import (
    ProactiveCandidate,
    ProactiveRecommendationContext,
)
from main_logic.proactive_recommendation.normalization import (
    clamp_to_unit_interval,
    coerce_float_or_default,
    sanitize_string_sequence,
    to_stripped_text,
)


def build_candidates(
    context: ProactiveRecommendationContext,
    sources: Mapping[str, Mapping[str, Any]],
) -> list[ProactiveCandidate]:
    candidates: list[ProactiveCandidate] = []
    enabled = set(context.enabled_modes or ())

    for source_type, content in sources.items():
        if not isinstance(content, Mapping):
            continue
        if enabled and source_type not in enabled:
            continue
        candidates.extend(build_source_candidates(source_type, content))

    for material in context.topic_materials or ():
        if isinstance(material, Mapping):
            candidate = build_topic_materialcreate_candidate(material)
            if candidate is not None:
                candidates.append(candidate)

    if context.mini_game_available:
        candidates.append(
            create_candidate(
                "mini_game",
                "mini_game_invite",
                "mini-game invite",
                "mini-game invite",
                payload={"available": True},
                freshness=0.6,
                quality=0.45,
            )
        )

    return candidates


def build_phase1_material_candidates(
    context: ProactiveRecommendationContext,
    *,
    phase1_topics: Sequence[Any],
    selected_web_link: Mapping[str, Any] | None,
    selected_music_link: Mapping[str, Any] | None,
    selected_meme_link: Mapping[str, Any] | None,
    vision_content: Mapping[str, Any] | None,
    active_channels: Sequence[Any],
) -> list[ProactiveCandidate]:
    candidates: list[ProactiveCandidate] = []
    topic_by_channel = _phase1_topic_by_channel(phase1_topics)
    active = set(sanitize_string_sequence(active_channels))

    if isinstance(selected_web_link, Mapping):
        source_type = _web_material_source_type(context, selected_web_link)
        title = (
            to_stripped_text(selected_web_link.get("title"))
            or to_stripped_text(topic_by_channel.get("web"))
            or "web material"
        )
        candidates.append(
            create_candidate(
                source_type,
                _family_for_source(source_type),
                title,
                to_stripped_text(topic_by_channel.get("web")) or title,
                payload={
                    "link": _safe_link_payload(selected_web_link),
                    "material_stage": "phase1",
                },
                freshness=0.85,
                quality=_link_metadata_quality_score(selected_web_link),
            )
        )
    elif "web" in active and topic_by_channel.get("web"):
        source_type = _fallback_web_source_type(context)
        topic = to_stripped_text(topic_by_channel.get("web"))
        candidates.append(
            create_candidate(
                source_type,
                _family_for_source(source_type),
                topic,
                topic,
                payload={"material_stage": "phase1"},
                freshness=0.65,
                quality=0.45,
            )
        )

    if isinstance(selected_music_link, Mapping):
        title = to_stripped_text(selected_music_link.get("title")) or "music material"
        artist = to_stripped_text(selected_music_link.get("artist"))
        topic = f"{title} - {artist}".strip(" -") if artist else title
        candidates.append(
            create_candidate(
                "music",
                "music",
                topic,
                to_stripped_text(topic_by_channel.get("music")) or topic,
                payload={
                    "link": _safe_link_payload(selected_music_link),
                    "material_stage": "phase1",
                },
                freshness=0.8,
                quality=_link_metadata_quality_score(selected_music_link),
            )
        )

    if isinstance(selected_meme_link, Mapping):
        title = to_stripped_text(selected_meme_link.get("title")) or "meme material"
        candidates.append(
            create_candidate(
                "meme",
                "meme",
                title,
                to_stripped_text(topic_by_channel.get("meme")) or title,
                payload={
                    "link": _safe_link_payload(selected_meme_link),
                    "material_stage": "phase1",
                },
                freshness=0.8,
                quality=_link_metadata_quality_score(selected_meme_link),
            )
        )

    if isinstance(vision_content, Mapping) and ("vision" in active or vision_content):
        title = to_stripped_text(vision_content.get("window_title")) or "screen context"
        candidates.append(
            create_candidate(
                "vision",
                "screen_context",
                title,
                title,
                payload={
                    "window_title": title,
                    "material_stage": "phase1",
                },
                freshness=0.75,
                quality=0.65 if title != "screen context" else 0.45,
                risk_flags=("screen",),
            )
        )

    for material in context.topic_materials or ():
        if isinstance(material, Mapping):
            candidate = build_topic_materialcreate_candidate(material)
            if candidate is not None:
                candidates.append(candidate)

    return candidates


def build_source_candidates(
    source_type: str, content: Mapping[str, Any]
) -> list[ProactiveCandidate]:
    if source_type == "vision":
        title = to_stripped_text(content.get("window_title")) or "screen context"
        quality = 0.75 if to_stripped_text(content.get("screenshot_b64")) else 0.35
        return [
            create_candidate(
                source_type,
                "screen_context",
                title,
                title,
                payload=dict(content),
                freshness=0.8,
                quality=quality,
                risk_flags=("screen",),
            )
        ]

    if content.get("placeholder"):
        note = to_stripped_text(content.get("note")) or f"{source_type} placeholder"
        return [
            create_candidate(
                source_type,
                _family_for_source(source_type),
                source_type,
                note,
                payload=dict(content),
                freshness=0.35,
                quality=0.25,
                risk_flags=("placeholder",),
            )
        ]

    links = content.get("links")
    if isinstance(links, Sequence) and not isinstance(links, (str, bytes)):
        out = []
        for link in links:
            if not isinstance(link, Mapping):
                continue
            title = to_stripped_text(link.get("title"))
            if not title:
                continue
            out.append(
                create_candidate(
                    source_type,
                    _family_for_source(source_type),
                    title,
                    to_stripped_text(link.get("summary")) or title,
                    payload={
                        "link": dict(link),
                        "raw_source": _source_hint_from_raw_data(content),
                    },
                    freshness=0.75,
                    quality=0.75 if to_stripped_text(link.get("url")) else 0.55,
                )
            )
            if len(out) >= 5:
                break
        if out:
            return out

    formatted = to_stripped_text(content.get("formatted_content"))
    raw = (
        content.get("raw_data") if isinstance(content.get("raw_data"), Mapping) else {}
    )
    fallback_topic = _first_content_line(formatted) or to_stripped_text(raw.get("window_title"))
    if not fallback_topic:
        return []
    return [
        create_candidate(
            source_type,
            _family_for_source(source_type),
            fallback_topic,
            fallback_topic,
            payload={"raw_source": _source_hint_from_raw_data(content)},
            freshness=0.55,
            quality=0.45,
        )
    ]


def build_topic_materialcreate_candidate(
    material: Mapping[str, Any],
) -> ProactiveCandidate | None:
    topic = to_stripped_text(material.get("interest"))
    if not topic:
        return None
    relevance = (
        coerce_float_or_default(material.get("relevance"), default=70.0) / 100.0
    )
    risk = coerce_float_or_default(material.get("risk"), default=20.0) / 100.0
    risk_flags = ("topic_risk",) if risk >= 0.65 else ()
    hint = material.get("material_hint")
    summary = ""
    if isinstance(hint, Mapping):
        summary = to_stripped_text(hint.get("summary"))
    return create_candidate(
        "topic_hook",
        "topic_hook",
        topic,
        summary or topic,
        payload=dict(material),
        freshness=0.8,
        quality=max(0.45, min(1.0, relevance)),
        risk_flags=risk_flags,
    )


def create_candidate(
    source_type: str,
    family: str,
    topic: str,
    summary: str,
    *,
    payload: dict[str, Any],
    freshness: float,
    quality: float,
    risk_flags: tuple[str, ...] = (),
) -> ProactiveCandidate:
    return ProactiveCandidate(
        id=make_candidate_id(source_type, topic, payload),
        source_type=source_type,
        family=family,
        topic=topic,
        summary=summary,
        payload=payload,
        freshness=clamp_to_unit_interval(freshness),
        risk_flags=risk_flags,
        quality=clamp_to_unit_interval(quality),
    )


def make_candidate_id(source_type: str, topic: str, payload: Mapping[str, Any]) -> str:
    link = payload.get("link")
    url = ""
    if isinstance(link, Mapping):
        url = to_stripped_text(link.get("url"))
    raw = f"{source_type}|{topic}|{url}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{source_type}:{digest}"


def _family_for_source(source_type: str) -> str:
    return {
        "news": "news",
        "video": "video",
        "home": "trending",
        "personal": "personal_dynamic",
        "window": "window_context",
        "music": "music",
        "meme": "meme",
        "vision": "screen_context",
    }.get(source_type, source_type)


def _phase1_topic_by_channel(phase1_topics: Sequence[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in phase1_topics or ():
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) < 2
        ):
            continue
        channel = to_stripped_text(item[0])
        topic = to_stripped_text(item[1])
        if channel and topic and channel not in out:
            out[channel] = topic
    return out


def _web_material_source_type(
    context: ProactiveRecommendationContext,
    selected_web_link: Mapping[str, Any],
) -> str:
    mode = to_stripped_text(selected_web_link.get("mode"))
    if mode:
        return mode
    return _fallback_web_source_type(context)


def _fallback_web_source_type(context: ProactiveRecommendationContext) -> str:
    for mode in context.enabled_modes or ():
        normalized = to_stripped_text(mode)
        if normalized in {"news", "video", "home", "personal"}:
            return normalized
    return "web"


def _safe_link_payload(link: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("title", "artist", "url", "source", "type", "mode")
    return {key: to_stripped_text(link.get(key)) for key in keep if to_stripped_text(link.get(key))}


def _link_metadata_quality_score(link: Mapping[str, Any]) -> float:
    title = bool(to_stripped_text(link.get("title")))
    url = bool(to_stripped_text(link.get("url")))
    source = bool(to_stripped_text(link.get("source")))
    artist = bool(to_stripped_text(link.get("artist")))
    return min(1.0, 0.35 + 0.25 * title + 0.25 * url + 0.10 * source + 0.05 * artist)


def _source_hint_from_raw_data(content: Mapping[str, Any]) -> str:
    raw = content.get("raw_data")
    if isinstance(raw, Mapping):
        return to_stripped_text(raw.get("source")) or to_stripped_text(raw.get("region"))
    return ""


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:160]
    return ""


