"""Bounded retry timing helpers for model-provider requests."""

from __future__ import annotations

import math
import secrets
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any


def parse_retry_after_seconds(
    value_or_headers: Any, *, now: datetime | None = None
) -> float | None:
    """Parse Retry-After seconds or an HTTP date without accepting booleans."""

    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("Retry-After")
            if raw is None:
                raw = getter("retry-after")
        except (KeyError, TypeError, ValueError):
            return None
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        seconds = float(raw)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter_ratio: float = 0.25,
) -> float:
    """Return capped exponential backoff with process-safe random jitter."""

    exponent = max(0, attempt - 1)
    delay = max_delay if exponent >= 32 else min(base_delay * (2**exponent), max_delay)
    if delay <= 0 or jitter_ratio <= 0:
        return max(0.0, delay)
    fraction = secrets.randbelow(1_000_001) / 1_000_000
    return delay + delay * jitter_ratio * fraction
