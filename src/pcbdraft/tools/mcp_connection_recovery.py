"""MCP connection cooldown, trust gating, and reconnect coordination.

The owning MCP module injects its process-wide state dictionaries, locks, and
legacy hooks into these helpers.  Server tasks, authentication retries,
configuration, handlers, and registration remain outside this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("pcbdraft.tools.mcp_tool")

_TRUST_FULL = "full"
_TRUST_UNTRUSTED = "untrusted"


def _record_connect_failure(
    server_name: str,
    *,
    failures: dict[str, int],
    retry_after: dict[str, float],
    base_backoff: float,
    max_backoff: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Stamp an exponential-backoff cooldown after a failed connection."""

    count = failures.get(server_name, 0) + 1
    failures[server_name] = count
    backoff = min(base_backoff * (2 ** (count - 1)), max_backoff)
    retry_after[server_name] = monotonic() + backoff


def _clear_connect_failure(
    server_name: str,
    *,
    failures: dict[str, int],
    retry_after: dict[str, float],
) -> None:
    """Clear connection cooldown state after a successful connection."""

    failures.pop(server_name, None)
    retry_after.pop(server_name, None)


def _connect_cooldown_active(
    server_name: str,
    *,
    retry_after: dict[str, float],
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Return whether a server is still inside its retry cooldown."""

    deadline = retry_after.get(server_name)
    return deadline is not None and monotonic() < deadline


def _normalize_server_trust(
    value: Any,
    *,
    warning: Callable[..., Any] = logger.warning,
    trust_full: str = _TRUST_FULL,
    trust_untrusted: str = _TRUST_UNTRUSTED,
) -> str:
    """Normalize a configured trust tier, failing closed on unknown values."""

    if value is None:
        return trust_full
    text = str(value).strip().lower()
    if text == trust_full:
        return trust_full
    if text == trust_untrusted:
        return trust_untrusted
    warning(
        "MCP trust: unrecognized trust value %r — treating as 'untrusted' "
        "(valid values: full, untrusted)",
        value,
    )
    return trust_untrusted


def _annotation_read_only_hint(mcp_tool: Any) -> bool:
    """Return true only for an explicit boolean ``readOnlyHint=True``."""

    annotations = getattr(mcp_tool, "annotations", None)
    if annotations is None:
        return False
    if isinstance(annotations, dict):
        hint = annotations.get("readOnlyHint")
    else:
        hint = getattr(annotations, "readOnlyHint", None)
    return hint is True


def _record_tool_trust_metadata(
    server_name: str,
    config: dict,
    tools: list[Any],
    *,
    lock: Any,
    server_trust_levels: dict[str, str],
    tool_read_only_hints: dict[str, dict[str, bool]],
    normalize_trust: Callable[[Any], str] = _normalize_server_trust,
    annotation_read_only_hint: Callable[[Any], bool] = _annotation_read_only_hint,
) -> None:
    """Capture per-server trust and per-tool read-only hints at discovery."""

    with lock:
        server_trust_levels[server_name] = normalize_trust((config or {}).get("trust"))
        hints = tool_read_only_hints.setdefault(server_name, {})
        for tool in tools:
            name = getattr(tool, "name", None)
            if name:
                hints[name] = annotation_read_only_hint(tool)


def _trust_gate_check(
    server_name: str,
    tool_name: str,
    *,
    server_trust_levels: dict[str, str],
    tool_read_only_hints: dict[str, dict[str, bool]],
    error_factory: Callable[[str], str],
    request_consent: Callable[..., str] | None = None,
    runtime_logger: logging.Logger = logger,
    trust_full: str = _TRUST_FULL,
    trust_untrusted: str = _TRUST_UNTRUSTED,
) -> str | None:
    """Gate write-capable tools on untrusted servers through approval."""

    trust = server_trust_levels.get(server_name, trust_full)
    if trust != trust_untrusted:
        return None
    if tool_read_only_hints.get(server_name, {}).get(tool_name) is True:
        return None

    try:
        if request_consent is None:
            from pcbdraft.tools.approval import request_elicitation_consent

            request_consent = request_elicitation_consent
        answer = request_consent(
            (
                f"MCP tool '{tool_name}' on UNTRUSTED server "
                f"'{server_name}' wants to run. This tool is write-capable "
                f"(no readOnlyHint=true annotation) and may modify external "
                f"state."
            ),
            (
                f"Server '{server_name}' is configured 'trust: untrusted'. "
                f"Approve to run '{tool_name}' once, or deny to block it."
            ),
            surface=f"mcp-trust/{server_name}",
        )
    except Exception as exc:
        runtime_logger.exception(
            "MCP trust gate: approval check failed for %s.%s: %s",
            server_name,
            tool_name,
            exc,  # noqa: TRY401 - preserve legacy log arguments
        )
        return error_factory(
            f"MCP tool '{tool_name}' on untrusted server '{server_name}' "
            f"was blocked: the approval system was unavailable "
            f"(fail-closed)."
        )

    if answer == "accept":
        return None
    runtime_logger.info(
        "MCP trust gate: user %s '%s' on untrusted server '%s'",
        "cancelled" if answer == "cancel" else "denied",
        tool_name,
        server_name,
    )
    return error_factory(
        f"The user did not approve running write-capable MCP tool "
        f"'{tool_name}' on untrusted server '{server_name}'. The command "
        f"was NOT run. Do not retry without explicit user direction."
    )


def _bump_server_error(
    server_name: str,
    *,
    error_counts: dict[str, int],
    breaker_opened_at: dict[str, float],
    threshold: int,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Increment a server's circuit-breaker count and open at threshold."""

    count = error_counts.get(server_name, 0) + 1
    error_counts[server_name] = count
    if count >= threshold:
        breaker_opened_at[server_name] = monotonic()


def _reset_server_error(
    server_name: str,
    *,
    error_counts: dict[str, int],
    breaker_opened_at: dict[str, float],
) -> None:
    """Close a server's circuit breaker after an unambiguous success."""

    error_counts[server_name] = 0
    breaker_opened_at.pop(server_name, None)


def _signal_reconnect(
    server: Any,
    *,
    loop: asyncio.AbstractEventLoop | None,
    asyncio_event_type: type = asyncio.Event,
) -> bool:
    """Ask a server task to rebuild its transport in a thread-safe way."""

    event = getattr(server, "_reconnect_event", None)
    if event is None:
        return False
    if isinstance(event, asyncio_event_type) and loop is not None and loop.is_running():
        loop.call_soon_threadsafe(event.set)
    else:
        event.set()
    return True


def reconnect_mcp_server(
    server_name: str,
    *,
    lock: Any,
    servers: dict[str, Any],
    signal_reconnect: Callable[[Any], bool],
) -> bool:
    """Ask a currently owned MCP server to rebuild after external re-auth."""

    with lock:
        server = servers.get(server_name)
    if server is None:
        return False
    return signal_reconnect(server)


def _wait_for_server_session_ready(
    server: Any,
    *,
    old_session: Any = None,
    timeout: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for a ready session that differs from an optional stale session."""

    poll_interval = 0.25
    iterations = max(1, int(max(float(timeout), 0.0) / poll_interval))
    for index in range(iterations):
        session = getattr(server, "session", None)
        ready = getattr(server, "_ready", None)
        is_ready = True
        if ready is not None and hasattr(ready, "is_set"):
            try:
                is_ready = bool(ready.is_set())
            except Exception:  # noqa: BLE001 - malformed readiness adapters fail open
                is_ready = True
        if session is not None and session is not old_session and is_ready:
            return True
        if index < iterations - 1:
            sleep(poll_interval)
    return False


def _signal_reconnect_and_wait(
    server_name: str,
    server: Any,
    *,
    op_description: str,
    loop: asyncio.AbstractEventLoop | None,
    wait_for_session_ready: Callable[..., bool] = _wait_for_server_session_ready,
    timeout: float = 15.0,
    runtime_logger: logging.Logger = logger,
) -> bool:
    """Request transport reconstruction and wait for a fresh ready session."""

    if loop is None or not loop.is_running():
        return False

    old_session = getattr(server, "session", None)

    def _request_reconnect() -> None:
        ready = getattr(server, "_ready", None)
        if ready is not None and hasattr(ready, "clear"):
            ready.clear()
        reconnect_event = getattr(server, "_reconnect_event", None)
        if reconnect_event is not None and hasattr(reconnect_event, "set"):
            reconnect_event.set()

    runtime_logger.info(
        "MCP server '%s': %s requesting transport reconnect",
        server_name,
        op_description,
    )
    loop.call_soon_threadsafe(_request_reconnect)
    return wait_for_session_ready(
        server,
        old_session=old_session,
        timeout=timeout,
    )
