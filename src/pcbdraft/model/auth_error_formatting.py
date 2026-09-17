"""Structured authentication errors and user-facing formatting.

This module owns no credential, token, OAuth, or provider-routing state. The
legacy :mod:`pcbdraft.model.auth` module re-exports these symbols and installs
late-bound compatibility hooks so its historical monkeypatch paths continue
to affect formatting without creating a reverse import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Nous account enrichment is best-effort. Formatting an auth failure must
# still return useful guidance when the optional account lookup raises.
# ruff: noqa: BLE001, S110


_DEFAULT_CODEX_RATE_LIMITED_CODE = "codex_rate_limited"


class AuthError(RuntimeError):
    """Structured auth error with UX mapping hints."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: str | None = None,
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required


_auth_error_type: Callable[[], type[BaseException]] = lambda: AuthError
_rate_limited_code: Callable[[], str] = lambda: _DEFAULT_CODEX_RATE_LIMITED_CODE
_rate_limit_classifier: Callable[[], Callable[[Exception], bool]]
_nous_entitlement_formatter: Callable[[], Callable[[AuthError], str]]


def _configure_legacy_auth_hooks(
    *,
    auth_error_type: Callable[[], type[BaseException]],
    rate_limited_code: Callable[[], str],
    rate_limit_classifier: Callable[[], Callable[[Exception], bool]],
    nous_entitlement_formatter: Callable[[], Callable[[AuthError], str]],
) -> None:
    """Install late-bound accessors for legacy ``model.auth`` globals."""
    global _auth_error_type
    global _rate_limited_code
    global _rate_limit_classifier
    global _nous_entitlement_formatter

    _auth_error_type = auth_error_type
    _rate_limited_code = rate_limited_code
    _rate_limit_classifier = rate_limit_classifier
    _nous_entitlement_formatter = nous_entitlement_formatter


def is_rate_limited_auth_error(error: Exception) -> bool:
    """True when an :class:`AuthError` represents upstream rate-limiting / quota
    exhaustion rather than missing or invalid credentials.

    These failures are transient — re-authenticating cannot resolve them — so
    callers should surface a "retry later" notice and prefer a fallback chain
    instead of prompting the operator to run ``hermes auth``.
    """
    return (
        isinstance(error, _auth_error_type())
        and not error.relogin_required
        and error.code == _rate_limited_code()
    )


def _parse_retry_after_seconds(headers: Any) -> int | None:
    """Best-effort parse of a ``Retry-After`` header into whole seconds.

    Thin wrapper around :func:`agent.retry_utils.parse_retry_after_seconds`
    (delta-seconds and HTTP-date forms; negatives clamp to 0; missing or
    unparseable values return ``None``).
    """
    from pcbdraft.agent.retry_utils import parse_retry_after_seconds

    seconds = parse_retry_after_seconds(headers)
    return None if seconds is None else int(seconds)


def format_auth_error(error: Exception) -> str:
    """Map auth failures to concise user-facing guidance."""
    if not isinstance(error, _auth_error_type()):
        return str(error)

    # Rate-limit / quota errors are not credential problems — never append the
    # "re-authenticate" remediation, which would mislead the operator.
    if _rate_limit_classifier()(error):
        return str(error)

    if error.relogin_required:
        return f"{error} Run `pcbdraft connect` to re-authenticate."

    if error.code == "subscription_required":
        if error.provider == "nous":
            return _nous_entitlement_formatter()(error)
        return "No active paid subscription found. Please purchase/activate a subscription, then retry."

    if error.code == "insufficient_credits":
        if error.provider == "nous":
            return _nous_entitlement_formatter()(error)
        return "Subscription credits are exhausted. Top up/renew credits, then retry."

    if (
        error.code
        in {
            "subscription_expired",
            "no_usable_credits",
            "account_missing",
            "member_spend_cap_exceeded",
        }
        and error.provider == "nous"
    ):
        return _nous_entitlement_formatter()(error)

    if error.code == "temporarily_unavailable":
        return f"{error} Please retry in a few seconds."

    return str(error)


def _format_nous_entitlement_auth_error(error: AuthError) -> str:
    try:
        from pcbdraft.interfaces.tui.nous_account import (
            format_nous_portal_entitlement_message,
            get_nous_portal_account_info,
        )

        account_info = get_nous_portal_account_info(force_fresh=True)
        message = format_nous_portal_entitlement_message(
            account_info,
            capability="Nous model access",
        )
        if message:
            return message
    except Exception:
        pass
    return f"{error} Check credits or billing in Nous Portal, then retry."


_rate_limit_classifier = lambda: is_rate_limited_auth_error
_nous_entitlement_formatter = lambda: _format_nous_entitlement_auth_error
