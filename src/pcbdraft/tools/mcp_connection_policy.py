"""Connection-front policy for remote MCP transports.

This module owns deterministic URL and header validation, client certificate
resolution, redirect credential stripping, connection failure classification,
and user-facing connection error rendering.  It has no dependency on MCP
server tasks, SDK bootstrap, event loops, registration, or lifecycle state.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("pcbdraft.tools.mcp_tool")

_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"
    r"|sk-[A-Za-z0-9_]{1,255}"
    r"|Bearer\s+\S+"
    r"|token=[^\s&,;\"']{1,255}"
    r"|key=[^\s&,;\"']{1,255}"
    r"|API_KEY=[^\s&,;\"']{1,255}"
    r"|password=[^\s&,;\"']{1,255}"
    r"|secret=[^\s&,;\"']{1,255}"
    r")",
    re.IGNORECASE,
)


def _default_sanitize_error(text: str) -> str:
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def _default_is_auth_error(exc: BaseException) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (401, 403) or exc.__class__.__name__ in {
        "OAuthFlowError",
        "OAuthTokenError",
        "UnauthorizedError",
        "OAuthNonInteractiveError",
    }


class InvalidMcpUrlError(ValueError):
    """Raised when a remote MCP server's ``url`` cannot be parsed as http(s)://.

    Validated once at startup so we fail fast with a clear message instead of
    burning through the reconnect-backoff loop on every attempt.  (Ported from
    anomalyco/opencode#25019.)
    """


class NonMcpEndpointError(ConnectionError):
    """Raised when an HTTP MCP URL serves a non-MCP response.

    A genuine MCP Streamable-HTTP endpoint answers with ``application/json``
    or ``text/event-stream``.  Anything else on a 2xx response (typically
    ``text/html`` from a web-app root) means the configured ``url`` points at
    the wrong place.  This is non-retryable: every attempt returns the same
    page, so the reconnect-backoff loop is skipped and the server is reported
    failed immediately with an actionable message.

    Subclasses :class:`ConnectionError` so callers that only catch the broad
    class still treat it as a connection problem.
    """


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Extract the root-cause exception from anyio TaskGroup wrappers.

    The MCP SDK uses anyio task groups, which wrap errors in
    ``BaseExceptionGroup`` / ``ExceptionGroup``. Their ``str()`` is opaque —
    "unhandled errors in a TaskGroup (1 sub-exception)" — so log sites must
    unwrap to surface the real cause (e.g. ``BrokenPipeError`` on a dead
    stdio pipe, "401 Unauthorized" on an auth failure).

    Adapted from :func:`hermes_cli.mcp_config._unwrap_exception_group` with
    two extra behaviours needed on the runtime path:

    - **Fatal leaves re-raise.** A ``KeyboardInterrupt`` / ``SystemExit``
      anywhere in the (possibly nested) group must propagate to the
      interpreter, never be flattened into a loggable error.
    - **Prefer non-cancellation leaves.** When a group carries both a real
      error and the ``CancelledError``s that anyio cancellation sprays across
      sibling tasks, the real error is the root cause worth logging.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        fatal, _rest = exc.split((KeyboardInterrupt, SystemExit))
        if fatal is not None:
            # Surface the fatal signal itself, not the wrapper.
            leaf: BaseException = fatal
            while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
                leaf = leaf.exceptions[0]
            raise leaf
        # Prefer a non-cancellation leaf when one exists: cancellation
        # noise from sibling tasks should not mask the real error.
        chosen = exc.exceptions[0]
        for sub in exc.exceptions:
            if not _contains_only_cancellation(sub):
                chosen = sub
                break
        exc = chosen
    return exc


def _contains_only_cancellation(exc: BaseException) -> bool:
    """True if ``exc`` is (or a group containing only) CancelledError."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_contains_only_cancellation(sub) for sub in exc.exceptions)
    return isinstance(exc, asyncio.CancelledError)


def _classify_mcp_failure(
    exc: BaseException,
    *,
    is_auth_error: Callable[[BaseException], bool] | None = None,
) -> str:
    """Classify an MCP connection failure as ``'permanent'`` or ``'transient'``.

    Permanent failures are deterministic — every retry hits the same wall, so
    burning the retry ladder (and log lines) on them is pure noise; ``run()``
    parks them immediately:

    - auth failures (401/403) — need new credentials, not a retry;
    - :class:`NonMcpEndpointError` — the URL serves a web page, not MCP;
    - :class:`InvalidMcpUrlError` — unusable config;
    - ``FileNotFoundError`` / ``ENOENT`` — the stdio command doesn't exist.

    Everything else (network blips, EOF, ``ClosedResourceError``, transport
    TaskGroup drops, timeouts) is transient and keeps the normal
    retry-with-backoff ladder.
    """
    root = _unwrap_exception_group(exc)
    auth_error = is_auth_error or _default_is_auth_error
    if auth_error(root):
        return "permanent"
    if isinstance(root, (NonMcpEndpointError, InvalidMcpUrlError)):
        return "permanent"
    # Stdio command missing: FileNotFoundError, or an OSError carrying ENOENT.
    if isinstance(root, FileNotFoundError):
        return "permanent"
    if isinstance(root, OSError) and getattr(root, "errno", None) == errno.ENOENT:
        return "permanent"
    # httpx.HTTPStatusError with 401/403 that _is_auth_error's type-gate
    # missed (e.g. auth types not importable in this environment).
    status = getattr(getattr(root, "response", None), "status_code", None)
    if status in (401, 403):
        return "permanent"
    return "transient"


def _validate_remote_mcp_url(server_name: str, url: Any) -> str:
    """Return the URL as a string if it's a valid http(s) remote MCP URL.

    Raises :class:`InvalidMcpUrlError` otherwise with a message naming the
    offending server, so users can spot the bad entry in their config.

    Accepts:
    - ``http://host`` / ``https://host`` with optional port, path, query
    - IPv4, IPv6 (bracketed), DNS hostnames

    Rejects:
    - Non-string values (``None``, dicts, ints)
    - Missing scheme (``example.com/mcp``)
    - Non-http(s) schemes (``file://``, ``ws://``, ``stdio:`` — stdio servers
      use the ``command`` key, not ``url``)
    - Empty host (``http://``, ``https:///path``)
    """
    if not isinstance(url, str):
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': expected a string, got "
            f"{type(url).__name__}"
        )
    stripped = url.strip()
    if not stripped:
        raise InvalidMcpUrlError(f"Invalid MCP URL for '{server_name}': empty url")
    try:
        parsed = urlparse(stripped)
    except Exception as exc:  # urlparse is very permissive — belt and braces
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': {stripped!r} ({exc})"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': scheme must be http or "
            f"https, got {parsed.scheme!r} ({stripped!r})"
        )
    if not parsed.netloc:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing host ({stripped!r})"
        )
    # ``urlparse`` accepts ``http://:8080`` (empty host, explicit port).
    # Reject that — we need a real host.
    if not parsed.hostname:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing hostname ({stripped!r})"
        )
    return stripped


