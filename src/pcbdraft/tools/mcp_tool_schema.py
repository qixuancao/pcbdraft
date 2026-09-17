"""Stateless MCP tool schemas, names, filters, and lifecycle config parsing.

Connection ownership, MCP server tasks, registration, caching, and lifecycle
execution stay in :mod:`pcbdraft.tools.mcp_tool`. The compatibility module
passes its late-bound schema-normalizer and cross-SDK field-reader patch points
into conversion, so this module does not import it or create a circular
dependency.
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


def _normalize_mcp_input_schema(schema: dict | None) -> dict:
    """Normalize MCP input schemas for LLM tool-calling compatibility.

    MCP servers can emit plain JSON Schema with ``definitions`` /
    ``#/definitions/...`` references.  Kimi / Moonshot rejects that form and
    requires local refs to point into ``#/$defs/...`` instead.  Normalize the
    common draft-07 shape here so MCP tool schemas remain portable across
    OpenAI-compatible providers.

    Additional MCP-server robustness repairs applied recursively:

    * Missing or ``null`` ``type`` on an object-shaped node is coerced to
      ``"object"`` (some servers omit it).  See PR #4897.
    * When an ``object`` node lacks ``properties``, an empty ``properties``
      dict is added so ``required`` entries don't dangle.
    * ``required`` arrays are pruned to only names that exist in
      ``properties``; otherwise Google AI Studio / Gemini 400s with
      ``property is not defined``.  See PR #4651.
    * MCP/Pydantic optional fields commonly arrive as
      ``anyOf: [{...}, {"type": "null"}], default: null``.  Anthropic rejects
      nullable branches in tool input schemas, so nullable unions are collapsed
      to the non-null branch and optionality remains represented solely by the
      parent object's ``required`` list.

    All repairs are provider-agnostic and ideally produce a schema valid on
    OpenAI, Anthropic, Gemini, and Moonshot in one pass.
    """
    if not schema:
        return {"type": "object", "properties": {}}

    def _rewrite_local_refs(node):
        """Walk the schema, promoting legacy ``definitions`` to ``$defs``.

        The promotion is contextual: ``definitions`` is renamed only when it
        appears as a JSON Schema *meta-keyword* (sibling of ``properties`` /
        ``$ref`` at a schema node), never when it appears as the *name of a
        property* (i.e., as a key inside a ``properties`` dict).

        Without this gate, MCP servers that legitimately expose a tool
        parameter named ``definitions`` (e.g. a CI/pipelines tool that uses
        ``definitions`` for an array of pipeline-definition IDs) would have
        that user-facing property name silently rewritten to ``$defs``.
        Anthropic and OpenAI both reject ``$`` in property names
        (``^[a-zA-Z0-9_.-]{1,64}$``), so the whole tool array gets a 400 and
        every conversation breaks.

        The gate works by treating ``properties`` and ``patternProperties``
        specially during descent: we iterate the property-name -> schema map
        directly, leaving the property names verbatim, then recurse into each
        property's schema where ordinary JSON Schema semantics resume (so any
        legitimately-nested ``definitions`` meta-keyword inside a property's
        schema is still promoted).
        """
        if isinstance(node, dict):
            normalized = {}
            for key, value in node.items():
                if key in ("properties", "patternProperties") and isinstance(
                    value, dict
                ):
                    # Keys of this dict are user-facing property names, not
                    # meta-keywords. Preserve them verbatim; recurse only into
                    # each property's schema, where ``definitions`` again has
                    # its JSON Schema meaning.
                    normalized[key] = {
                        prop_name: _rewrite_local_refs(prop_schema)
                        for prop_name, prop_schema in value.items()
                    }
                else:
                    out_key = "$defs" if key == "definitions" else key
                    normalized[out_key] = _rewrite_local_refs(value)
            ref = normalized.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/definitions/"):
                normalized["$ref"] = "#/$defs/" + ref[len("#/definitions/") :]
            return normalized
        if isinstance(node, list):
            return [_rewrite_local_refs(item) for item in node]
        return node

    def _strip_nullable_union(node):
        """Collapse JSON Schema nullable unions to provider-safe non-null schemas.

        Delegates to ``tools.schema_sanitizer.strip_nullable_unions`` so MCP
        ingestion, the Anthropic guard, and the global sanitizer all share one
        implementation. Keeps the ``nullable: true`` hint so runtime argument
        coercion can still map a model-emitted ``"null"`` string to Python
        ``None`` for this optional field.
        """
        from pcbdraft.tools.schema_sanitizer import strip_nullable_unions

        return strip_nullable_unions(node, keep_nullable_hint=True)

    def _collapse_const_unions(node):
        """Collapse anyOf/oneOf unions of same-typed consts to property enums.

        Delegates to ``tools.schema_sanitizer.collapse_const_unions``. Runs
        AFTER the nullable strip: single-non-null unions are already collapsed
        by then, and unions of several const branches plus a null branch are
        handled here (consts -> enum, null -> ``nullable: true`` hint).
        Ported from block/goose tool_schema_normalize.rs (Apache-2.0).
        """
        from pcbdraft.tools.schema_sanitizer import collapse_const_unions

        return collapse_const_unions(node)

    def _repair_object_shape(node):
        """Recursively repair object-shaped nodes: fill type, prune required."""
        if isinstance(node, list):
            return [_repair_object_shape(item) for item in node]
        if not isinstance(node, dict):
            return node

        repaired = {k: _repair_object_shape(v) for k, v in node.items()}

        # Coerce missing / null type when the shape is clearly an object
        # (has properties or required but no type).
        if not repaired.get("type") and (
            "properties" in repaired or "required" in repaired
        ):
            repaired["type"] = "object"

        if repaired.get("type") == "object":
            # Ensure properties exists so required can reference it safely
            if "properties" not in repaired or not isinstance(
                repaired.get("properties"), dict
            ):
                repaired["properties"] = repaired.get("properties", {})
                if not isinstance(repaired.get("properties"), dict):
                    repaired["properties"] = {}

            # Prune required to only include names that exist in properties
            required = repaired.get("required")
            if isinstance(required, list):
                props = repaired.get("properties") or {}
                valid = [r for r in required if isinstance(r, str) and r in props]
                if len(valid) != len(required):
                    if valid:
                        repaired["required"] = valid
                    else:
                        repaired.pop("required", None)

        return repaired

    normalized = _rewrite_local_refs(schema)
    normalized = _strip_nullable_union(normalized)
    normalized = _collapse_const_unions(normalized)
    normalized = _repair_object_shape(normalized)

    # Ensure top-level is a well-formed object schema
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}

    return normalized


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
