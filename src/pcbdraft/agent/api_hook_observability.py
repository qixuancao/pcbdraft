# mypy: disable-error-code="attr-defined"
"""Sanitize and emit API hook payloads and delegate debug request dumps."""

# Hook delivery and SDK-object normalization are deliberately best-effort.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pcbdraft.model.usage_pricing import normalize_usage

_normalize_usage_hook: Callable[..., Any]
_env_get_hook: Callable[[str, str], str]
_is_simple_namespace_hook: Callable[[Any], bool]
_json_dumps_hook: Callable[..., str]
_now_hook: Callable[[], float]


def configure_api_hook_observability_runtime(
    *,
    normalize_usage_fn: Callable[..., Any] | None = None,
    env_get: Callable[[str, str], str] | None = None,
    is_simple_namespace: Callable[[Any], bool] | None = None,
    json_dumps: Callable[..., str] | None = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Inject late-bound helpers exposed by the legacy agent module."""
    global _normalize_usage_hook
    global _env_get_hook
    global _is_simple_namespace_hook
    global _json_dumps_hook
    global _now_hook

    if normalize_usage_fn is not None:
        _normalize_usage_hook = normalize_usage_fn
    if env_get is not None:
        _env_get_hook = env_get
    if is_simple_namespace is not None:
        _is_simple_namespace_hook = is_simple_namespace
    if json_dumps is not None:
        _json_dumps_hook = json_dumps
    if now is not None:
        _now_hook = now


class ApiHookObservabilityMixin:
    """Prepare bounded API hook payloads and dispatch hook notifications."""

    def _usage_summary_for_api_request_hook(
        self, response: Any
    ) -> dict[str, Any] | None:
        """Token buckets for ``post_api_request`` plugins (no raw ``response`` object)."""
        if response is None:
            return None
        raw_usage = getattr(response, "usage", None)
        if not raw_usage:
            return None
        from dataclasses import asdict

        cu = _normalize_usage_hook(
            raw_usage, provider=self.provider, api_mode=self.api_mode
        )
        summary = asdict(cu)
        summary.pop("raw_usage", None)
        summary["prompt_tokens"] = cu.prompt_tokens
        summary["total_tokens"] = cu.total_tokens
        return summary

    @staticmethod
    def _hook_payload_max_chars() -> int:
        raw = _env_get_hook("PCBDRAFT_RUNTIME_PLUGIN_PAYLOAD_MAX_CHARS", "50000")
        try:
            return max(1000, int(raw))
        except (TypeError, ValueError):
            return 50000

    @staticmethod
    def _is_sensitive_hook_key(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower().replace("-", "_")
        exact = {
            "api_key",
            "authorization",
            "proxy_authorization",
            "cookie",
            "set_cookie",
        }
        return lowered in exact or lowered.endswith("_api_key")

    @classmethod
    def _hook_jsonable(
        cls,
        value: Any,
        *,
        depth: int = 0,
        max_depth: int = 8,
        max_string: int = 8000,
        max_sequence: int = 200,
    ) -> Any:
        if depth > max_depth:
            return f"<{type(value).__name__} depth limit>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) > max_string:
                return (
                    value[:max_string]
                    + f"...[truncated {len(value) - max_string} chars]"
                )
            return value
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= max_sequence:
                    out["_truncated_items"] = len(value) - max_sequence
                    break
                str_key = str(key)
                if cls._is_sensitive_hook_key(str_key):
                    out[str_key] = "<redacted>"
                else:
                    out[str_key] = cls._hook_jsonable(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_string=max_string,
                        max_sequence=max_sequence,
                    )
            return out
        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            out_items = [
                cls._hook_jsonable(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
                for item in seq[:max_sequence]
            ]
            if len(seq) > max_sequence:
                out_items.append({"_truncated_items": len(seq) - max_sequence})
            return out_items
        try:
            if hasattr(value, "model_dump"):
                try:
                    # warnings=False: pydantic's serializer UserWarnings on
                    # generic-union SDK models (Anthropic ParsedMessage etc.)
                    # would otherwise leak to the terminal mid-response.
                    dumped = value.model_dump(mode="json", warnings=False)
                except TypeError:
                    try:
                        dumped = value.model_dump(mode="json")
                    except TypeError:
                        dumped = value.model_dump()
                return cls._hook_jsonable(
                    dumped,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
        except Exception:
            pass
        try:
            from dataclasses import asdict, is_dataclass

            if is_dataclass(value) and not isinstance(value, type):
                return cls._hook_jsonable(
                    asdict(value),
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
        except Exception:
            pass
        if _is_simple_namespace_hook(value):
            return cls._hook_jsonable(
                vars(value),
                depth=depth + 1,
                max_depth=max_depth,
                max_string=max_string,
                max_sequence=max_sequence,
            )
        if hasattr(value, "__dict__"):
            try:
                public_attrs = {
                    k: v for k, v in vars(value).items() if not str(k).startswith("_")
                }
                return cls._hook_jsonable(
                    public_attrs,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
            except Exception:
                pass
        return str(value)[:max_string]

    @classmethod
    def _sanitize_hook_payload(cls, value: Any) -> Any:
        payload = cls._hook_jsonable(value)
        limit = cls._hook_payload_max_chars()
        try:
            encoded = _json_dumps_hook(payload, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)[:limit]
        if len(encoded) <= limit:
            return payload
        payload = cls._hook_jsonable(value, max_string=1000, max_sequence=50)
        try:
            encoded = _json_dumps_hook(payload, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)[:limit]
        if len(encoded) <= limit:
            return payload
        return {
            "_truncated": True,
            "original_type": type(value).__name__,
            "preview": encoded[:limit],
        }

    def _api_request_payload_for_hook(
        self, api_kwargs: dict[str, Any] | None
    ) -> dict[str, Any]:
        body = {
            key: value
            for key, value in (api_kwargs or {}).items()
            if key not in {"timeout", "http_client"}
        }
        return self._sanitize_hook_payload(
            {
                "method": "POST",
                "body": body,
            }
        )

    def _api_response_payload_for_hook(
        self,
        response: Any,
        assistant_message: Any,
        *,
        finish_reason: str | None,
    ) -> dict[str, Any]:
        # ``tool_calls`` is the raw list of provider SDK objects (e.g.
        # OpenAI ``ChatCompletionMessageToolCall``).  We deliberately hand
        # the raw objects to ``_sanitize_hook_payload`` and rely on
        # ``_hook_jsonable`` to normalise them via ``model_dump`` /
        # ``__dict__`` / dataclass introspection — a future refactor of
        # the sanitiser MUST preserve that capability or hook subscribers
        # will receive opaque ``str(obj)`` blobs here.
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        return self._sanitize_hook_payload(
            {
                "model": getattr(response, "model", None),
                "finish_reason": finish_reason,
                "assistant_message": {
                    "role": getattr(assistant_message, "role", "assistant"),
                    "content": getattr(assistant_message, "content", None),
                    "tool_calls": tool_calls,
                },
                "usage": self._usage_summary_for_api_request_hook(response),
            }
        )

    def _invoke_api_request_error_hook(
        self,
        *,
        task_id: str,
        turn_id: str,
        api_request_id: str,
        api_call_count: int,
        api_start_time: float,
        api_kwargs: dict[str, Any] | None,
        error_type: str,
        error_message: str,
        status_code: int | None = None,
        retry_count: int | None = None,
        max_retries: int | None = None,
        retryable: bool | None = None,
        reason: str | None = None,
    ) -> None:
        # Lazy module import (not from-import) so tests can replace lifecycle
        # dispatch at this call site. After first call the import is a
        # ``sys.modules`` dict lookup, so retries don't repay any real cost.
        try:
            from pcbdraft.interfaces.tui import lifecycle as _lifecycle

            if not _lifecycle.has_hook("api_request_error"):
                return
            ended_at = _now_hook()
            _lifecycle.invoke_hook(
                "api_request_error",
                task_id=task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                session_id=self.session_id or "",
                platform=self.platform or "",
                model=self.model,
                provider=self.provider,
                base_url=self.base_url,
                api_mode=self.api_mode,
                api_call_count=api_call_count,
                api_duration=ended_at - api_start_time,
                started_at=api_start_time,
                ended_at=ended_at,
                status_code=status_code,
                retry_count=retry_count,
                max_retries=max_retries,
                retryable=retryable,
                reason=reason,
                error={
                    "type": error_type,
                    "message": error_message,
                },
                request=self._api_request_payload_for_hook(api_kwargs),
            )
        except Exception:
            pass

    def _dump_api_request_debug(
        self,
        api_kwargs: dict[str, Any],
        *,
        reason: str,
        error: Exception | None = None,
    ) -> Path | None:
        """Forwarder — see ``agent.agent_runtime_helpers.dump_api_request_debug``."""
        from pcbdraft.agent.agent_runtime_helpers import dump_api_request_debug

        return dump_api_request_debug(self, api_kwargs, reason=reason, error=error)


def _default_is_simple_namespace(value: Any) -> bool:
    return isinstance(value, SimpleNamespace)


_normalize_usage_hook = normalize_usage
_env_get_hook = os.getenv
_is_simple_namespace_hook = _default_is_simple_namespace
_json_dumps_hook = json.dumps
_now_hook = time.time
