"""Normalize provider errors into safe, concise user-facing details."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.redact import redact_sensitive_text

_coerce_api_error_detail_hook: Callable[[Any], str]
_decorate_xai_entitlement_error_hook: Callable[[str], str]
_json_dumps_hook: Callable[..., str]
_json_loads_hook: Callable[[str], Any]
_redact_sensitive_text_hook: Callable[[str], str]
_regex_search_hook: Callable[..., Any]


def configure_error_normalization_runtime(
    *,
    coerce_api_error_detail: Callable[[Any], str] | None = None,
    decorate_xai_entitlement_error: Callable[[str], str] | None = None,
    json_dumps: Callable[..., str] | None = None,
    json_loads: Callable[[str], Any] | None = None,
    redact_text: Callable[[str], str] | None = None,
    regex_search: Callable[..., Any] | None = None,
) -> None:
    """Inject helpers exposed through the legacy agent module."""
    global _coerce_api_error_detail_hook
    global _decorate_xai_entitlement_error_hook
    global _json_dumps_hook
    global _json_loads_hook
    global _redact_sensitive_text_hook
    global _regex_search_hook

    if coerce_api_error_detail is not None:
        _coerce_api_error_detail_hook = coerce_api_error_detail
    if decorate_xai_entitlement_error is not None:
        _decorate_xai_entitlement_error_hook = decorate_xai_entitlement_error
    if json_dumps is not None:
        _json_dumps_hook = json_dumps
    if json_loads is not None:
        _json_loads_hook = json_loads
    if redact_text is not None:
        _redact_sensitive_text_hook = redact_text
    if regex_search is not None:
        _regex_search_hook = regex_search


class ErrorNormalizationMixin:
    """Classify entitlement failures and summarize provider exceptions."""

    @staticmethod
    def _is_entitlement_failure(
        error_context: dict[str, Any] | None,
        status_code: int | None,
    ) -> bool:
        """Detect subscription failures that masquerade as auth failures."""
        if status_code not in {401, 403, None}:
            return False
        if not isinstance(error_context, dict):
            return False
        message = str(error_context.get("message") or "").lower()
        reason = str(error_context.get("reason") or "").lower()
        code = str(error_context.get("code") or "").lower()
        error = str(error_context.get("error") or "").lower()
        haystack = f"{message} {reason} {code} {error}"
        if not haystack.strip():
            return False
        if "[wke=unauthenticated:" in haystack:
            return False
        if "oauth2 access token could not be validated" in haystack:
            return False
        if "do not have an active grok subscription" in haystack:
            return True
        if "out of available resources" in haystack and "grok" in haystack:
            return True
        return bool("does not have permission" in haystack and "grok" in haystack)

    @staticmethod
    def _decorate_xai_entitlement_error(detail: str) -> str:
        """Append guidance to xAI OAuth entitlement failures once."""
        if not detail:
            return detail
        lower = detail.lower()
        is_entitlement = (
            "do not have an active grok subscription" in lower
            or ("out of available resources" in lower and "grok" in lower)
            or ("does not have permission" in lower and "grok" in lower)
        )
        if not is_entitlement:
            return detail
        hint = (
            " — xAI rejected this OAuth account. NOTE: X Premium+ does NOT "
            "include xAI API access — only standalone SuperGrok subscribers "
            "can use this provider. Other possible causes: no Grok "
            "subscription, your tier doesn't include this model, or your "
            "quota is exhausted. Check https://grok.com/?_s=usage to see "
            "which, or run `/model` to switch providers."
        )
        if "X Premium+ does NOT include" in detail:
            return detail
        return f"{detail}{hint}"

    @staticmethod
    def _coerce_api_error_detail(value: Any) -> str:
        """Return a display-safe string for structured provider error fields."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("message", "detail", "error", "code", "type"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested
            for key in ("message", "detail", "error", "code", "type"):
                if key in value:
                    nested_detail = _coerce_api_error_detail_hook(value[key])
                    if nested_detail:
                        return nested_detail
            try:
                return _json_dumps_hook(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            except TypeError:
                return str(value)
        if isinstance(value, (list, tuple)):
            parts = [_coerce_api_error_detail_hook(item) for item in value]
            return "; ".join(part for part in parts if part)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _summarize_api_error(error: Exception) -> str:
        """Extract a human-readable one-line summary from an API error."""
        raw = str(error)

        network_resolution_markers = (
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname provided, or not known",
            "getaddrinfo failed",
            "no address associated with hostname",
            "network is unreachable",
        )
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if any(
                marker in str(current).lower() for marker in network_resolution_markers
            ):
                return (
                    "PCBDraft can't reach the model provider. You may be offline. "
                    "Check your internet connection and try again."
                )
            current = current.__cause__ or current.__context__

        if isinstance(error, ValueError) and "expected ident at line" in raw.lower():
            return f"Malformed provider streaming response: {raw[:300]}"

        if "<!DOCTYPE" in raw or "<html" in raw:
            match = _regex_search_hook(
                r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE
            )
            title = (
                match.group(1).strip() if match else "HTML error page (title not found)"
            )
            ray = _regex_search_hook(
                r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw
            )
            ray_id = ray.group(1).strip() if ray else None
            status_code = getattr(error, "status_code", None)
            parts = []
            if status_code:
                parts.append(f"HTTP {status_code}")
            parts.append(title)
            if ray_id:
                parts.append(f"Ray {ray_id}")
            return " — ".join(parts)

        if type(error).__name__ == "GeminiAPIError":
            return _redact_sensitive_text_hook(raw[:1000])

        body = getattr(error, "body", None)
        if isinstance(body, dict):
            message = (
                body.get("error", {}).get("message")
                if isinstance(body.get("error"), dict)
                else body.get("message")
            )
            if message:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                message = _coerce_api_error_detail_hook(message)
                return _decorate_xai_entitlement_error_hook(f"{prefix}{message[:300]}")

        response = getattr(error, "response", None)
        if response is not None:
            try:
                snippet = (getattr(response, "text", None) or "").strip()
            except Exception:  # noqa: BLE001 - response access is provider-defined
                snippet = ""
            if snippet:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                try:
                    payload = _json_loads_hook(snippet)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    nested_error = payload.get("error")
                    if isinstance(nested_error, dict) and nested_error.get("message"):
                        return _redact_sensitive_text_hook(
                            f"{prefix}{str(nested_error['message'])[:300]}"
                        )
                    if payload.get("message"):
                        return _redact_sensitive_text_hook(
                            f"{prefix}{str(payload['message'])[:300]}"
                        )
                return _redact_sensitive_text_hook(f"{prefix}{snippet[:300]}")

        status_code = getattr(error, "status_code", None)
        prefix = f"HTTP {status_code}: " if status_code else ""
        return _decorate_xai_entitlement_error_hook(f"{prefix}{raw[:500]}")

    def _mask_api_key_for_logs(self, key: Any) -> str | None:
        """Mask credential strings without invoking callable token providers."""
        if callable(key) and not isinstance(key, str):
            return "<entra-id-bearer>"
        if not key:
            return None
        if len(key) <= 12:
            return "***"
        return f"{key[:8]}...{key[-4:]}"

    def _clean_error_message(self, error_msg: str) -> str:
        """Remove HTML and excessive whitespace from user-facing errors."""
        if not error_msg:
            return "Unknown error"
        if error_msg.strip().startswith("<!DOCTYPE html") or "<html" in error_msg:
            return "Service temporarily unavailable (HTML error page returned)"
        cleaned = " ".join(error_msg.split())
        if len(cleaned) > 150:
            cleaned = cleaned[:150] + "..."
        return cleaned

    @staticmethod
    def _extract_api_error_context(error: Exception) -> dict[str, Any]:
        """Forward to the shared structured API error extractor."""
        from pcbdraft.agent.agent_runtime_helpers import extract_api_error_context

        return extract_api_error_context(error)


_coerce_api_error_detail_hook = ErrorNormalizationMixin._coerce_api_error_detail
_decorate_xai_entitlement_error_hook = (
    ErrorNormalizationMixin._decorate_xai_entitlement_error
)
_json_dumps_hook = json.dumps
_json_loads_hook = json.loads
_redact_sensitive_text_hook = redact_sensitive_text
_regex_search_hook = re.search