def _resolve_client_cert(server_name: str, config: dict):
    """Resolve the ``client_cert`` / ``client_key`` config for mTLS.

    Returns whatever ``httpx``'s ``cert=`` parameter accepts, or ``None`` when
    no client certificate is configured:

      - ``None`` if neither ``client_cert`` nor ``client_key`` is set.
      - A single absolute path string if ``client_cert`` is a string and
        ``client_key`` is unset (PEM file with cert + key combined).
      - A ``(cert_path, key_path)`` tuple when both are set, or when
        ``client_cert`` is a 2-element list/tuple.
      - A ``(cert_path, key_path, password)`` tuple when ``client_cert`` is
        a 3-element list/tuple — the third element is the key passphrase.

    User paths support ``~`` expansion. Missing files raise ``FileNotFoundError``
    with a server-scoped message so the failure surfaces as a clear setup
    error rather than an opaque TLS handshake error.
    """
    raw_cert = config.get("client_cert")
    raw_key = config.get("client_key")

    if raw_cert is None and raw_key is None:
        return None

    def _expand(path: Any, label: str) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"MCP server '{server_name}': {label} must be a non-empty "
                f"string path (got {type(path).__name__})"
            )
        expanded = os.path.expanduser(path.strip())
        if not os.path.isfile(expanded):
            raise FileNotFoundError(
                f"MCP server '{server_name}': {label} not found at {expanded!r}"
            )
        return expanded

    # Tuple/list form for client_cert — (cert, key) or (cert, key, password).
    if isinstance(raw_cert, (list, tuple)):
        if raw_key is not None:
            raise ValueError(
                f"MCP server '{server_name}': specify either client_cert as "
                f"a list [cert, key] OR client_cert + client_key, not both"
            )
        if len(raw_cert) == 2:
            cert_path = _expand(raw_cert[0], "client_cert[0]")
            key_path = _expand(raw_cert[1], "client_cert[1]")
            return (cert_path, key_path)
        if len(raw_cert) == 3:
            cert_path = _expand(raw_cert[0], "client_cert[0]")
            key_path = _expand(raw_cert[1], "client_cert[1]")
            password = raw_cert[2]
            if not isinstance(password, str):
                raise ValueError(
                    f"MCP server '{server_name}': client_cert[2] (key "
                    f"passphrase) must be a string"
                )
            return (cert_path, key_path, password)
        raise ValueError(
            f"MCP server '{server_name}': client_cert list form must have 2 "
            f"or 3 elements (got {len(raw_cert)})"
        )

    # String form for client_cert.
    cert_path = _expand(raw_cert, "client_cert")
    if raw_key is not None:
        key_path = _expand(raw_key, "client_key")
        return (cert_path, key_path)
    # Single combined PEM file (cert + key in one file).
    return cert_path


