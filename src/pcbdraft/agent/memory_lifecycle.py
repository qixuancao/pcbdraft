"""External-memory lifecycle helpers for AIAgent sessions and turns."""

# External memory providers are optional and deliberately best-effort.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.memory_provider import (
    is_trivial_prompt as _default_is_trivial_prompt,
)
from pcbdraft.model.codex_responses_adapter import (
    _summarize_user_message_for_log as _default_summarize_user_message,
)

logger = logging.getLogger(__name__)

_summarize_user_message_hook: Callable[..., str]
_is_trivial_prompt_hook: Callable[[str], bool]
_warning_hook: Callable[..., None]


def configure_memory_lifecycle_runtime(
    *,
    summarize_user_message: Callable[..., str] | None = None,
    is_trivial_prompt: Callable[[str], bool] | None = None,
    warning: Callable[..., None] | None = None,
) -> None:
    """Inject late-bound dependencies owned by the legacy agent module."""
    global _summarize_user_message_hook
    global _is_trivial_prompt_hook
    global _warning_hook

    if summarize_user_message is not None:
        _summarize_user_message_hook = summarize_user_message
    if is_trivial_prompt is not None:
        _is_trivial_prompt_hook = is_trivial_prompt
    if warning is not None:
        _warning_hook = warning


class MemoryLifecycleMixin:
    """Manage external-memory providers without owning the agent itself."""

    def shutdown_memory_provider(self, messages: list | None = None) -> None:
        """Flush and shut down memory providers once at session end."""
        if getattr(self, "_memory_provider_shutdown", False):
            return
        self._memory_provider_shutdown = True
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception as exc:
                _warning_hook(
                    "Memory provider on_session_end failed during shutdown: %s",
                    exc,
                    exc_info=True,
                )
            try:
                self._memory_manager.shutdown_all()
            except Exception:
                pass
        if hasattr(self, "context_compressor") and self.context_compressor:
            try:
                self.context_compressor.on_session_end(
                    self.session_id or "",
                    messages or [],
                )
            except Exception:
                pass

    def commit_memory_session(self, messages: list | None = None) -> None:
        """Flush session memory without tearing providers down."""
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception:
                pass
        if hasattr(self, "context_compressor") and self.context_compressor:
            try:
                self.context_compressor.on_session_end(
                    self.session_id or "",
                    messages or [],
                )
            except Exception:
                pass

    def _sync_external_memory_for_turn(
        self,
        *,
        original_user_message: Any,
        final_response: Any,
        interrupted: bool,
        messages: list | None = None,
    ) -> None:
        """Persist and prefetch external memory for one completed turn."""
        if interrupted:
            return
        if not (self._memory_manager and final_response and original_user_message):
            return
        user_text = _summarize_user_message_hook(original_user_message, sep="\n")
        response_text = _summarize_user_message_hook(final_response, sep="\n")
        if not (user_text and response_text):
            return
        try:
            sync_kwargs = {"session_id": self.session_id or ""}
            if messages is not None:
                sync_kwargs["messages"] = messages
            self._memory_manager.sync_all(
                user_text,
                response_text,
                **sync_kwargs,
            )
            if not _is_trivial_prompt_hook(user_text):
                self._memory_manager.queue_prefetch_all(
                    user_text,
                    session_id=self.session_id or "",
                )
        except Exception:
            pass


_summarize_user_message_hook = _default_summarize_user_message
_is_trivial_prompt_hook = _default_is_trivial_prompt
_warning_hook = logger.warning
