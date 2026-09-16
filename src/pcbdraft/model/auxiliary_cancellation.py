"""Cancellation and progress policy for synchronous auxiliary model calls.

The module is deliberately provider-agnostic.  It owns only request-scoped
interrupt protection, explicit-cancellation arbitration, progress hooks, and
the isolated worker used by protected synchronous provider attempts.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
from collections.abc import Callable
from typing import Any

# Preserve the established logger path for callers and tests that capture it.
logger = logging.getLogger("pcbdraft.model.auxiliary_client")


# ── Interrupt protection for atomic auxiliary tasks ──────────────────────
# Some auxiliary tasks must NOT be aborted mid-flight by a gateway interrupt
# (e.g. an incoming user message while the agent is busy). Context
# compression is the prime case: if the summary LLM call is interrupted
# part-way, compression falls back to a static "summary unavailable" marker
# and the real handoff is lost (#23975). A thread-local flag lets such a
# task mark its in-flight LLM call as interrupt-protected; the Codex
# Responses stream's cancellation check honors it. An explicit host cancel
# (CLI Ctrl+C or /stop) may install a cancel check that overrides protection;
# ordinary incoming-message interrupts remain protected. TIMEOUTS still fire
# (a hung call must die), and all OTHER aux tasks (vision, web_extract,
# title_generation, …) remain freely interruptible.
_aux_interrupt_protection = threading.local()


class AuxiliaryExplicitCancellation(BaseException):
    """Frozen signal that an auxiliary attempt was explicitly hard-cancelled.

    This deliberately follows ``asyncio.CancelledError`` and inherits directly
    from ``BaseException``: provider retry/fallback code catches ``Exception``
    broadly and must never reinterpret an explicit host stop as a transport
    failure. ``cause`` is immutable class data so downstream compression code
    does not re-query a mutable host Event after the transport has unwound.
    """

    cause = "explicit_host_cancel"

    def __init__(self) -> None:
        super().__init__("auxiliary request explicitly cancelled by host")


def _aux_interrupt_protected() -> bool:
    return bool(getattr(_aux_interrupt_protection, "active", False))


def _aux_interrupt_cancel_requested() -> bool:
    """Return whether an explicit host cancel overrides aux protection."""
    event = getattr(_aux_interrupt_protection, "cancel_event", None)
    if event is not None:
        try:
            return bool(event.is_set())
        except Exception:
            logger.debug("aux interrupt cancel event check failed", exc_info=True)
            return False
    check = getattr(_aux_interrupt_protection, "cancel_check", None)
    if not callable(check):
        return False
    try:
        return bool(check())
    except Exception:
        logger.debug("aux interrupt cancel check failed", exc_info=True)
        return False


@contextlib.contextmanager
def aux_interrupt_protection(
    active: bool = True,
    cancel_check=None,
    cancel_event=None,
):
    """Mark the current thread's auxiliary LLM call as interrupt-protected.

    Used by atomic aux tasks (compression) so a mid-flight gateway interrupt
    doesn't abort the call and trigger a degraded fallback. Re-entrant-safe:
    restores the previous value on exit. ``cancel_check`` lets the host retain
    an explicit hard-cancel path; ``cancel_event`` is preferred when the host
    already owns an Event. Nested protection scopes inherit both values.
    """
    prev = getattr(_aux_interrupt_protection, "active", False)
    prev_cancel_check = getattr(_aux_interrupt_protection, "cancel_check", None)
    prev_cancel_event = getattr(_aux_interrupt_protection, "cancel_event", None)
    _aux_interrupt_protection.active = active
    if callable(cancel_check):
        _aux_interrupt_protection.cancel_check = cancel_check
    if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)):
        _aux_interrupt_protection.cancel_event = cancel_event
    try:
        yield
    finally:
        _aux_interrupt_protection.active = prev
        _aux_interrupt_protection.cancel_check = prev_cancel_check
        _aux_interrupt_protection.cancel_event = prev_cancel_event


def _capture_aux_cancel_check() -> Callable[[], Any] | None:
    """Capture the current explicit-cancel source on the owning request thread."""
    event = getattr(_aux_interrupt_protection, "cancel_event", None)
    is_set = getattr(event, "is_set", None)
    if callable(is_set):
        return is_set
    check = getattr(_aux_interrupt_protection, "cancel_check", None)
    if callable(check):
        # Preserve callable identity so attempt-local decision objects retain
        # methods such as begin_timeout_cleanup() when captured by adapters.
        return check
    return None


def _captured_aux_cancel_requested(cancel_check: Callable[[], Any]) -> bool:
    """Read a request-thread cancellation source without leaking its failures."""
    try:
        return bool(cancel_check())
    except Exception:
        logger.debug("captured aux cancel check failed", exc_info=True)
        return False


class _AuxiliaryCancellationDecision:
    """Atomically choose explicit cancellation or provider timeout per attempt."""

    def __init__(self, source_cancel_check: Callable[[], Any]) -> None:
        self._source_cancel_check = source_cancel_check
        self._lock = threading.Lock()
        self._outcome = "active"

    def __call__(self) -> bool:
        with self._lock:
            if self._outcome == "cancelled":
                return True
            if self._outcome == "timed_out":
                return False
            if _captured_aux_cancel_requested(self._source_cancel_check):
                self._outcome = "cancelled"
                return True
            return False

    def begin_timeout_cleanup(self) -> bool:
        """Return whether timeout won and destructive cleanup is permitted."""
        with self._lock:
            if self._outcome == "active":
                if _captured_aux_cancel_requested(self._source_cancel_check):
                    self._outcome = "cancelled"
                else:
                    self._outcome = "timed_out"
            return self._outcome == "timed_out"


# ── Forward-progress hook for streamed auxiliary calls ───────────────────
# Long auxiliary calls (context compression is the prime case) are watched by
# wall-clock deadlines in their hosts (gateway session hygiene). A fixed
# deadline punishes SLOW summary models exactly as hard as HUNG ones: a
# reasoning model happily streaming a large summary is killed mid-generation.
# This thread-local hook lets the host observe liveness instead: the wire
# consumers below tick it on every streamed token/SSE event, and the host
# extends its deadline while tokens are moving (see gateway/run.py session
# hygiene + CompressionCommitFence.touch_progress). Thread-local matches the
# call topology — the aux call and its stream consumption run synchronously
# on the thread that installed the hook.
_aux_progress = threading.local()


def _notify_aux_progress() -> None:
    """Tick the installed forward-progress hook, if any. Never raises."""
    hook = getattr(_aux_progress, "hook", None)
    if hook is None:
        return
    try:
        hook()
    except Exception:
        logger.debug("aux progress hook failed", exc_info=True)


def _aux_progress_active() -> bool:
    return getattr(_aux_progress, "hook", None) is not None


@contextlib.contextmanager
def aux_progress_hook(hook):
    """Install *hook* as the current thread's aux forward-progress callback.

    ``hook=None`` is a no-op passthrough so callers can wire it
    unconditionally. Re-entrant-safe: restores the previous hook on exit.
    """
    prev = getattr(_aux_progress, "hook", None)
    _aux_progress.hook = hook if callable(hook) else prev
    try:
        yield
    finally:
        _aux_progress.hook = prev


def _run_protected_sync_provider_call(
    callback: Callable[[dict[str, Any]], Any],
    kwargs: dict[str, Any],
) -> Any:
    """Run one protected provider callback in an attempt-isolated daemon.

    A hard cancel must release the compression-owning thread promptly, but
    auxiliary clients are process-shared and cannot safely be closed or evicted
    to wake one request.  Only protected calls with a captured hard-cancel source
    use this seam.  Their provider callback (including stream aggregation) runs
    in a daemon worker while the owner polls cancellation.  On cancel the owner
    unwinds immediately; the worker is left to finish under the provider timeout
    already present in ``kwargs``.  It owns no transcript or compressor commit
    state and never holds the session lock.

    Ordinary auxiliary calls, and protected calls without a cancellation source,
    retain the historical direct synchronous path with no extra thread.
    """
    source_cancel_check = _capture_aux_cancel_check()
    if not _aux_interrupt_protected() or not callable(source_cancel_check):
        return callback(kwargs)

    # Freeze one linearized outcome for this isolated attempt. The host Event is
    # reused and cleared on a later turn, while the Codex timeout Timer may race
    # owner polling. Both paths must decide under the same attempt-local lock.
    cancel_check = _AuxiliaryCancellationDecision(source_cancel_check)

    if cancel_check():
        raise AuxiliaryExplicitCancellation()

    progress_hook = getattr(_aux_progress, "hook", None)
    provider_context = contextvars.copy_context()
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _provider_worker() -> None:
        try:
            with (
                aux_progress_hook(progress_hook),
                aux_interrupt_protection(cancel_check=cancel_check),
            ):
                outcome["result"] = callback(kwargs)
        except BaseException as exc:
            outcome["exception"] = exc
        finally:
            done.set()

    threading.Thread(
        target=provider_context.run,
        args=(_provider_worker,),
        name="pcbdraft-protected-aux-provider",
        daemon=True,
    ).start()

    while True:
        # Cancellation is checked before and after every completion wait so it
        # wins whenever result publication and the host Event become visible in
        # the same polling interval.
        if _captured_aux_cancel_requested(cancel_check):
            raise AuxiliaryExplicitCancellation()
        if not done.wait(0.02):
            continue
        if _captured_aux_cancel_requested(cancel_check):
            raise AuxiliaryExplicitCancellation()
        exception = outcome.get("exception")
        if exception is not None:
            raise exception
        return outcome.get("result")
