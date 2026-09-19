# mypy: disable-error-code="attr-defined"
"""Release AIAgent client and session-owned resources."""

# Teardown is deliberately best-effort so one broken resource cannot leak others.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import logging
from collections.abc import Callable

from pcbdraft.tools.terminal_tool import cleanup_vm as _default_cleanup_vm

logger = logging.getLogger(__name__)

_cleanup_vm_hook: Callable[[str], None]
_cleanup_browser_hook: Callable[[str], None]
_debug_hook: Callable[..., None]


def configure_client_lifecycle_runtime(
    *,
    cleanup_vm: Callable[[str], None] | None = None,
    cleanup_browser: Callable[[str], None] | None = None,
    debug: Callable[..., None] | None = None,
) -> None:
    """Inject late-bound teardown dependencies owned by the legacy module."""
    global _cleanup_vm_hook
    global _cleanup_browser_hook
    global _debug_hook

    if cleanup_vm is not None:
        _cleanup_vm_hook = cleanup_vm
    if cleanup_browser is not None:
        _cleanup_browser_hook = cleanup_browser
    if debug is not None:
        _debug_hook = debug


class ClientLifecycleMixin:
    """Own soft client eviction and hard agent teardown."""

    def release_clients(self) -> None:
        """Release LLM clients and child agents while keeping session tools alive."""
        try:
            with self._active_children_lock:
                children = list(self._active_children)
                self._active_children.clear()
            for child in children:
                try:
                    child.release_clients()
                except Exception:
                    try:
                        child.close()
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            client = getattr(self, "client", None)
            if client is not None:
                self._retire_shared_openai_client(client, reason="cache_evict")
                self.client = None
        except Exception:
            pass

        try:
            self._close_cached_request_openai_client(reason="cache_evict")
        except Exception:
            pass
        try:
            self._close_cached_request_anthropic_client(reason="cache_evict")
        except Exception:
            pass

    def close(self) -> None:
        """Release all resources owned by this agent instance."""
        try:
            session_messages = getattr(self, "_session_messages", None)
            self.shutdown_memory_provider(
                session_messages if isinstance(session_messages, list) else None
            )
        except Exception:
            pass

        task_id = getattr(self, "session_id", None) or ""

        try:
            from pcbdraft.tools.process_registry import process_registry

            process_registry.kill_all(task_id=task_id)
        except Exception:
            pass

        try:
            _cleanup_vm_hook(task_id)
        except Exception:
            pass

        try:
            _cleanup_browser_hook(task_id)
        except Exception:
            pass

        try:
            from pcbdraft.tools.computer_use import release_computer_use_session

            release_computer_use_session(task_id)
        except Exception:
            pass

        try:
            with self._active_children_lock:
                children = list(self._active_children)
                self._active_children.clear()
            for child in children:
                try:
                    child.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            client = getattr(self, "client", None)
            if client is not None:
                self._close_openai_client(client, reason="agent_close", shared=True)
                self.client = None
        except Exception:
            pass

        try:
            self._close_cached_request_openai_client(reason="agent_close")
        except Exception:
            pass
        try:
            self._close_cached_request_anthropic_client(reason="agent_close")
        except Exception:
            pass

        try:
            anthropic_client = getattr(self, "_anthropic_client", None)
            if anthropic_client is not None:
                self._anthropic_client = None
                anthropic_client.close()
        except Exception:
            _debug_hook("Shared Anthropic client close failed", exc_info=True)

        try:
            codex_session = getattr(self, "_codex_session", None)
            if codex_session is not None:
                self._codex_session = None
                codex_session.close()
        except Exception:
            pass

        try:
            self._session_messages: list = []
        except Exception:
            pass

        try:
            from pcbdraft.interfaces.tui.mem_trim import trim_memory

            trim_memory(force=True, reason="agent close")
        except Exception:
            pass

        session_db = getattr(self, "_session_db", None)
        try:
            if getattr(self, "_end_session_on_close", True):
                session_id = getattr(self, "session_id", None)
                if session_db and session_id:
                    session_db.end_session(session_id, "agent_close")
        except Exception:
            pass

        try:
            if getattr(self, "_owns_session_db", False) and session_db is not None:
                self._owns_session_db = False
                session_db.close()
        except Exception:
            pass

    def _close_cached_request_openai_client(self, *, reason: str) -> None:
        """Close or abort the cached request-local OpenAI client."""
        with self._openai_client_lock():
            cache = getattr(self, "_request_client_cache", None)
            client = cache["client"] if cache else None
            in_use = bool(cache["in_use"]) if cache else False
            if cache is not None:
                cache["client"] = None
                cache["kwargs"] = None
                cache["poisoned"] = False
                cache["in_use"] = False
        if client is None:
            return
        if in_use:
            self._abort_request_openai_client(client, reason=f"{reason}_in_flight")
            return
        self._close_openai_client(client, reason=reason, shared=False)

    def _close_cached_request_anthropic_client(self, *, reason: str) -> None:
        """Close or abort the cached request-local Anthropic client."""
        with self._openai_client_lock():
            cache = getattr(self, "_request_anthropic_client_cache", None)
            client = cache["client"] if cache else None
            in_use = bool(cache["in_use"]) if cache else False
            if cache is not None:
                cache["client"] = None
                cache["key"] = None
                cache["poisoned"] = False
                cache["in_use"] = False
        if client is None:
            return
        if in_use:
            self._abort_request_anthropic_client(client, reason=f"{reason}_in_flight")
            return
        try:
            self._force_close_tcp_sockets(client)
            client.close()
        except Exception:
            pass


def _noop_cleanup_browser(_task_id: str) -> None:
    return None


_cleanup_vm_hook = _default_cleanup_vm
_cleanup_browser_hook = _noop_cleanup_browser
_debug_hook = logger.debug
