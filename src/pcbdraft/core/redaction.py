"""Shared redaction for text that may cross a durable or UI boundary."""

from __future__ import annotations

import os
import re


def sanitize_user_text(value: str) -> str:
    """Redact obvious credentials before text is persisted or rendered.

    This deliberately lives in ``core`` rather than the application service:
    protocol adapters and durable agent records need the same safety guarantee
    without depending on the service implementation.
    """

    result = value
    patterns = (
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        r"\bsk-[A-Za-z0-9_-]{8,}",
        r"\bgh[opusr]_[A-Za-z0-9]{20,}",
        r"(?i)\b(api[_ -]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
    )
    for pattern in patterns:
        result = re.sub(pattern, "[REDACTED]", result)
    for name, secret in os.environ.items():
        upper = name.upper()
        if (
            len(secret) >= 8
            and any(
                marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )
            and secret in result
        ):
            result = result.replace(secret, "[REDACTED]")
    return result
