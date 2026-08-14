from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class RecommendationConfig:
    enabled: bool = False
    shadow_mode: bool = True
    memory_bucket: str = "default"
    daily_limit: int = 2
    min_interval_minutes: int = 240
    quiet_start: str = "23:00"
    quiet_end: str = "09:00"
    score_threshold: float = 0.72
    candidate_ttl_hours: int = 12
    reply_window_minutes: int = 10
    ignored_window_minutes: int = 30
    max_consecutive_ignored: int = 2
    max_idle_seconds: int = 900
    min_user_silence_minutes: int = 20
    web_search: bool = True
    bilibili: bool = False
    background_llm: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RecommendationConfig":
        root = raw if isinstance(raw, Mapping) else {}
        rec = _section(root, "recommendation")
        sources = _section(rec, "sources")
        return cls(
            enabled=bool(rec.get("enabled", False)),
            shadow_mode=bool(rec.get("shadow_mode", True)),
            memory_bucket=str(rec.get("memory_bucket") or "default"),
            daily_limit=max(0, int(rec.get("daily_limit", 2))),
            min_interval_minutes=max(0, int(rec.get("min_interval_minutes", 240))),
            quiet_start=str(rec.get("quiet_start") or "23:00"),
            quiet_end=str(rec.get("quiet_end") or "09:00"),
            score_threshold=min(1.0, max(0.0, float(rec.get("score_threshold", 0.72)))),
            candidate_ttl_hours=max(1, int(rec.get("candidate_ttl_hours", 12))),
            reply_window_minutes=max(1, int(rec.get("reply_window_minutes", 10))),
            ignored_window_minutes=max(1, int(rec.get("ignored_window_minutes", 30))),
            max_consecutive_ignored=max(1, int(rec.get("max_consecutive_ignored", 2))),
            max_idle_seconds=max(0, int(rec.get("max_idle_seconds", 900))),
            min_user_silence_minutes=max(
                0, int(rec.get("min_user_silence_minutes", 20))
            ),
            web_search=bool(sources.get("web_search", True)),
            bilibili=bool(sources.get("bilibili", False)),
            background_llm=bool(rec.get("background_llm", True)),
        )
