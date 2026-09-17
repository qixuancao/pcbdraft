# Utility calls preserve the original fail-soft recovery behavior.
# ruff: noqa: BLE001
"""MCP resource and prompt utility handlers.

``mcp_tool`` remains the compatibility surface and injects its live namespace
so established monkeypatch paths remain effective. This module never imports
``mcp_tool`` and owns no generic tool-call, transport, configuration, discovery,
or authentication/session recovery implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_utility_handlers_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP utility handler runtime is not configured")
    return _runtime_namespace()


def _make_list_resources_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists resources from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _runtime()["_get_connected_server_for_call"](server_name)
        if not server or not server.session:
            return _runtime()["tool_error"](
                f"MCP server '{server_name}' is not connected"
            )

        async def _call():
            _runtime()["_mark_server_call_started"](server)
            async with server._rpc_lock:
                all_resources = await _runtime()["_paginate_full_list"](
                    server.session.list_resources, "resources", server_name
                )
            resources = []
            for r in all_resources:
                entry = {}
                if hasattr(r, "uri"):
                    entry["uri"] = str(r.uri)
                if hasattr(r, "name"):
                    entry["name"] = r.name
                if hasattr(r, "description") and r.description:
                    entry["description"] = r.description
                # Key stays camelCase — this dict is the tool's own JSON
                # output shape, not an SDK model.
                _mime = _runtime()["mcp_field"](r, "mime_type", "mimeType")
                if _mime:
                    entry["mimeType"] = _mime
                resources.append(entry)
            return _runtime()["json"].dumps(
                {"resources": resources}, ensure_ascii=False
            )

        def _call_once():
            return _runtime()["_run_on_mcp_loop"](_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _runtime()["_interrupted_call_result"]()
        except Exception as exc:
            recovered = _runtime()["_handle_auth_error_and_retry"](
                server_name,
                exc,
                _call_once,
                "resources/list",
            )
            if recovered is not None:
                return recovered
            recovered = _runtime()["_handle_session_expired_and_retry"](
                server_name,
                exc,
                _call_once,
                "resources/list",
            )
            if recovered is not None:
                return recovered
            _runtime()["logger"].error(
                "MCP %s/list_resources failed: %s",
                server_name,
                exc,
            )
            error_text = _runtime()["_exc_str"](exc)
            return _runtime()["tool_error"](
                _runtime()["_sanitize_error"](
                    f"MCP call failed: {type(exc).__name__}: {error_text}"
                )
            )

    return _handler


def _make_read_resource_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that reads a resource by URI from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _runtime()["_get_connected_server_for_call"](server_name)
        if not server or not server.session:
            return _runtime()["tool_error"](
                f"MCP server '{server_name}' is not connected"
            )

        uri = args.get("uri")
        if not uri:
            return _runtime()["tool_error"]("Missing required parameter 'uri'")

        async def _call():
            _runtime()["_mark_server_call_started"](server)
            async with server._rpc_lock:
                result = await server.session.read_resource(uri)
            # read_resource returns ReadResourceResult with .contents list
            parts: list[str] = []
            contents = result.contents if hasattr(result, "contents") else []
            for block in contents:
                if getattr(block, "text", None) is not None:
                    parts.append(_runtime()["strip_unicode_tags"](block.text))
                elif getattr(block, "blob", None) is not None:
                    # Materialize binary resource contents into the document
                    # cache instead of discarding them (same contract as
                    # EmbeddedResource blocks in tool results).
                    rendered = _runtime()["_render_mcp_resource_block"](
                        _runtime()["SimpleNamespace"](type="resource", resource=block),
                        server_name,
                    )
                    parts.append(rendered or f"[binary data, {len(block.blob)} bytes]")
            return _runtime()["json"].dumps(
                {"result": "\n".join(parts) if parts else ""}, ensure_ascii=False
            )

        def _call_once():
            return _runtime()["_run_on_mcp_loop"](_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _runtime()["_interrupted_call_result"]()
        except Exception as exc:
            recovered = _runtime()["_handle_auth_error_and_retry"](
                server_name,
                exc,
                _call_once,
                "resources/read",
            )
            if recovered is not None:
                return recovered
            recovered = _runtime()["_handle_session_expired_and_retry"](
                server_name,
                exc,
                _call_once,
                "resources/read",
            )
            if recovered is not None:
                return recovered
            _runtime()["logger"].error(
                "MCP %s/read_resource failed: %s",
                server_name,
                exc,
            )
            error_text = _runtime()["_exc_str"](exc)
            return _runtime()["tool_error"](
                _runtime()["_sanitize_error"](
                    f"MCP call failed: {type(exc).__name__}: {error_text}"
                )
            )

    return _handler


def _make_list_prompts_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists prompts from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _runtime()["_get_connected_server_for_call"](server_name)
        if not server or not server.session:
            return _runtime()["tool_error"](
                f"MCP server '{server_name}' is not connected"
            )

        async def _call():
            _runtime()["_mark_server_call_started"](server)
            async with server._rpc_lock:
                all_prompts = await _runtime()["_paginate_full_list"](
                    server.session.list_prompts, "prompts", server_name
                )
            prompts = []
            for p in all_prompts:
                entry = {}
                if hasattr(p, "name"):
                    entry["name"] = p.name
                if hasattr(p, "description") and p.description:
                    entry["description"] = p.description
                if hasattr(p, "arguments") and p.arguments:
                    entry["arguments"] = [
                        {
                            "name": a.name,
                            **(
                                {"description": a.description}
                                if hasattr(a, "description") and a.description
                                else {}
                            ),
                            **(
                                {"required": a.required}
                                if hasattr(a, "required")
                                else {}
                            ),
                        }
                        for a in p.arguments
                    ]
                prompts.append(entry)
            return _runtime()["json"].dumps({"prompts": prompts}, ensure_ascii=False)

        def _call_once():
            return _runtime()["_run_on_mcp_loop"](_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _runtime()["_interrupted_call_result"]()
        except Exception as exc:
            recovered = _runtime()["_handle_auth_error_and_retry"](
                server_name,
                exc,
                _call_once,
                "prompts/list",
            )
            if recovered is not None:
                return recovered
            recovered = _runtime()["_handle_session_expired_and_retry"](
                server_name,
                exc,
                _call_once,
                "prompts/list",
            )
            if recovered is not None:
                return recovered
            _runtime()["logger"].error(
                "MCP %s/list_prompts failed: %s",
                server_name,
                exc,
            )
            error_text = _runtime()["_exc_str"](exc)
            return _runtime()["tool_error"](
                _runtime()["_sanitize_error"](
                    f"MCP call failed: {type(exc).__name__}: {error_text}"
                )
            )

    return _handler


def _make_get_prompt_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that gets a prompt by name from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _runtime()["_get_connected_server_for_call"](server_name)
        if not server or not server.session:
            return _runtime()["tool_error"](
                f"MCP server '{server_name}' is not connected"
            )

        name = args.get("name")
        if not name:
            return _runtime()["tool_error"]("Missing required parameter 'name'")
        arguments = args.get("arguments", {})

        async def _call():
            _runtime()["_mark_server_call_started"](server)
            async with server._rpc_lock:
                result = await server.session.get_prompt(name, arguments=arguments)
            # GetPromptResult has .messages list
            messages = []
            for msg in result.messages if hasattr(result, "messages") else []:
                entry = {}
                if hasattr(msg, "role"):
                    entry["role"] = msg.role
                if hasattr(msg, "content"):
                    content = msg.content
                    if hasattr(content, "text"):
                        entry["content"] = _runtime()["strip_unicode_tags"](
                            content.text
                        )
                    elif isinstance(content, str):
                        entry["content"] = _runtime()["strip_unicode_tags"](content)
                    else:
                        entry["content"] = _runtime()["strip_unicode_tags"](
                            str(content)
                        )
                messages.append(entry)
            resp = {"messages": messages}
            if hasattr(result, "description") and result.description:
                resp["description"] = result.description
            return _runtime()["json"].dumps(resp, ensure_ascii=False)

        def _call_once():
            return _runtime()["_run_on_mcp_loop"](_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _runtime()["_interrupted_call_result"]()
        except Exception as exc:
            recovered = _runtime()["_handle_auth_error_and_retry"](
                server_name,
                exc,
                _call_once,
                "prompts/get",
            )
            if recovered is not None:
                return recovered
            recovered = _runtime()["_handle_session_expired_and_retry"](
                server_name,
                exc,
                _call_once,
                "prompts/get",
            )
            if recovered is not None:
                return recovered
            _runtime()["logger"].error(
                "MCP %s/get_prompt failed: %s",
                server_name,
                exc,
            )
            error_text = _runtime()["_exc_str"](exc)
            return _runtime()["tool_error"](
                _runtime()["_sanitize_error"](
                    f"MCP call failed: {type(exc).__name__}: {error_text}"
                )
            )

    return _handler
