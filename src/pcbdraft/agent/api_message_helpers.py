"""Pure message and tool-call helpers used by the agent API boundary."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.message_sanitization import (
    coalesce_tool_call_id as _default_coalesce_tool_call_id,
)
from pcbdraft.agent.message_sanitization import (
    uniquify_tool_call_ids as _default_uniquify_tool_call_ids,
)
from pcbdraft.model.codex_responses_adapter import (
    _derive_responses_function_call_id as _default_derive_responses_function_call_id,
)
from pcbdraft.model.codex_responses_adapter import (
    _deterministic_call_id as _default_deterministic_call_id,
)
from pcbdraft.model.codex_responses_adapter import (
    _split_responses_tool_id as _default_split_responses_tool_id,
)

logger = logging.getLogger(__name__)

_coalesce_tool_call_id_hook: Callable[[Any], str]
_uniquify_tool_call_ids_hook: Callable[[list], list]
_deterministic_call_id_hook: Callable[[str, str, int], str]
_split_responses_tool_id_hook: Callable[[Any], tuple[str | None, str | None]]
_derive_responses_function_call_id_hook: Callable[[str, str | None], str]
_warning_hook: Callable[..., None]


def configure_api_message_helper_runtime(
    *,
    coalesce_tool_call_id: Callable[[Any], str] | None = None,
    uniquify_tool_call_ids: Callable[[list], list] | None = None,
    deterministic_call_id: Callable[[str, str, int], str] | None = None,
    split_responses_tool_id: Callable[[Any], tuple[str | None, str | None]]
    | None = None,
    derive_responses_function_call_id: Callable[[str, str | None], str] | None = None,
    warning: Callable[..., None] | None = None,
) -> None:
    """Inject late-bound compatibility hooks owned by the legacy module."""
    global _coalesce_tool_call_id_hook
    global _uniquify_tool_call_ids_hook
    global _deterministic_call_id_hook
    global _split_responses_tool_id_hook
    global _derive_responses_function_call_id_hook
    global _warning_hook

    if coalesce_tool_call_id is not None:
        _coalesce_tool_call_id_hook = coalesce_tool_call_id
    if uniquify_tool_call_ids is not None:
        _uniquify_tool_call_ids_hook = uniquify_tool_call_ids
    if deterministic_call_id is not None:
        _deterministic_call_id_hook = deterministic_call_id
    if split_responses_tool_id is not None:
        _split_responses_tool_id_hook = split_responses_tool_id
    if derive_responses_function_call_id is not None:
        _derive_responses_function_call_id_hook = derive_responses_function_call_id
    if warning is not None:
        _warning_hook = warning


class ApiMessageHelpersMixin:
    """Provide API-message cleanup and stable tool-call identifiers."""

    _VALID_API_ROLES = frozenset(
        {"system", "user", "assistant", "tool", "function", "developer"}
    )

    def _build_system_prompt_parts(
        self, system_message: str | None = None
    ) -> dict[str, str]:
        """Forward to the system-prompt policy owner."""
        from pcbdraft.agent.system_prompt import build_system_prompt_parts

        return build_system_prompt_parts(self, system_message=system_message)

    def _build_system_prompt(self, system_message: str | None = None) -> str:
        """Forward to the system-prompt policy owner."""
        from pcbdraft.agent.system_prompt import build_system_prompt

        return build_system_prompt(self, system_message=system_message)

    @staticmethod
    def _get_tool_call_id_static(tc: Any) -> str:
        """Extract the call ID from a dict or SDK tool-call object."""
        return _coalesce_tool_call_id_hook(tc)

    @staticmethod
    def _get_tool_call_name_static(tc: Any) -> str:
        """Extract the function name from a dict or SDK tool-call object."""
        if isinstance(tc, dict):
            fn = tc.get("function")
            if isinstance(fn, dict):
                return fn.get("name", "") or ""
            return ""
        fn = getattr(tc, "function", None)
        return getattr(fn, "name", "") or ""

    @staticmethod
    def _sanitize_api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Forward to the API-message sanitizer policy owner."""
        from pcbdraft.agent.agent_runtime_helpers import sanitize_api_messages

        return sanitize_api_messages(messages)

    @staticmethod
    def _is_thinking_only_assistant(
        msg: dict[str, Any],
        *,
        drop_codex_reasoning_items: bool = True,
    ) -> bool:
        """Return whether an assistant turn contains reasoning and no output."""
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return False
        if msg.get("tool_calls"):
            return False
        if msg.get("_thinking_prefill"):
            return True

        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                return False
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    if block:
                        return False
                    continue
                block_type = block.get("type")
                if block_type in {"thinking", "redacted_thinking"}:
                    continue
                if block_type == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text.strip():
                        return False
                    continue
                return False
        elif content is not None and content != "":
            return False

        from pcbdraft.agent.native_compaction import has_compaction_checkpoint

        if has_compaction_checkpoint(msg.get("codex_reasoning_items")):
            return False
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return True
        reasoning_details = msg.get("reasoning_details")
        if isinstance(reasoning_details, list) and reasoning_details:
            return True
        codex_items = msg.get("codex_reasoning_items")
        if drop_codex_reasoning_items and isinstance(codex_items, list):
            return any(
                isinstance(item, dict) and item.get("type") == "reasoning"
                for item in codex_items
            )
        return False

    @staticmethod
    def _drop_thinking_only_and_merge_users(
        messages: list[dict[str, Any]],
        *,
        drop_codex_reasoning_items: bool = True,
    ) -> list[dict[str, Any]]:
        """Forward to thinking-only cleanup and adjacent-user merging."""
        from pcbdraft.agent.agent_runtime_helpers import (
            drop_thinking_only_and_merge_users,
        )

        return drop_thinking_only_and_merge_users(
            messages,
            drop_codex_reasoning_items=drop_codex_reasoning_items,
        )

    @staticmethod
    def _cap_delegate_task_calls(tool_calls: list) -> list:
        """Truncate excess delegate-task calls to the configured child cap."""
        from pcbdraft.tools.delegate_tool import _get_max_concurrent_children

        max_children = _get_max_concurrent_children()
        delegate_count = sum(
            1 for tool_call in tool_calls if tool_call.function.name == "delegate_task"
        )
        if delegate_count <= max_children:
            return tool_calls
        kept_delegates = 0
        truncated = []
        for tool_call in tool_calls:
            if tool_call.function.name == "delegate_task":
                if kept_delegates < max_children:
                    truncated.append(tool_call)
                    kept_delegates += 1
            else:
                truncated.append(tool_call)
        _warning_hook(
            "Truncated %d excess delegate_task call(s) to enforce "
            "max_concurrent_children=%d limit",
            delegate_count - max_children,
            max_children,
        )
        return truncated

    @staticmethod
    def _deduplicate_tool_calls(tool_calls: list) -> list:
        """Keep the first tool call for each canonical name/arguments pair."""
        seen: set = set()
        unique: list = []
        for tool_call in tool_calls:
            arguments = tool_call.function.arguments
            try:
                arguments = json.dumps(
                    json.loads(arguments), separators=(",", ":"), sort_keys=True
                )
            except (TypeError, ValueError):
                pass
            key = (tool_call.function.name, arguments)
            if key not in seen:
                seen.add(key)
                unique.append(tool_call)
            else:
                _warning_hook(
                    "Removed duplicate tool call: %s", tool_call.function.name
                )
        return unique if len(unique) < len(tool_calls) else tool_calls

    @staticmethod
    def _uniquify_tool_call_ids(tool_calls: list) -> list:
        """Give colliding tool-call IDs deterministic suffixes."""
        return _uniquify_tool_call_ids_hook(tool_calls)

    def _repair_tool_call(self, tool_name: str) -> str | None:
        """Forward to the tool-name repair policy owner."""
        from pcbdraft.agent.agent_runtime_helpers import repair_tool_call

        return repair_tool_call(self, tool_name)

    @staticmethod
    def _deterministic_call_id(fn_name: str, arguments: str, index: int = 0) -> str:
        """Generate a stable fallback call ID from tool-call content."""
        return _deterministic_call_id_hook(fn_name, arguments, index)

    @staticmethod
    def _split_responses_tool_id(raw_id: Any) -> tuple[str | None, str | None]:
        """Split a stored tool ID into call and response-item IDs."""
        return _split_responses_tool_id_hook(raw_id)

    def _derive_responses_function_call_id(
        self,
        call_id: str,
        response_item_id: str | None = None,
    ) -> str:
        """Build a valid Responses function-call item ID."""
        return _derive_responses_function_call_id_hook(call_id, response_item_id)


_coalesce_tool_call_id_hook = _default_coalesce_tool_call_id
_uniquify_tool_call_ids_hook = _default_uniquify_tool_call_ids
_deterministic_call_id_hook = _default_deterministic_call_id
_split_responses_tool_id_hook = _default_split_responses_tool_id
_derive_responses_function_call_id_hook = _default_derive_responses_function_call_id
_warning_hook = logger.warning
