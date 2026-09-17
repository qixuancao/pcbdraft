"""Pure auxiliary-response text and shape projection helpers.

This module does not perform requests, usage accounting, relay completion,
credential lookup, provider selection, fallback execution, or client caching.
It deliberately does not import :mod:`pcbdraft.model.auxiliary_client`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    value = getattr(obj, key, default)
    if value is default and isinstance(obj, dict):
        value = obj.get(key, default)
    return value


def _extract_aux_response_text(
    response: Any,
    *,
    object_get: Callable[..., Any] = _obj_get,
) -> str:
    output_text = object_get(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = object_get(response, "output")
    if not isinstance(output, list):
        return ""

    parts: list[str] = []
    for item in output:
        item_type = object_get(item, "type")
        if item_type and item_type != "message":
            continue
        for part in object_get(item, "content") or []:
            part_type = object_get(part, "type")
            if part_type in {"output_text", "text", None}:
                text = object_get(part, "text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts).strip()


def _recover_aux_response_message(
    response: Any,
    *,
    extract_text: Callable[[Any], str] = _extract_aux_response_text,
) -> Any | None:
    """Synthesize chat-completions shape from Responses-style text fields."""

    text = extract_text(response)
    if not text:
        return None

    choice = SimpleNamespace(
        message=SimpleNamespace(content=text),
        finish_reason=getattr(response, "finish_reason", None) or "stop",
    )
    try:
        response.choices = [choice]
        return response
    except Exception:  # noqa: BLE001 - immutable provider response fallback
        return SimpleNamespace(
            id=getattr(response, "id", ""),
            model=getattr(response, "model", ""),
            object=getattr(response, "object", "chat.completion"),
            choices=[choice],
            usage=getattr(response, "usage", None),
        )


def extract_content_or_reasoning(response: Any) -> str:
    """Extract visible content, falling back to structured reasoning fields."""

    msg = response.choices[0].message
    content = (msg.content or "").strip()

    if content:
        cleaned = re.sub(
            r"<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>"
            r".*?"
            r"</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned

    reasoning_parts: list[str] = []
    for field in ("reasoning", "reasoning_content"):
        value = getattr(msg, field, None)
        if (
            value
            and isinstance(value, str)
            and value.strip()
            and value not in reasoning_parts
        ):
            reasoning_parts.append(value.strip())

    details = getattr(msg, "reasoning_details", None)
    if details and isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                summary = (
                    detail.get("summary") or detail.get("content") or detail.get("text")
                )
                if summary and summary not in reasoning_parts:
                    reasoning_parts.append(
                        summary.strip() if isinstance(summary, str) else str(summary)
                    )

    if reasoning_parts:
        return "\n\n".join(reasoning_parts)
    return ""
