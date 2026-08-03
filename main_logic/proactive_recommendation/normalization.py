"""Pure value normalization primitives for recommendation contracts."""

from __future__ import annotations

from collections.abc import Sequence
import math
from typing import Any


_SOURCE_IDENTIFIER_ALIASES = {
    "home": "web",
}


def to_stripped_text(value: Any) -> str:
    return str(value or "").strip()


def coerce_float_or_default(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_finite_float(value: Any, *, default: float = 0.0) -> float:
    number = coerce_float_or_default(value, default=default)
    return number if math.isfinite(number) else default


def clamp_to_range(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def clamp_to_unit_interval(value: float) -> float:
    return clamp_to_range(float(value), 0.0, 1.0)


def sanitize_string_sequence(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence):
        return []
    return [text for item in value if (text := to_stripped_text(item))]


def sanitize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [sanitize_json_value(item) for item in value]
    return str(value)


def normalize_source_identifier(value: Any) -> str:
    source_identifier = to_stripped_text(value).lower()
    if source_identifier and source_identifier.replace("_", "").isalnum():
        return _SOURCE_IDENTIFIER_ALIASES.get(source_identifier, source_identifier)
    return ""


def coerce_nonnegative_integer_count(
    value: Any,
    *,
    maximum: int = 1_000_000,
) -> int:
    try:
        return max(0, min(maximum, int(value)))
    except (TypeError, ValueError):
        return 0


def coerce_bounded_evidence_weight(
    value: Any,
    *,
    maximum: float = 1_000_000.0,
) -> float:
    return clamp_to_range(coerce_finite_float(value), 0.0, maximum)


def rounded_ratio_or_none(
    numerator: int | float,
    denominator: int | float,
    *,
    digits: int = 3,
) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, digits)


def rounded_mean_or_none(
    values: Sequence[int | float],
    *,
    digits: int = 3,
) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), digits)


__all__ = [
    "clamp_to_range",
    "clamp_to_unit_interval",
    "coerce_bounded_evidence_weight",
    "coerce_finite_float",
    "coerce_float_or_default",
    "coerce_nonnegative_integer_count",
    "normalize_source_identifier",
    "rounded_mean_or_none",
    "rounded_ratio_or_none",
    "sanitize_json_value",
    "sanitize_string_sequence",
    "to_stripped_text",
]
