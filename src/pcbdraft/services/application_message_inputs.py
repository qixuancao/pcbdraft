"""Pure message-input normalization for ``ApplicationService``.

Message delivery, project locking, provider calls, transcript writes, and state
transitions remain in the host application.  This module only normalizes one
message and projects the exactly-once reply binding from existing transcript
data.  It deliberately does not import :mod:`pcbdraft.services.application`.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from typing import Any


class ApplicationMessageInputMixin:
    """Normalize message text and read reply delivery identity."""

    def _normalize_message_text(self, text: str, field: str) -> str:
        bounded = self._message_input_safe_text(
            text,
            field,
            limit=self._message_input_max_bytes(),
        )
        return self._message_input_sanitize_secret_text(bounded)

    def _reply_delivery_binding(
        self,
        turn_id: str | None,
        index: int | None,
    ) -> dict[str, str | int] | None:
        if turn_id is None and index is None:
            return None
        if (
            not isinstance(turn_id, str)
            or isinstance(index, bool)
            or not (isinstance(index, int) and index >= 0)
        ):
            raise self._message_input_validation_error(
                "reply delivery binding is invalid"
            )
        return {"turn_id": turn_id, "index": index}

    @staticmethod
    def _reply_already_delivered(
        conversation: dict[str, Any],
        binding: dict[str, str | int],
    ) -> bool:
        for message in conversation["messages"]:
            data = message.get("data") if isinstance(message, dict) else None
            if not isinstance(data, dict):
                continue
            if (
                data.get("turn_id") == binding["turn_id"]
                and data.get("index") == binding["index"]
            ):
                return True
        return False
