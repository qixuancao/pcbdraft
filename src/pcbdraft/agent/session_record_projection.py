"""Pure session-record shaping and redaction helpers.

The request loop and snapshot/database writers remain in ``agent.loop``.  This
module only projects in-memory message records and deliberately has no session
switching, persistence, model-call, or cancellation responsibilities.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.redact import redact_sensitive_text
from pcbdraft.agent.trajectory import convert_scratchpad_to_think

_convert_scratchpad_hook: Callable[[str], str]
_redact_text_hook: Callable[[str], str]
_regex_sub_hook: Callable[[str, str, str], str]


def configure_session_record_projection_runtime(
    *,
    convert_scratchpad: Callable[[str], str] | None = None,
    redact_text: Callable[[str], str] | None = None,
    regex_sub: Callable[[str, str, str], str] | None = None,
) -> None:
    """Inject helpers exposed through the legacy ``agent.loop`` module."""

    global _convert_scratchpad_hook
    global _redact_text_hook
    global _regex_sub_hook

    if convert_scratchpad is not None:
        _convert_scratchpad_hook = convert_scratchpad
    if redact_text is not None:
        _redact_text_hook = redact_text
    if regex_sub is not None:
        _regex_sub_hook = regex_sub


class SessionRecordProjectionMixin:
    """Project conversation records without performing persistence writes."""

    def _get_messages_up_to_last_assistant(self, messages: list[dict]) -> list[dict]:
        """Return the history before its last assistant record."""

        if not messages:
            return []

        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "assistant":
                return messages[:index]
        return messages.copy()

    @staticmethod
    def _clean_session_content(content: str) -> str:
        """Convert scratchpad tags and normalize whitespace around think tags."""

        if not content:
            return content
        content = _convert_scratchpad_hook(content)
        content = _regex_sub_hook(r"\n+(<think>)", r"\n\1", content)
        content = _regex_sub_hook(r"(</think>)\n+", r"\1\n", content)
        return content.strip()

    @staticmethod
    def _redact_message_content(content: Any) -> Any:
        """Redact text fields while retaining the input content shape."""

        if content is None:
            return content
        if isinstance(content, str):
            return _redact_text_hook(content)
        if isinstance(content, list):
            redacted = []
            for part in content:
                if isinstance(part, dict):
                    part = dict(part)
                    if isinstance(part.get("text"), str):
                        part["text"] = _redact_text_hook(part["text"])
                    if isinstance(part.get("content"), str):
                        part["content"] = _redact_text_hook(part["content"])
                redacted.append(part)
            return redacted
        return content


_convert_scratchpad_hook = convert_scratchpad_to_think
_redact_text_hook = redact_sensitive_text
_regex_sub_hook = re.sub
