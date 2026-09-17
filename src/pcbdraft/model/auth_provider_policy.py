"""Provider credential timing and Nous endpoint policy helpers.

Authentication storage, OAuth/token flows, and provider routing remain in
``model.auth``. That legacy module re-exports these helpers and installs
late-bound adapters so its established constants and monkeypatch paths keep
working without a reverse import.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import ParseResult, urlparse

# Timestamp and network-policy parsing deliberately fail closed on malformed
# external values, matching the legacy authentication behavior.
# ruff: noqa: BLE001


_datetime_hook: Callable[[], Any] = lambda: datetime
_utc_hook: Callable[[], Any] = lambda: UTC
_parse_iso_timestamp_hook: Callable[[Any], float | None]
_now_hook: Callable[[], float] = time.time
_urlparse_hook: Callable[[str], ParseResult] = urlparse
_stale_portal_hosts_hook: Callable[[], frozenset[str]]
_default_portal_url_hook: Callable[[], str]
_allowed_inference_hosts_hook: Callable[[], frozenset[str]]
_logger_hook: Callable[[], Any]
_optional_base_url_hook: Callable[[Any], str | None]
_environment_getter_hook: Callable[[str], str | None]


def _configure_legacy_auth_hooks(
    *,
    datetime_type: Callable[[], Any],
    utc: Callable[[], Any],
    parse_iso_timestamp: Callable[[Any], float | None],
    now: Callable[[], float],
    parse_url: Callable[[str], ParseResult],
    stale_portal_hosts: Callable[[], frozenset[str]],
    default_portal_url: Callable[[], str],
    allowed_inference_hosts: Callable[[], frozenset[str]],
    logger: Callable[[], Any],
    optional_base_url: Callable[[Any], str | None],
    environment_getter: Callable[[str], str | None],
) -> None:
    """Install late-bound adapters for legacy ``model.auth`` globals."""
    global _datetime_hook
    global _utc_hook
    global _parse_iso_timestamp_hook
    global _now_hook
    global _urlparse_hook
    global _stale_portal_hosts_hook
    global _default_portal_url_hook
    global _allowed_inference_hosts_hook
    global _logger_hook
    global _optional_base_url_hook
    global _environment_getter_hook

    _datetime_hook = datetime_type
    _utc_hook = utc
    _parse_iso_timestamp_hook = parse_iso_timestamp
    _now_hook = now
    _urlparse_hook = parse_url
    _stale_portal_hosts_hook = stale_portal_hosts
    _default_portal_url_hook = default_portal_url
    _allowed_inference_hosts_hook = allowed_inference_hosts
    _logger_hook = logger
    _optional_base_url_hook = optional_base_url
    _environment_getter_hook = environment_getter


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _datetime_hook().fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_utc_hook())
    return parsed.timestamp()


def _is_expiring(expires_at_iso: Any, skew_seconds: int) -> bool:
    expires_epoch = _parse_iso_timestamp_hook(expires_at_iso)
    if expires_epoch is None:
        return True
    return expires_epoch <= (_now_hook() + skew_seconds)


def _coerce_ttl_seconds(expires_in: Any) -> int:
    try:
        ttl = int(expires_in)
    except Exception:
        ttl = 0
    return max(0, ttl)


def _optional_base_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().rstrip("/")
    return cleaned if cleaned else None


def _migrate_stale_nous_portal_url(providers: dict[str, Any]) -> None:
    nous = providers.get("nous")
    if not isinstance(nous, dict):
        return
    stored = (nous.get("portal_base_url") or "").strip()
    if stored:
        parsed = _urlparse_hook(stored)
        if parsed.hostname in _stale_portal_hosts_hook():
            default_url = _default_portal_url_hook()
            _logger_hook().warning(
                "auth: migrating stale nous portal_base_url %s -> %s",
                stored,
                default_url,
            )
            nous["portal_base_url"] = default_url


def _validate_nous_inference_url_from_network(url: str | None) -> str | None:
    """Validate a Portal-returned inference URL against the host allowlist.

    Returns the normalized URL for a well-formed HTTPS allowlisted host, or
    ``None`` for a missing, malformed, insecure, or unexpected URL. Environment
    overrides bypass this network-response policy because the operator controls
    those values directly.

    Co-authored-by: memosr <mehmet.sr35@gmail.com>
    """
    if not isinstance(url, str):
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    try:
        parsed = _urlparse_hook(cleaned)
    except Exception:
        return None
    if parsed.scheme != "https":
        _logger_hook().warning(
            "nous: refusing non-https inference URL scheme %r from Portal response",
            parsed.scheme,
        )
        return None
    if parsed.hostname not in _allowed_inference_hosts_hook():
        _logger_hook().warning(
            "nous: refusing inference URL host %r from Portal response "
            "(not in allowlist); falling back to default",
            parsed.hostname,
        )
        return None
    return cleaned.rstrip("/")


def _nous_inference_env_override() -> str | None:
    """Return the user-set Nous inference base URL override, if any.

    This is the documented development and staging escape hatch. It returns a
    trailing-slash-stripped non-empty string, or ``None`` when unset or blank.
    """
    return _optional_base_url_hook(_environment_getter_hook("NOUS_INFERENCE_BASE_URL"))


def _nous_portal_env_override() -> str | None:
    """Return the user/deployment-set Nous Portal base URL override, if any.

    ``PCBDRAFT_RUNTIME_PORTAL_BASE_URL`` takes precedence over the legacy
    ``NOUS_PORTAL_BASE_URL``. These trusted operator values intentionally bypass
    the allowlist used for untrusted network-provided values.
    """
    return _optional_base_url_hook(
        _environment_getter_hook("PCBDRAFT_RUNTIME_PORTAL_BASE_URL")
        or _environment_getter_hook("NOUS_PORTAL_BASE_URL")
    )
