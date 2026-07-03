"""Shared utilities for text normalization, similarity, and rate limiting."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timezone
from typing import Any

import Levenshtein

_normalize_re = re.compile(r"[^\w\s]")


def normalize_text(text: str | None) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    if not text:
        return ""
    text = _normalize_re.sub(" ", text.lower())
    return " ".join(text.split())


def title_similarity(a: str | None, b: str | None) -> float:
    """Return a ratio in [0, 1] comparing two titles."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return Levenshtein.ratio(na, nb)


def author_similarity(authors_a: list[str], authors_b: list[str]) -> float:
    """Simple Jaccard-ish overlap on normalized author names."""
    set_a = {normalize_text(a) for a in authors_a if normalize_text(a)}
    set_b = {normalize_text(b) for b in authors_b if normalize_text(b)}
    if not set_a and not set_b:
        return 0.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def parse_year(value: str | int | None) -> int | None:
    """Extract a 4-digit year from a string or int."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2100 else None
    match = re.search(r"(19|20)\d{2}", str(value))
    return int(match.group(0)) if match else None


def parse_date(value: str | None) -> date | None:
    """Parse ISO-ish dates; fall back to year-only January 1st."""
    from datetime import datetime

    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def to_json_blob(value: Any) -> str:
    return json.dumps(value, default=str)


def from_json_blob(value: str | None) -> Any:
    if value is None:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def utc_now() -> datetime:
    """Return the current naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RateLimiter:
    """Async token-bucket rate limiter."""

    def __init__(self, rate_per_second: float) -> None:
        self.rate = rate_per_second
        self.min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._last_release: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._last_release + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_release = asyncio.get_event_loop().time()
