"""Stateless MCP tool schemas, names, filters, and lifecycle config parsing.

Connection ownership, MCP server tasks, registration, caching, and lifecycle
execution stay in :mod:`pcbdraft.tools.mcp_tool`. The compatibility module
injects its schema normalizer and cross-SDK field reader into conversion so
this module does not import it or create a circular dependency.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Callable
from typing import Any

from pcbdraft.tools.ansi_strip import strip_unicode_tags

logger = logging.getLogger("pcbdraft.tools.mcp_tool")

MCP_TOOL_NAME_PREFIX = "mcp__"
_MCP_NAME_DELIM = "__"


def sanitize_mcp_name_component(value: str) -> str:
    """Return an MCP name component safe for tool and prefix generation."""

    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def mcp_prefixed_tool_name(
    server_name: str,
    tool_name: str,
    *,
    sanitizer: Callable[[str], str] | None = None,
    prefix: str = MCP_TOOL_NAME_PREFIX,
    delimiter: str = _MCP_NAME_DELIM,
) -> str:
    """Build ``mcp__<sanitizedServer>__<sanitizedTool>``."""

    sanitize = sanitizer or sanitize_mcp_name_component
    safe_server = sanitize(server_name)
    safe_tool = sanitize(tool_name)
    return f"{prefix}{safe_server}{delimiter}{safe_tool}"


def _convert_mcp_schema(
    server_name: str,
    mcp_tool: Any,
    *,
    schema_normalizer: Callable[[dict | None], dict],
    field_reader: Callable[..., Any],
    name_builder: Callable[[str, str], str] = mcp_prefixed_tool_name,
    description_sanitizer: Callable[[str], str] = strip_unicode_tags,
) -> dict[str, Any]:
    """Convert an MCP tool listing to the registry schema format."""

    prefixed_name = name_builder(server_name, mcp_tool.name)
    return {
        "name": prefixed_name,
        "description": description_sanitizer(
            mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}"
        ),
        "parameters": schema_normalizer(
            field_reader(mcp_tool, "input_schema", "inputSchema")
        ),
    }


def _build_utility_schemas(
    server_name: str,
    *,
    name_builder: Callable[[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """Build schemas for MCP resource and prompt utility tools."""

    build_name = name_builder or mcp_prefixed_tool_name
    return [
        {
            "schema": {
                "name": build_name(server_name, "list_resources"),
                "description": f"List available resources from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_resources",
        },
        {
            "schema": {
                "name": build_name(server_name, "read_resource"),
                "description": f"Read a resource by URI from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "URI of the resource to read",
                        },
                    },
                    "required": ["uri"],
                },
            },
            "handler_key": "read_resource",
        },
        {
            "schema": {
                "name": build_name(server_name, "list_prompts"),
                "description": f"List available prompts from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_prompts",
        },
        {
            "schema": {
                "name": build_name(server_name, "get_prompt"),
                "description": f"Get a prompt by name from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the prompt to retrieve",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Optional arguments to pass to the prompt",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name"],
                },
            },
            "handler_key": "get_prompt",
        },
    ]


def _normalize_name_filter(
    value: Any,
    label: str,
    *,
    warning: Callable[..., Any] | None = None,
) -> set[str]:
    """Normalize include/exclude config to exact or fnmatch-style patterns."""

    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    (warning or logger.warning)(
        "MCP config %s must be a string or list of strings; ignoring %r", label, value
    )
    return set()


def matches_name_filter(tool_name: str, patterns: set[str]) -> bool:
    """Return whether a tool name matches an exact name or case-sensitive glob."""

    if not patterns:
        return False
    if tool_name in patterns:
        return True
    return any(
        fnmatch.fnmatchcase(tool_name, pattern)
        for pattern in patterns
        if "*" in pattern or "?" in pattern or "[" in pattern
    )


def _parse_boolish(
    value: Any,
    default: bool = True,
    *,
    warning: Callable[..., Any] | None = None,
) -> bool:
    """Parse a bool-like config value with a safe fallback."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    (warning or logger.warning)(
        "MCP config expected a boolean-ish value, got %r; using default=%s",
        value,
        default,
    )
    return default


def _get_lifecycle_seconds(
    config: dict,
    key: str,
    *,
    warning: Callable[..., Any] | None = None,
) -> float | None:
    """Return an optional positive lifecycle timeout from top-level/nested config."""

    raw = config.get(key)
    lifecycle = config.get("lifecycle")
    if raw is None and isinstance(lifecycle, dict):
        raw = lifecycle.get(key)
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        (warning or logger.warning)(
            "MCP config %s must be a number of seconds; ignoring %r", key, raw
        )
        return None
    if seconds == 0:
        return None
    if seconds < 0:
        (warning or logger.warning)(
            "MCP config %s must be positive; ignoring %r", key, raw
        )
        return None
    return seconds
