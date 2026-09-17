"""Pure MCP protocol error, redaction, and description-scan policy.

``mcp_tool`` remains the compatibility surface and injects its live namespace
so established constant and logger monkeypatch paths remain effective. This
module never imports ``mcp_tool`` and owns no authentication recovery,
connection lifecycle, RPC execution, or server state.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"
    r"|sk-[A-Za-z0-9_]{1,255}"
    r"|Bearer\s+\S+"
    r"|token=[^\s&,;\"']{1,255}"
    r"|key=[^\s&,;\"']{1,255}"
    r"|API_KEY=[^\s&,;\"']{1,255}"
    r"|password=[^\s&,;\"']{1,255}"
    r"|secret=[^\s&,;\"']{1,255}"
    r")",
    re.IGNORECASE,
)

_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION = -32022

_MCP_INJECTION_PATTERNS = [
    (
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        "prompt override attempt ('ignore previous instructions')",
    ),
    (
        re.compile(r"you\s+are\s+now\s+a", re.IGNORECASE),
        "identity override attempt ('you are now a...')",
    ),
    (
        re.compile(r"your\s+new\s+(task|role|instructions?)\s+(is|are)", re.IGNORECASE),
        "task override attempt",
    ),
    (re.compile(r"system\s*:\s*", re.IGNORECASE), "system prompt injection attempt"),
    (
        re.compile(r"<\s*(system|human|assistant)\s*>", re.IGNORECASE),
        "role tag injection attempt",
    ),
    (
        re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.IGNORECASE),
        "concealment instruction",
    ),
    (
        re.compile(r"(curl|wget|fetch)\s+https?://", re.IGNORECASE),
        "network command in description",
    ),
    (
        re.compile(r"base64\.(b64decode|decodebytes)", re.IGNORECASE),
        "base64 decode reference",
    ),
    (re.compile(r"exec\s*\(|eval\s*\(", re.IGNORECASE), "code execution reference"),
    (
        re.compile(r"import\s+(subprocess|os|shutil|socket)", re.IGNORECASE),
        "dangerous import reference",
    ),
]

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_protocol_policy_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP protocol policy runtime is not configured")
    return _runtime_namespace()


def _sanitize_error(text: str) -> str:
    """Strip credential-like patterns from model-visible error text."""

    return _runtime()["_CREDENTIAL_PATTERN"].sub("[REDACTED]", text)


def _exc_str(exc: BaseException) -> str:
    """Return a non-empty human-readable string for an exception."""

    text = str(exc).strip()
    return text if text else repr(exc)


def _handshake_rejected_as_modern(exc: BaseException) -> bool:
    """Return whether legacy initialization was rejected by a modern server."""

    runtime = _runtime()
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None) or getattr(exc, "code", None)
    if code in (
        runtime["_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION"],
        runtime["_JSONRPC_METHOD_NOT_FOUND"],
    ):
        return True
    message = str(exc).lower()
    if not message:
        return False
    return (
        "unsupported protocol version" in message
        or str(runtime["_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION"]) in message
        or runtime["_is_method_not_found_error"](exc)
    )


def _is_method_not_found_error(exc: BaseException) -> bool:
    """Return whether an exception represents JSON-RPC method-not-found."""

    method_not_found = _runtime()["_JSONRPC_METHOD_NOT_FOUND"]
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    if code == method_not_found:
        return True
    message = str(exc).lower()
    if not message:
        return False
    return (
        str(method_not_found) in message
        or "method not found" in message
        or "unknown method" in message
        or "not found: ping" in message
    )


def _scan_mcp_description(
    server_name: str, tool_name: str, description: str
) -> list[str]:
    """Return and log policy findings in an MCP tool description."""

    findings = []
    if not description:
        return findings
    runtime = _runtime()
    for pattern, reason in runtime["_MCP_INJECTION_PATTERNS"]:
        if pattern.search(description):
            findings.append(reason)
    if findings:
        runtime["logger"].warning(
            "MCP server '%s' tool '%s': suspicious description content — %s. "
            "Description: %.200s",
            server_name,
            tool_name,
            "; ".join(findings),
            description,
        )
    return findings
