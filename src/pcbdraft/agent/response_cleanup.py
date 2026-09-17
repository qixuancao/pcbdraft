"""Clean assistant responses and classify incomplete reasoning output."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_regex_search_hook: Callable[[str, str], Any]


def configure_response_cleanup_runtime(
    *,
    regex_search: Callable[[str, str], Any] | None = None,
) -> None:
    """Inject the legacy module's late-bound regular-expression helper."""
    global _regex_search_hook

    if regex_search is not None:
        _regex_search_hook = regex_search


class ResponseCleanupMixin:
    """Normalize visible output and recover reasoning metadata."""

    def _has_content_after_think_block(self, content: str) -> bool:
        """Return whether visible content remains after reasoning is removed."""
        if not content:
            return False
        cleaned = self._strip_think_blocks(content)
        return bool(cleaned.strip())

    def _strip_think_blocks(self, content: str) -> str:
        """Forward to the shared response-cleanup policy owner."""
        from pcbdraft.agent.agent_runtime_helpers import strip_think_blocks

        return strip_think_blocks(self, content)

    @staticmethod
    def _has_natural_response_ending(content: str) -> bool:
        """Return whether visible assistant text looks intentionally finished."""
        if not content:
            return False
        stripped = content.rstrip()
        if not stripped:
            return False
        if stripped.endswith("```"):
            return True
        if stripped.endswith("^"):
            return True
        last = stripped[-1]
        if last in ".!?:)\"']}。！？：）】」』》^":
            return True
        return ord(last) >= 127744

    def _is_ollama_glm_backend(self) -> bool:
        """Detect Ollama-hosted GLM models affected by stop misreports."""
        model_lower = (self.model or "").lower()
        provider_lower = (self.provider or "").lower()
        if "glm" not in model_lower and provider_lower != "zai":
            return False
        if "ollama" in self._base_url_lower or ":11434" in self._base_url_lower:
            return True
        return provider_lower == "ollama"

    def _should_treat_stop_as_truncated(
        self,
        finish_reason: str,
        assistant_message: Any,
        messages: list | None = None,
    ) -> bool:
        """Detect conservative stop-to-length misreports from Ollama GLM."""
        if finish_reason != "stop" or self.api_mode != "chat_completions":
            return False
        if not self._is_ollama_glm_backend():
            return False
        if not any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in (messages or [])
        ):
            return False
        if assistant_message is None or getattr(assistant_message, "tool_calls", None):
            return False

        content = getattr(assistant_message, "content", None)
        if not isinstance(content, str):
            return False

        visible_text = self._strip_think_blocks(content).strip()
        if not visible_text:
            return False
        if len(visible_text) < 20 or not _regex_search_hook(r"\s", visible_text):
            return False

        return not self._has_natural_response_ending(visible_text)

    def _looks_like_codex_intermediate_ack(
        self,
        user_message: str,
        assistant_content: str,
        messages: list[dict[str, Any]],
        require_workspace: bool = True,
    ) -> bool:
        """Forward to the shared intermediate-ack policy owner."""
        from pcbdraft.agent.agent_runtime_helpers import (
            looks_like_codex_intermediate_ack,
        )

        return looks_like_codex_intermediate_ack(
            self, user_message, assistant_content, messages, require_workspace
        )

    def _extract_reasoning(self, assistant_message: Any) -> str | None:
        """Forward to the shared structured-reasoning extractor."""
        from pcbdraft.agent.agent_runtime_helpers import extract_reasoning

        return extract_reasoning(self, assistant_message)


_regex_search_hook = re.search