def _resolve_identity_header(server_name: str, config: dict):
    """Resolve the optional per-server ``identity_header`` config.

    Config shape (in the server's ``mcp_servers`` entry)::

        identity_header:
          name: "X-User-Id"
          value_from: "static"   # or "profile"; default: static
          value: "alice"         # required when value_from is static

    Returns a ``(header_name, header_value)`` tuple, or ``None`` when the
    key is unset or invalid. Invalid configs warn and are ignored — an
    identity header must never break the server connection. ``profile``
    mode resolves the value to the active Hermes profile name once at
    connect time; there is no per-call mutation.
    """
    raw = config.get("identity_header")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "MCP server '%s': identity_header must be a mapping with "
            "'name' and 'value'/'value_from' keys (got %s) — ignoring",
            server_name,
            type(raw).__name__,
        )
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning(
            "MCP server '%s': identity_header requires a non-empty 'name' — ignoring",
            server_name,
        )
        return None
    value_from = (raw.get("value_from") or "static").strip().lower()
    if value_from == "static":
        value = raw.get("value")
        if not isinstance(value, str) or not value.strip():
            logger.warning(
                "MCP server '%s': identity_header with value_from: static "
                "requires a non-empty string 'value' — ignoring",
                server_name,
            )
            return None
        return (name.strip(), value)
    if value_from == "profile":
        from pcbdraft.interfaces.tui.profiles import get_active_profile_name

        return (name.strip(), get_active_profile_name())
    logger.warning(
        "MCP server '%s': identity_header value_from must be 'static' or "
        "'profile' (got %r) — ignoring",
        server_name,
        value_from,
    )
    return None


def _apply_identity_header(
    server_name: str,
    config: dict,
    headers: dict,
    *,
    resolve_identity_header: Callable[[str, dict], tuple[str, str] | None]
    | None = None,
) -> dict:
    """Merge the resolved identity header into ``headers`` (in place).

    An explicit per-server ``headers`` entry with the same name (any
    casing) wins — the identity header never silently overrides user
    config.
    """
    resolver = resolve_identity_header or _resolve_identity_header
    resolved = resolver(server_name, config)
    if resolved is None:
        return headers
    name, value = resolved
    if any(key.lower() == name.lower() for key in headers):
        logger.debug(
            "MCP server '%s': identity_header '%s' already set via explicit "
            "headers config — keeping the explicit value",
            server_name,
            name,
        )
        return headers
    headers[name] = value
    return headers


def _make_redirect_header_stripper(
    original_url,
    *,
    strict: bool = False,
    configured_header_names: "set[str] | frozenset[str]" = frozenset(),
):
    """Build an httpx response hook that guards cross-origin redirects.

    Always strips ``Authorization`` when a redirect leaves the original
    origin. When *strict* is true (portable Agent Plugins v1 packages with
    ``strict_redirect_headers``), every *configured* header (lowercase names
    in *configured_header_names*) is stripped as well — the v1 spec forbids
    forwarding package-configured headers to a different origin without
    explicit user authorization.
    """

    async def _strip_on_cross_origin_redirect(response):
        if response.is_redirect and response.next_request:
            target = response.next_request.url
            if (target.scheme, target.host, target.port) != (
                original_url.scheme,
                original_url.host,
                original_url.port,
            ):
                response.next_request.headers.pop("authorization", None)
                response.next_request.headers.pop("Authorization", None)
                if strict:
                    for _name in configured_header_names:
                        while _name in response.next_request.headers:
                            del response.next_request.headers[_name]

    return _strip_on_cross_origin_redirect


def _format_connect_error(
    exc: BaseException,
    *,
    sanitize_error: Callable[[str], str] | None = None,
) -> str:
    """Render nested MCP connection errors into an actionable short message."""

    def _find_missing(current: BaseException) -> str | None:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                missing = _find_missing(child)
                if missing:
                    return missing
            return None
        if isinstance(current, FileNotFoundError):
            if getattr(current, "filename", None):
                return str(current.filename)
            match = re.search(r"No such file or directory: '([^']+)'", str(current))
            if match:
                return match.group(1)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                missing = _find_missing(nested_exc)
                if missing:
                    return missing
        return None

    def _flatten_messages(current: BaseException) -> list[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            flattened: list[str] = []
            for child in nested:
                flattened.extend(_flatten_messages(child))
            return flattened
        messages = []
        text = str(current).strip()
        if text:
            messages.append(text)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                messages.extend(_flatten_messages(nested_exc))
        return messages or [current.__class__.__name__]

    sanitizer = sanitize_error or _default_sanitize_error
    missing = _find_missing(exc)
    if missing:
        message = f"missing executable '{missing}'"
        if os.path.basename(missing) in {"npx", "npm", "node"}:
            message += (
                " (ensure Node.js is installed and PATH includes its bin directory, "
                "or set mcp_servers.<name>.command to an absolute path and include "
                "that directory in mcp_servers.<name>.env.PATH)"
            )
        return sanitizer(message)

    deduped: list[str] = []
    for item in _flatten_messages(exc):
        if item not in deduped:
            deduped.append(item)
    return sanitizer("; ".join(deduped[:3]))
