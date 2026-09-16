"""Signal and event-loop shutdown handling for the native terminal UI."""

# Signal and teardown paths must absorb failures so shutdown can continue.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, TextIO

from pcbdraft.agent.interrupt_compat import request_hard_interrupt


class SignalModule(Protocol):
    """Small signal-module surface used by the installers and their tests."""

    SIGINT: int
    SIGTERM: int

    def signal(self, signalnum: int, handler: Callable[..., Any]) -> Any: ...


def _interrupt_grace_seconds(environ: Mapping[str, str]) -> float:
    try:
        return float(environ.get("PCBDRAFT_RUNTIME_SIGTERM_GRACE", "1.5"))
    except (TypeError, ValueError):
        return 1.5


def _interrupt_agent(
    cli: Any,
    signum: int,
    *,
    require_running: bool,
    environ: Mapping[str, str],
    sleep: Callable[[float], None],
    hard_interrupt: Callable[..., Any],
) -> None:
    """Interrupt the active agent and allow its tool process to unwind."""
    try:
        agent = getattr(cli, "agent", None)
        if agent is None or (
            require_running and not getattr(cli, "_agent_running", False)
        ):
            return
        hard_interrupt(agent, f"received signal {signum}")
        grace = _interrupt_grace_seconds(environ)
        if grace > 0:
            sleep(grace)
    except Exception:
        pass


def make_interactive_signal_handler(
    cli: Any,
    *,
    arm_exit_watchdog: Callable[[], None],
    logger: logging.Logger,
    environ: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
    hard_interrupt: Callable[..., Any] = request_hard_interrupt,
    get_app: Callable[[], Any] | None = None,
) -> Callable[[int, Any], None]:
    """Build the SIGTERM/SIGHUP handler used by interactive sessions."""

    def handler(signum: int, _frame: Any) -> None:
        try:
            logger.debug("Received signal %s, triggering graceful shutdown", signum)
        except Exception:
            # Logging is not reentrant-safe and must never break signal handling.
            pass

        arm_exit_watchdog()
        _interrupt_agent(
            cli,
            signum,
            require_running=True,
            environ=environ,
            sleep=sleep,
            hard_interrupt=hard_interrupt,
        )

        try:
            app_getter = get_app
            if app_getter is None:
                from prompt_toolkit.application.current import get_app_or_none

                app_getter = get_app_or_none
            app = app_getter()
            if app is not None:
                loop = getattr(app, "loop", None)
                if loop is not None:
                    loop.call_soon_threadsafe(app.exit)
                    return
        except Exception:
            pass
        raise KeyboardInterrupt()

    return handler


def install_interactive_signal_handlers(
    cli: Any,
    *,
    arm_exit_watchdog: Callable[[], None],
    logger: logging.Logger,
    signal_module: SignalModule = signal,
    platform: str = sys.platform,
) -> Callable[[int, Any], None]:
    """Install graceful interactive termination handlers and return the handler."""
    handler = make_interactive_signal_handler(
        cli,
        arm_exit_watchdog=arm_exit_watchdog,
        logger=logger,
    )
    try:
        signal_module.signal(signal_module.SIGTERM, handler)
        sighup = getattr(signal_module, "SIGHUP", None)
        if sighup is not None:
            signal_module.signal(sighup, handler)
        if platform == "win32":

            def absorb_sigint(_signum: int, _frame: Any) -> None:
                return None

            signal_module.signal(signal_module.SIGINT, absorb_sigint)
    except Exception:
        pass
    return handler


def make_single_query_signal_handler(
    cli: Any,
    *,
    arm_exit_watchdog: Callable[[], None],
    flush_session_store: Callable[[Any], None],
    logger: logging.Logger,
    environ: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
    hard_interrupt: Callable[..., Any] = request_hard_interrupt,
    signal_module: SignalModule = signal,
    exit_process: Callable[[int], Any] = os._exit,
    streams: Sequence[TextIO | None] | None = None,
    shutdown_logging: Callable[[], None] = logging.shutdown,
) -> Callable[[int, Any], None]:
    """Build the handler for one-shot CLI sessions and kanban workers."""

    def handler(signum: int, _frame: Any) -> None:
        logger.debug("Received signal %s in single-query mode", signum)
        arm_exit_watchdog()
        _interrupt_agent(
            cli,
            signum,
            require_running=False,
            environ=environ,
            sleep=sleep,
            hard_interrupt=hard_interrupt,
        )
        if environ.get("PCBDRAFT_RUNTIME_KANBAN_TASK"):
            try:
                sigalrm = getattr(signal_module, "SIGALRM", None)
                if sigalrm is not None:
                    signal_module.signal(sigalrm, lambda *_: exit_process(0))
                    signal_module.alarm(5)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                flush_session_store(cli)
            except Exception:
                pass
            try:
                shutdown_logging()
            except Exception:
                pass
            output_streams = (
                streams if streams is not None else (sys.stdout, sys.stderr)
            )
            for stream in output_streams:
                try:
                    if stream is not None:
                        stream.flush()
                except Exception:
                    pass
            exit_process(0)
            return
        raise KeyboardInterrupt()

    return handler


def install_single_query_signal_handlers(
    cli: Any,
    *,
    arm_exit_watchdog: Callable[[], None],
    flush_session_store: Callable[[Any], None],
    logger: logging.Logger,
    signal_module: SignalModule = signal,
) -> Callable[[int, Any], None]:
    """Install SIGINT/SIGTERM/SIGHUP handlers for one-shot sessions."""
    handler = make_single_query_signal_handler(
        cli,
        arm_exit_watchdog=arm_exit_watchdog,
        flush_session_store=flush_session_store,
        logger=logger,
        signal_module=signal_module,
    )
    try:
        signal_module.signal(signal_module.SIGINT, handler)
        signal_module.signal(signal_module.SIGTERM, handler)
        sighup = getattr(signal_module, "SIGHUP", None)
        if sighup is not None:
            signal_module.signal(sighup, handler)
    except Exception:
        pass
    return handler


def suppress_closed_loop_errors(loop: Any, context: Mapping[str, Any]) -> None:
    """Ignore known teardown/input errors and delegate all other loop errors."""
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return
    if isinstance(exc, KeyError) and "is not registered" in str(exc):
        return
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EIO:
        return
    loop.default_exception_handler(context)
