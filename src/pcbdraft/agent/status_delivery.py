"""Terminal status output, buffering, and stream diagnostics for agents."""

# Status callbacks and diagnostic collection are deliberately best-effort.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


class StatusDeliveryMixin:
    """Deliver agent status updates without coupling them to the core loop."""

    def _safe_print(self, *args, **kwargs):
        """Print that silently handles broken pipes / closed stdout.

        In headless environments (systemd, Docker, nohup) stdout may become
        unavailable mid-session.  A raw ``print()`` raises ``OSError`` which
        can crash cron jobs and lose completed work.

        Internally routes through ``self._print_fn`` (default: builtin
        ``print``) so callers such as the CLI can inject a renderer that
        handles ANSI escape sequences properly (e.g. prompt_toolkit's
        ``print_formatted_text(ANSI(...))``) without touching this method.
        """
        try:
            fn = self._print_fn or print
            fn(*args, **kwargs)
        except (OSError, ValueError):
            pass

    def _vprint(self, *args, force: bool = False, **kwargs):
        """Verbose print — suppressed when actively streaming tokens.

        Pass ``force=True`` for error/warning messages that should always be
        shown even during streaming playback (TTS or display).

        During tool execution (``_executing_tools`` is True), printing is
        allowed even with stream consumers registered because no tokens
        are being streamed at that point.

        After the main response has been delivered and the remaining tool
        calls are post-response housekeeping (``_mute_post_response``),
        all non-forced output is suppressed.

        ``suppress_status_output`` is a stricter CLI automation mode used by
        parseable single-query flows such as ``hermes chat -q``. In that mode,
        all status/diagnostic prints routed through ``_vprint`` are suppressed
        so stdout stays machine-readable.
        """
        if getattr(self, "suppress_status_output", False):
            return
        if not force and getattr(self, "_mute_post_response", False):
            return
        if not force and self._has_stream_consumers() and not self._executing_tools:
            return
        self._safe_print(*args, **kwargs)

    def _should_start_quiet_spinner(self) -> bool:
        """Return True when quiet-mode spinner output has a safe sink.

        In headless/stdio-protocol environments, a raw spinner with no custom
        ``_print_fn`` falls back to ``sys.stdout`` and can corrupt protocol
        streams such as ACP JSON-RPC. Allow quiet spinners only when either:
        - output is explicitly rerouted via ``_print_fn``; or
        - stdout is a real TTY.
        """
        if self._print_fn is not None:
            return True
        stream = getattr(sys, "stdout", None)
        if stream is None:
            return False
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    def _should_emit_quiet_tool_messages(self) -> bool:
        """Return True when quiet-mode tool summaries should print directly.

        Quiet mode is used by both the interactive CLI and embedded/library
        callers. The CLI may still want compact progress hints when no callback
        owns rendering. Embedded/library callers, on the other hand, expect
        quiet mode to be truly silent.
        """
        return (
            self.quiet_mode
            and not self.tool_progress_callback
            and getattr(self, "platform", "") == "cli"
        )

    def _emit_status(self, message: str) -> None:
        """Emit a lifecycle status message to both CLI and gateway channels.

        CLI users see the message via ``_vprint(force=True)`` so it is always
        visible regardless of verbose/quiet mode.  Gateway consumers receive
        it through ``status_callback("lifecycle", ...)``.

        This helper never raises — exceptions are swallowed so it cannot
        interrupt the retry/fallback logic.
        """
        try:
            self._vprint(f"{self.log_prefix}{message}", force=True)
        except Exception:
            pass
        if self.status_callback:
            try:
                self.status_callback("lifecycle", message)
            except Exception:
                logger.debug("status_callback error in _emit_status", exc_info=True)

    def _emit_warning(self, message: str) -> None:
        """Emit a user-visible warning through the same status plumbing.

        Unlike debug logs, these warnings are meant for degraded side paths
        such as auxiliary compression or memory flushes where the main turn can
        continue but the user needs to know something important failed.
        """
        try:
            self._vprint(f"{self.log_prefix}{message}", force=True)
        except Exception:
            pass
        if self.status_callback:
            try:
                self.status_callback("warn", message)
            except Exception:
                logger.debug("status_callback error in _emit_warning", exc_info=True)

    def _emit_notice(self, notice) -> None:
        """Fire a structured ``AgentNotice`` to the active driver (TUI / CLI).

        Driver-agnostic: the bound ``notice_callback`` renders it however that
        driver does (TUI status-bar override, CLI console line). Swallows all
        callback errors — a notice must NEVER break the agent loop (D-D fail-open).
        """
        if self.notice_callback:
            try:
                self.notice_callback(notice)
            except Exception:
                logger.debug("notice_callback error in _emit_notice", exc_info=True)

    def _emit_notice_clear(self, key: str) -> None:
        """Clear a previously-fired sticky notice by ``key`` (e.g. on recovery)."""
        if self.notice_clear_callback:
            try:
                self.notice_clear_callback(key)
            except Exception:
                logger.debug(
                    "notice_clear_callback error in _emit_notice_clear", exc_info=True
                )

    def _emit_wait_notice(self, text: str) -> None:
        """Surface a live wait-state explanation on every driver.

        Long provider waits (slow/overloaded backend, no first byte, reasoning
        model thinking for minutes) used to leave the user staring at a generic
        "cogitating..." spinner with no hint of what the agent was waiting on.
        This helper rewrites the live status line with an explanation:

        - CLI: ``thinking_callback`` updates the prompt_toolkit spinner text.
        - TUI / Desktop: the same callback is bridged to the ``thinking.delta``
          event, which both render as the live spinner/status line.
        - Gateway: ``_touch_activity`` stores the text as the activity
          description, which the "⏳ Working — N min" heartbeat includes.

        Never raises — a wait notice must not break the API-call wait loop.
        """
        self._touch_activity(text)
        _thinking_cb = getattr(self, "thinking_callback", None)
        if _thinking_cb:
            try:
                _thinking_cb(text)
            except Exception:
                logger.debug(
                    "thinking_callback error in _emit_wait_notice", exc_info=True
                )

    # Retry and fallback messages stay buffered unless every recovery path fails.
    def _buffer_status(self, message: str) -> None:
        """Buffer a retry/fallback status message.

        Stored as a (kind, text) tuple where ``kind`` is one of:
        - ``"status"``  -> replays via ``_emit_status``
        - ``"vprint"``  -> replays via ``_vprint(force=True)``
        - ``"warn"``    -> replays via ``_emit_warning``
        Used to defer noisy retry chatter until we know whether the
        turn ultimately recovered or failed.
        """
        try:
            buf = getattr(self, "_retry_status_buffer", None)
            if buf is None:
                buf = []
                self._retry_status_buffer = buf
            buf.append(("status", message))
        except Exception:
            # Never break the retry loop on a buffer hiccup.
            pass

    def _buffer_vprint(self, message: str) -> None:
        """Buffer a vprint(force=True) retry/fallback line."""
        try:
            buf = getattr(self, "_retry_status_buffer", None)
            if buf is None:
                buf = []
                self._retry_status_buffer = buf
            buf.append(("vprint", message))
        except Exception:
            pass

    def _clear_status_buffer(self) -> None:
        """Drop buffered retry messages — call on successful recovery."""
        try:
            buf = getattr(self, "_retry_status_buffer", None)
            if buf:
                buf.clear()
        except Exception:
            pass

    def _emit_pending_fallback_notice(self) -> None:
        """Surface the one-shot fallback-switch notice on successful recovery.

        A provider/model switch is a durable state change operators must see,
        unlike transient retry chatter that ``_clear_status_buffer`` drops.
        ``try_activate_fallback`` records the switch in
        ``self._pending_fallback_notice``; this emits it exactly once via
        ``_emit_status`` and then clears it, so a successful fallback still
        produces one visible notice.  On terminal failure the buffered switch
        line is flushed instead (and this notice discarded) — see
        ``_flush_status_buffer`` — so the user always sees the switch once.
        """
        try:
            notice = getattr(self, "_pending_fallback_notice", None)
            if notice:
                # Clear before emitting so a (swallowed) callback error can't
                # leave the notice set for a stale re-emit on a later turn.
                self._pending_fallback_notice = None
                self._emit_status(notice)
        except Exception:
            # Never break the conversation loop on a notice hiccup.
            pass

    def _flush_status_buffer(self) -> None:
        """Emit buffered retry messages — call on terminal failure.

        Surfaces the full retry/fallback trace so the user can see what
        was tried before the turn gave up.
        """
        try:
            # The buffered trace already carries the fallback switch line, so
            # drop any one-shot fallback notice to avoid a stale duplicate
            # leaking into a later successful turn.
            self._pending_fallback_notice = None
            buf = getattr(self, "_retry_status_buffer", None)
            if not buf:
                return
            # Drain first so a callback exception doesn't double-emit.
            messages = list(buf)
            buf.clear()
            for kind, msg in messages:
                try:
                    if kind == "status":
                        self._emit_status(msg)
                    elif kind == "warn":
                        self._emit_warning(msg)
                    else:
                        self._vprint(f"{self.log_prefix}{msg}", force=True)
                except Exception:
                    pass
        except Exception:
            pass

    # Stream-diagnostic class header preserved for backward compat —
    # actual list lives in ``agent.stream_diag.STREAM_DIAG_HEADERS``.
    from pcbdraft.agent.stream_diag import (
        STREAM_DIAG_HEADERS as _STREAM_DIAG_HEADERS,
    )

    @staticmethod
    def _stream_diag_init() -> dict[str, Any]:
        """Forwarder — see ``agent.stream_diag.stream_diag_init``."""
        from pcbdraft.agent.stream_diag import stream_diag_init

        return stream_diag_init()

    def _stream_diag_capture_response(
        self, diag: dict[str, Any], http_response: Any
    ) -> None:
        """Forwarder — see ``agent.stream_diag.stream_diag_capture_response``."""
        from pcbdraft.agent.stream_diag import stream_diag_capture_response

        stream_diag_capture_response(self, diag, http_response)

    @staticmethod
    def _flatten_exception_chain(error: BaseException) -> str:
        """Forwarder — see ``agent.stream_diag.flatten_exception_chain``."""
        from pcbdraft.agent.stream_diag import flatten_exception_chain

        return flatten_exception_chain(error)

    def _is_provider_stream_parse_error(self, error: BaseException) -> bool:
        """Return True for malformed provider streaming data from SDK parsers.

        Some Anthropic-compatible streaming providers can send a malformed
        event-stream frame.  The Anthropic SDK surfaces that as a plain
        ``ValueError`` such as ``expected ident at line 1 column 149``.  That
        is provider wire-format trouble, not local request validation, so it
        should follow the same retry path as a truncated JSON body.
        """
        if getattr(self, "api_mode", None) != "anthropic_messages":
            return False
        if not isinstance(error, ValueError):
            return False
        if isinstance(error, (UnicodeEncodeError, json.JSONDecodeError)):
            return False
        message = str(error).strip().lower()
        return "expected ident at line" in message

    def _log_stream_retry(
        self,
        *,
        kind: str,
        error: BaseException,
        attempt: int,
        max_attempts: int,
        mid_tool_call: bool,
        diag: dict[str, Any] | None = None,
    ) -> None:
        """Forwarder — see ``agent.stream_diag.log_stream_retry``."""
        from pcbdraft.agent.stream_diag import log_stream_retry

        log_stream_retry(
            self,
            kind=kind,
            error=error,
            attempt=attempt,
            max_attempts=max_attempts,
            mid_tool_call=mid_tool_call,
            diag=diag,
        )

    def _emit_stream_drop(
        self,
        *,
        error: BaseException,
        attempt: int,
        max_attempts: int,
        mid_tool_call: bool,
        diag: dict[str, Any] | None = None,
    ) -> None:
        """Forwarder — see ``agent.stream_diag.emit_stream_drop``."""
        from pcbdraft.agent.stream_diag import emit_stream_drop

        emit_stream_drop(
            self,
            error=error,
            attempt=attempt,
            max_attempts=max_attempts,
            mid_tool_call=mid_tool_call,
            diag=diag,
        )

    def _emit_auxiliary_failure(self, task: str, exc: BaseException) -> None:
        """Surface a compact warning for failed auxiliary work."""
        try:
            detail = self._summarize_api_error(exc)
        except Exception:
            detail = str(exc)
        detail = (detail or exc.__class__.__name__).strip()
        if len(detail) > 220:
            detail = detail[:217].rstrip() + "..."
        self._emit_warning(f"⚠ Auxiliary {task} failed: {detail}")
