from __future__ import annotations

import re
import unicodedata
from datetime import date


WHITESPACE_OR_SEPARATOR = re.compile(r"[\s\-‐‑‒–—―_·・,，.。()（）\[\]【】{}]+")


def normalized_key(value: str | None) -> str:
    """Create a stable comparison key without rewriting the user-facing value."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return WHITESPACE_OR_SEPARATOR.sub("", normalized)


def normalized_contains(haystack: str | None, needle: str | None) -> bool:
    needle_key = normalized_key(needle)
    return bool(needle_key and needle_key in normalized_key(haystack))


DEGREE_RANK = {
    "unknown": 0,
    "vocational_or_below": 1,
    "high_school": 2,
    "associate": 3,
    "bachelor": 4,
    "master": 5,
    "doctor": 6,
}


def highest_degree(values: list[str]) -> str | None:
    if not values:
        return None
    return max(values, key=lambda value: DEGREE_RANK.get(value, 0))


def month_index(value: str) -> int:
    year_text, month_text = value.split("-", 1)
    return int(year_text) * 12 + int(month_text) - 1


def current_month() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def merged_month_count(intervals: list[tuple[str | None, str | None, bool]]) -> int:
    """Avoid double-counting overlapping employment intervals."""
    normalized_intervals: list[tuple[int, int]] = []
    for start, end, is_current in intervals:
        if not start:
            continue
        resolved_end = current_month() if is_current else end
        if not resolved_end:
            continue
        start_index = month_index(start)
        end_index = month_index(resolved_end)
        if end_index >= start_index:
            normalized_intervals.append((start_index, end_index))
    if not normalized_intervals:
        return 0

    normalized_intervals.sort()
    total = 0
    active_start, active_end = normalized_intervals[0]
    for start, end in normalized_intervals[1:]:
        if start <= active_end + 1:
            active_end = max(active_end, end)
            continue
        total += active_end - active_start + 1
        active_start, active_end = start, end
    return total + active_end - active_start + 1
