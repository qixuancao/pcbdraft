# Elicitation is deliberately fail-closed across approval backend failures.
# ruff: noqa: BLE001, TRY401
"""MCP elicitation callback handling.

``mcp_tool`` remains the compatibility surface and injects its live namespace
so established monkeypatch paths and lazily loaded SDK types remain effective.
This module owns no connection, authentication, registration, or server state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_elicitation_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP elicitation runtime is not configured")
    return _runtime_namespace()


def _format_elicitation_schema_summary(schema: dict, server_name: str) -> str:
    """Render a requested schema to a human-readable field list."""

    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"Approval requested by MCP server '{server_name}'."

    lines = [f"Fields requested by MCP server '{server_name}':"]
    for field_name, field_spec in props.items():
        field_type = ""
        field_desc = ""
        if isinstance(field_spec, dict):
            field_type = str(field_spec.get("type", "") or "")
            field_desc = str(field_spec.get("description", "") or "")
        suffix = f" ({field_type})" if field_type else ""
        if field_desc:
            lines.append(f"  - {field_name}{suffix}: {field_desc}")
        else:
            lines.append(f"  - {field_name}{suffix}")
    return "\n".join(lines)


class ElicitationHandler:
    """Handle ``elicitation/create`` requests for one MCP server."""

    # Outer cap for the approval await. The approval implementation has its
    # own timeout; this keeps the MCP event loop bounded if that is bypassed.
    _OUTER_TIMEOUT_GRACE_SECONDS = 5

    def __init__(
        self,
        server_name: str,
        config: dict,
        owner: Any | None = None,
    ):
        _safe_numeric = _runtime()["_safe_numeric"]
        self.server_name = server_name
        # Default 5 min mirrors the gateway approval default for async users.
        self.timeout = _safe_numeric(config.get("timeout", 300), 300, float)
        # Owner supplies the agent context snapshot captured by the tool call.
        self.owner = owner
        self.metrics = {
            "requests": 0,
            "accepted": 0,
            "declined": 0,
            "errors": 0,
        }

    def session_kwargs(self) -> dict:
        """Return kwargs to pass to ClientSession for elicitation support."""

        return {"elicitation_callback": self}

    async def __call__(self, context, params):
        """Run the SDK elicitation callback with fail-closed outcomes."""

        runtime = _runtime()
        logger = runtime["logger"]
        ElicitResult = runtime["ElicitResult"]
        _format_elicitation_schema_summary = runtime[
            "_format_elicitation_schema_summary"
        ]
        _sanitize_error = runtime["_sanitize_error"]
        asyncio = runtime["asyncio"]

        self.metrics["requests"] += 1

        # URL mode needs a browser completion flow, which is not implemented.
        mode = getattr(params, "mode", "form")
        if mode == "url":
            logger.info(
                "MCP server '%s' requested URL-mode elicitation; "
                "declining (URL-mode elicitation not implemented)",
                self.server_name,
            )
            self.metrics["declined"] += 1
            return ElicitResult(action="decline")

        message = getattr(params, "message", "") or (
            f"MCP server '{self.server_name}' is requesting your approval"
        )
        # MCP 1.x uses requestedSchema; 2.x exposes requested_schema.
        schema = (
            getattr(params, "requestedSchema", None)
            or getattr(params, "requested_schema", None)
            or {}
        )
        description = _format_elicitation_schema_summary(schema, self.server_name)

        logger.info(
            "MCP server '%s' elicitation request: %s",
            self.server_name,
            _sanitize_error(message)[:200],
        )

        try:
            from pcbdraft.tools.approval import request_elicitation_consent
        except Exception as exc:  # pragma: no cover -- defensive
            logger.error(
                "MCP server '%s' elicitation: approval system unavailable: %s",
                self.server_name,
                exc,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        # The SDK receive task does not inherit the initiating agent context.
        # Replay the snapshot captured by the tool wrapper when available.
        captured = (
            getattr(self.owner, "_pending_call_context", None) if self.owner else None
        )

        def _invoke_consent() -> str:
            if captured is None:
                return request_elicitation_consent(
                    message,
                    description,
                    timeout_seconds=int(self.timeout),
                    surface=f"mcp-elicitation/{self.server_name}",
                )
            return captured.copy().run(
                request_elicitation_consent,
                message,
                description,
                timeout_seconds=int(self.timeout),
                surface=f"mcp-elicitation/{self.server_name}",
            )

        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(_invoke_consent),
                timeout=self.timeout + self._OUTER_TIMEOUT_GRACE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "MCP server '%s' elicitation timed out after %ds",
                self.server_name,
                int(self.timeout),
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        except Exception as exc:
            logger.exception(
                "MCP server '%s' elicitation failed: %s",
                self.server_name,
                exc,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        if answer == "accept":
            self.metrics["accepted"] += 1
            return ElicitResult(action="accept", content={})
        if answer == "cancel":
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        self.metrics["declined"] += 1
        return ElicitResult(action="decline")
