# The compatibility helpers deliberately keep broad best-effort fallbacks and
# a lock file handle open for the lifetime of its cookie.
# ruff: noqa: BLE001, S110, SIM115
"""MCP process guards, event-loop startup, and synchronous call delivery.

MCP server ownership, discovery, registration, and connection behavior remain
in :mod:`pcbdraft.tools.mcp_tool`.  That module injects its live loop state and
legacy hooks here so callers can continue patching the original symbol paths.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import logging
import os
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger("pcbdraft.tools.mcp_tool")

_LOCK_UNAVAILABLE: Any = object()
_DEFAULT_DISCOVERY_LOCK_PATH: str | None = None
_USE_DEFAULT_LOCK_PATH = object()

# Non-MCP gateway children that can race into a child-PID snapshot while an
# MCP stdio process starts.  Match argv markers rather than argv[0] because
# Python and Java children begin with their interpreter or binary path.
_NON_MCP_CHILD_CMDLINE_MARKERS: tuple[str, ...] = (
    "tui_gateway.slash_worker",
    "tui_gateway.entry",
    "-dorg.eclipse.equinox.launcher",  # jdtls (legacy arg style)
    "eclipse.jdt.ls",
    "org.eclipse.equinox.launcher_",
)


class _LockCookie:
    """Hold a cross-process file lock until :meth:`release` is called."""

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                fd = self._fh.fileno()
                if os.name == "posix":
                    import fcntl

                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                else:
                    import portalocker

                    try:
                        portalocker.unlock(self._fh)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _acquire_lock_on_fh(fh: Any) -> bool:
    """Acquire a non-blocking exclusive lock on an open file handle."""

    fd = fh.fileno()
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
    else:
        import portalocker

        try:
            portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
            return True
        except portalocker.LockException:
            return False


def _cache_default_discovery_lock_path(lock_path: str) -> None:
    global _DEFAULT_DISCOVERY_LOCK_PATH
    _DEFAULT_DISCOVERY_LOCK_PATH = lock_path


def _try_acquire_mcp_discovery_lock(
    *,
    lock_path: Any = _USE_DEFAULT_LOCK_PATH,
    cache_lock_path: Callable[[str], None] | None = None,
    lock_unavailable: Any = _LOCK_UNAVAILABLE,
    runtime_home_getter: Callable[[], Any] | None = None,
    acquire_lock_on_fh: Callable[[Any], bool] = _acquire_lock_on_fh,
    lock_cookie_factory: Callable[[Any], Any] = _LockCookie,
) -> Any:
    """Try to acquire the cross-process discovery lock without blocking."""

    if lock_path is _USE_DEFAULT_LOCK_PATH:
        lock_path = _DEFAULT_DISCOVERY_LOCK_PATH
    try:
        if runtime_home_getter is None:
            from pcbdraft.core.runtime_environment import get_runtime_home

            runtime_home_getter = get_runtime_home
        if lock_path is None:
            lock_path = str(runtime_home_getter() / ".mcp-discovery.lock")
            (cache_lock_path or _cache_default_discovery_lock_path)(lock_path)
    except Exception:
        return lock_unavailable

    try:
        fh = open(lock_path, "w", encoding="utf-8")
    except Exception:
        return lock_unavailable

    try:
        acquired = acquire_lock_on_fh(fh)
    except Exception:
        fh.close()
        return lock_unavailable

    if acquired:
        return lock_cookie_factory(fh)
    fh.close()
    return None


def _snapshot_child_pids() -> set[int]:
    """Return current direct child process PIDs when they can be inspected."""

    my_pid = os.getpid()
    try:
        children_path = f"/proc/{my_pid}/task/{my_pid}/children"
        with open(children_path, encoding="utf-8") as children:
            return {int(pid) for pid in children.read().split() if pid.strip()}
    except (FileNotFoundError, OSError, ValueError):
        pass

    try:
        import psutil

        return {child.pid for child in psutil.Process(my_pid).children()}
    except Exception:
        return set()


def _filter_mcp_children(
    pids: set[int],
    *,
    non_mcp_markers: tuple[str, ...] = _NON_MCP_CHILD_CMDLINE_MARKERS,
) -> set[int]:
    """Remove known non-MCP gateway children from a PID snapshot delta."""

    if not pids:
        return pids
    try:
        import psutil
    except ImportError:
        return pids
    filtered: set[int] = set()
    for pid in pids:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if any(marker in arg for arg in argv[1:] for marker in non_mcp_markers):
            continue
        filtered.add(pid)
    return filtered


def _mcp_loop_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    """Suppress benign event-loop-closed noise during MCP shutdown."""

    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return
    loop.default_exception_handler(context)


def _ensure_mcp_loop(
    current_loop: asyncio.AbstractEventLoop | None,
    *,
    exception_handler: Callable[[Any, dict[str, Any]], None] = (
        _mcp_loop_exception_handler
    ),
    event_loop_factory: Callable[[], asyncio.AbstractEventLoop] = (
        asyncio.new_event_loop
    ),
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
) -> tuple[asyncio.AbstractEventLoop, threading.Thread | None]:
    """Return a running MCP loop and a newly started thread when needed.

    Synchronization and state ownership belong to the caller.  This lets the
    compatibility module keep the same loop globals used by its shutdown path.
    """

    if current_loop is not None and current_loop.is_running():
        return current_loop, None
    loop = event_loop_factory()
    loop.set_exception_handler(exception_handler)
    thread = thread_factory(
        target=loop.run_forever,
        name="mcp-event-loop",
        daemon=True,
    )
    thread.start()
    return loop, thread


def _wrap_with_home_override(
    coro: Coroutine[Any, Any, Any],
) -> Coroutine[Any, Any, Any]:
    """Carry the caller's context-local runtime-home override into ``coro``."""

    try:
        from pcbdraft.core.runtime_environment import (
            get_runtime_home_override,
            reset_runtime_home_override,
            set_runtime_home_override,
        )

        home_override = get_runtime_home_override()
    except Exception:
        return coro
    if not home_override:
        return coro

    async def _scoped():
        token = set_runtime_home_override(home_override)
        try:
            return await coro
        finally:
            reset_runtime_home_override(token)

    return _scoped()


