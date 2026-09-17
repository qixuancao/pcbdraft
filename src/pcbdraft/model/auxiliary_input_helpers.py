"""Pure defensive input helpers for the auxiliary client coordinator.

These helpers validate type-like inputs and split URL query defaults without
reading runtime state or owning provider, request, credential, or cache policy.
This module never imports :mod:`pcbdraft.model.auxiliary_client`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse


def _safe_isinstance(obj: Any, maybe_type: Any) -> bool:
    """Return False instead of raising when a patched symbol is not a type."""
    try:
        return isinstance(obj, maybe_type)
    except TypeError:
        return False


def _extract_url_query_params(url: str):
    """Extract query params from URL, return (clean_url, default_query dict or None)."""
    parsed = urlparse(url)
    if parsed.query:
        clean = urlunparse(parsed._replace(query=""))
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return clean, params
    return url, None
