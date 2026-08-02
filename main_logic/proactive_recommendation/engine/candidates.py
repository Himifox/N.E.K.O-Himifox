"""Candidate construction for proactive recommendation sources and materials."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from typing import Any

from main_logic.proactive_recommendation.contracts import (
    ProactiveCandidate,
    ProactiveRecommendationContext,
)


def build_candidates(
    ctx: ProactiveRecommendationContext,
    sources: Mapping[str, Mapping[str, Any]],
) -> list[ProactiveCandidate]:
    candidates: list[ProactiveCandidate] = []
    enabled = set(ctx.enabled_modes or ())

    for source_type, content in sources.items():
        if not isinstance(content, Mapping):
            continue
        if enabled and source_type not in enabled:
            continue
        candidates.extend(build_source_candidates(source_type, content))

    for material in ctx.topic_materials or ():
        if isinstance(material, Mapping):
            candidate = build_topic_materialcreate_candidate(material)
            if candidate is not None:
                candidates.append(candidate)

    if ctx.mini_game_available:
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
    ctx: ProactiveRecommendationContext,
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
    active = set(_clean_string_list(active_channels))

    if isinstance(selected_web_link, Mapping):
        source_type = _web_material_source_type(ctx, selected_web_link)
        title = (
            _text(selected_web_link.get("title"))
            or _text(topic_by_channel.get("web"))
            or "web material"
        )
        candidates.append(
            create_candidate(
                source_type,
                _family_for_source(source_type),
                title,
                _text(topic_by_channel.get("web")) or title,
                payload={
                    "link": _safe_link_payload(selected_web_link),
                    "material_stage": "phase1",
                },
                freshness=0.85,
                quality=_link_quality(selected_web_link),
            )
        )
    elif "web" in active and topic_by_channel.get("web"):
        source_type = _fallback_web_source_type(ctx)
        topic = _text(topic_by_channel.get("web"))
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
        title = _text(selected_music_link.get("title")) or "music material"
        artist = _text(selected_music_link.get("artist"))
        topic = f"{title} - {artist}".strip(" -") if artist else title
        candidates.append(
            create_candidate(
                "music",
                "music",
                topic,
                _text(topic_by_channel.get("music")) or topic,
                payload={
                    "link": _safe_link_payload(selected_music_link),
                    "material_stage": "phase1",
                },
                freshness=0.8,
                quality=_link_quality(selected_music_link),
            )
        )

    if isinstance(selected_meme_link, Mapping):
        title = _text(selected_meme_link.get("title")) or "meme material"
        candidates.append(
            create_candidate(
                "meme",
                "meme",
                title,
                _text(topic_by_channel.get("meme")) or title,
                payload={
                    "link": _safe_link_payload(selected_meme_link),
                    "material_stage": "phase1",
                },
                freshness=0.8,
                quality=_link_quality(selected_meme_link),
            )
        )

    if isinstance(vision_content, Mapping) and ("vision" in active or vision_content):
        title = _text(vision_content.get("window_title")) or "screen context"
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

    for material in ctx.topic_materials or ():
        if isinstance(material, Mapping):
            candidate = build_topic_materialcreate_candidate(material)
            if candidate is not None:
                candidates.append(candidate)

    return candidates


def build_source_candidates(
    source_type: str, content: Mapping[str, Any]
) -> list[ProactiveCandidate]:
    if source_type == "vision":
        title = _text(content.get("window_title")) or "screen context"
        quality = 0.75 if _text(content.get("screenshot_b64")) else 0.35
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
        note = _text(content.get("note")) or f"{source_type} placeholder"
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
            title = _text(link.get("title"))
            if not title:
                continue
            out.append(
                create_candidate(
                    source_type,
                    _family_for_source(source_type),
                    title,
                    _text(link.get("summary")) or title,
                    payload={
                        "link": dict(link),
                        "raw_source": _raw_source_hint(content),
                    },
                    freshness=0.75,
                    quality=0.75 if _text(link.get("url")) else 0.55,
                )
            )
            if len(out) >= 5:
                break
        if out:
            return out

    formatted = _text(content.get("formatted_content"))
    raw = (
        content.get("raw_data") if isinstance(content.get("raw_data"), Mapping) else {}
    )
    fallback_topic = _first_content_line(formatted) or _text(raw.get("window_title"))
    if not fallback_topic:
        return []
    return [
        create_candidate(
            source_type,
            _family_for_source(source_type),
            fallback_topic,
            fallback_topic,
            payload={"raw_source": _raw_source_hint(content)},
            freshness=0.55,
            quality=0.45,
        )
    ]


def build_topic_materialcreate_candidate(
    material: Mapping[str, Any],
) -> ProactiveCandidate | None:
    topic = _text(material.get("interest"))
    if not topic:
        return None
    relevance = _number(material.get("relevance"), 70.0) / 100.0
    risk = _number(material.get("risk"), 20.0) / 100.0
    risk_flags = ("topic_risk",) if risk >= 0.65 else ()
    hint = material.get("material_hint")
    summary = ""
    if isinstance(hint, Mapping):
        summary = _text(hint.get("summary"))
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
        freshness=_clamp01(freshness),
        risk_flags=risk_flags,
        quality=_clamp01(quality),
    )


def make_candidate_id(source_type: str, topic: str, payload: Mapping[str, Any]) -> str:
    link = payload.get("link")
    url = ""
    if isinstance(link, Mapping):
        url = _text(link.get("url"))
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
        channel = _text(item[0])
        topic = _text(item[1])
        if channel and topic and channel not in out:
            out[channel] = topic
    return out


def _web_material_source_type(
    ctx: ProactiveRecommendationContext,
    selected_web_link: Mapping[str, Any],
) -> str:
    mode = _text(selected_web_link.get("mode"))
    if mode:
        return mode
    return _fallback_web_source_type(ctx)


def _fallback_web_source_type(ctx: ProactiveRecommendationContext) -> str:
    for mode in ctx.enabled_modes or ():
        normalized = _text(mode)
        if normalized in {"news", "video", "home", "personal"}:
            return normalized
    return "web"


def _safe_link_payload(link: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("title", "artist", "url", "source", "type", "mode")
    return {key: _text(link.get(key)) for key in keep if _text(link.get(key))}


def _link_quality(link: Mapping[str, Any]) -> float:
    title = bool(_text(link.get("title")))
    url = bool(_text(link.get("url")))
    source = bool(_text(link.get("source")))
    artist = bool(_text(link.get("artist")))
    return min(1.0, 0.35 + 0.25 * title + 0.25 * url + 0.10 * source + 0.05 * artist)


def _raw_source_hint(content: Mapping[str, Any]) -> str:
    raw = content.get("raw_data")
    if isinstance(raw, Mapping):
        return _text(raw.get("source")) or _text(raw.get("region"))
    return ""


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:160]
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence):
        return []
    return [_text(item) for item in value if _text(item)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