def _wrap_with_dashboard_oauth_flow(
    coro: Coroutine[Any, Any, Any],
) -> Coroutine[Any, Any, Any]:
    """Propagate a dashboard OAuth flow onto the dedicated MCP loop task."""

    try:
        from pcbdraft.tools.mcp_dashboard_oauth import (
            dashboard_oauth_flow,
            get_dashboard_oauth_flow,
        )

        flow = get_dashboard_oauth_flow()
    except Exception:
        return coro
    if flow is None:
        return coro

    async def _scoped():
        with dashboard_oauth_flow(flow):
            return await coro

    return _scoped()


def _run_on_mcp_loop(
    coro_or_factory: Any,
    timeout: float | None = 30,
    *,
    loop: asyncio.AbstractEventLoop | None,
    schedule_threadsafe: Callable[..., Any],
    is_interrupted: Callable[[], bool],
    home_wrapper: Callable[[Coroutine[Any, Any, Any]], Coroutine[Any, Any, Any]] = (
        _wrap_with_home_override
    ),
    dashboard_oauth_wrapper: Callable[
        [Coroutine[Any, Any, Any]], Coroutine[Any, Any, Any]
    ] = _wrap_with_dashboard_oauth_flow,
    runtime_logger: logging.Logger = logger,
) -> Any:
    """Schedule a coroutine on the MCP loop and synchronously await it."""

    if loop is None or not loop.is_running():
        if asyncio.iscoroutine(coro_or_factory):
            coro_or_factory.close()
        raise RuntimeError("MCP event loop is not running")

    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    coro = home_wrapper(coro)
    coro = dashboard_oauth_wrapper(coro)

    future = schedule_threadsafe(
        coro,
        loop,
        logger=runtime_logger,
        log_message="MCP scheduling failed",
    )
    if future is None:
        raise RuntimeError("MCP event loop unavailable (failed to schedule)")
    start_time = time.monotonic()
    deadline = None if timeout is None else start_time + timeout

    while True:
        if is_interrupted():
            future.cancel()
            raise InterruptedError("User sent a new message")

        wait_timeout = 0.1
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                elapsed = time.monotonic() - start_time
                raise TimeoutError(
                    f"MCP call timed out after {elapsed:.1f}s "
                    f"(configured timeout: {float(timeout):.1f}s)"
                )
            wait_timeout = min(wait_timeout, remaining)

        try:
            return future.result(timeout=wait_timeout)
        except concurrent.futures.TimeoutError:
            if future.done():
                return future.result()


def _interrupted_call_result(
    *,
    error_factory: Callable[[str], str],
) -> str:
    """Return the standardized JSON error for an interrupted MCP call."""

    return error_factory("MCP call interrupted: user sent a new message")
