"""Complete generic MCP tool-call transaction orchestration.

Connection lookup, authentication recovery, circuit-breaker state, server
objects, and registry ownership remain in ``mcp_tool``.  The compatibility
module injects its live namespace so historical monkeypatch paths and mutable
runtime state remain authoritative without a reverse import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_tool_call_runtime(*, namespace: Callable[[], dict[str, Any]]) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP tool-call runtime is not configured")
    return _runtime_namespace()


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """Return a sync handler that calls an MCP tool via the background loop.

    The handler conforms to the registry's dispatch interface:
    ``handler(args_dict, **kwargs) -> str``
    """

    def _handler(args: dict, **kwargs) -> str:
        runtime = _runtime()
        _trust_gate_check = runtime["_trust_gate_check"]
        _server_error_counts = runtime["_server_error_counts"]
        _CIRCUIT_BREAKER_THRESHOLD = runtime["_CIRCUIT_BREAKER_THRESHOLD"]
        _server_breaker_opened_at = runtime["_server_breaker_opened_at"]
        time = runtime["time"]
        _CIRCUIT_BREAKER_COOLDOWN_SEC = runtime["_CIRCUIT_BREAKER_COOLDOWN_SEC"]
        tool_error = runtime["tool_error"]
        _get_connected_server_for_call = runtime["_get_connected_server_for_call"]
        _bump_server_error = runtime["_bump_server_error"]
        _wait_for_server_session_ready = runtime["_wait_for_server_session_ready"]
        _signal_reconnect = runtime["_signal_reconnect"]
        _mark_server_call_started = runtime["_mark_server_call_started"]
        contextvars = runtime["contextvars"]
        mcp_field = runtime["mcp_field"]
        _sanitize_error = runtime["_sanitize_error"]
        _cache_mcp_image_block = runtime["_cache_mcp_image_block"]
        _cache_mcp_audio_block = runtime["_cache_mcp_audio_block"]
        _render_mcp_resource_block = runtime["_render_mcp_resource_block"]
        _strip_reserved_meta_keys = runtime["_strip_reserved_meta_keys"]
        strip_unicode_tags = runtime["strip_unicode_tags"]
        logger = runtime["logger"]
        json = runtime["json"]
        _run_on_mcp_loop = runtime["_run_on_mcp_loop"]
        _reset_server_error = runtime["_reset_server_error"]
        _interrupted_call_result = runtime["_interrupted_call_result"]
        _handle_auth_error_and_retry = runtime["_handle_auth_error_and_retry"]
        _handle_session_expired_and_retry = runtime["_handle_session_expired_and_retry"]
        _exc_str = runtime["_exc_str"]
        # Trust-tier gate (security boundary): write-capable tools on
        # servers configured ``trust: untrusted`` must be approved by the
        # user before ANY transport work happens — including the lazy
        # first-use spawn below. A denied call never touches the server.
        gate_error = _trust_gate_check(server_name, tool_name)
        if gate_error is not None:
            return gate_error

        # Circuit breaker: if this server has failed too many times
        # consecutively, short-circuit with a clear message so the model
        # stops retrying and uses alternative approaches (#10447).
        #
        # Once the cooldown elapses, the breaker transitions to
        # half-open: we let the *next* call through as a probe. On
        # success the success-path below resets the breaker; on
        # failure the error paths below bump the count again, which
        # re-stamps the open-time via _bump_server_error (re-arming
        # the cooldown).
        if _server_error_counts.get(server_name, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
            opened_at = _server_breaker_opened_at.get(server_name, 0.0)
            age = time.monotonic() - opened_at
            if age < _CIRCUIT_BREAKER_COOLDOWN_SEC:
                remaining = max(1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - age))
                return tool_error(
                    f"MCP server '{server_name}' is unreachable after "
                    f"{_server_error_counts[server_name]} consecutive "
                    f"failures. Auto-retry available in ~{remaining}s. "
                    f"Do NOT retry this tool yet — use alternative "
                    f"approaches or ask the user to check the MCP server."
                )
            # Cooldown elapsed → fall through as a half-open probe.

        server = _get_connected_server_for_call(server_name)
        if not server:
            _bump_server_error(server_name)
            return tool_error(f"MCP server '{server_name}' is not connected")

        if not server.session:
            # No live session. A reconnect may already be completing (the
            # transport swaps in a fresh session object asynchronously) —
            # wait briefly before treating this as a failure, so a
            # transient reconnect window doesn't burn a circuit-breaker
            # strike (#26892).
            if _wait_for_server_session_ready(
                server,
                timeout=min(5.0, float(tool_timeout or 5.0)),
            ):
                pass  # Fresh session arrived; proceed below.
            else:
                # Still down — the server task is reconnecting, or it has
                # exhausted its retry budget and parked (e.g. a dead stdio
                # subprocess). Probing here would write into a dead/absent
                # transport and re-arm the breaker forever (#16788). Instead,
                # ask the (always-present) server task to rebuild the
                # transport — which respawns a dead stdio subprocess — and
                # return a clean "reconnecting" error so the model backs off
                # without burning iterations. The breaker resets once the
                # fresh session initializes (_run_stdio/_run_http call
                # _reset_server_error).
                _bump_server_error(server_name)
                if _signal_reconnect(server):
                    return tool_error(
                        f"MCP server '{server_name}' transport is down; "
                        f"reconnect requested. Do NOT retry this tool "
                        f"immediately — give it a few seconds to come back."
                    )
                return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            _mark_server_call_started(server)
            async with server._rpc_lock:
                # Snapshot the agent's context so an elicitation callback
                # triggered during this call (fired on the MCP recv loop
                # task, which doesn't inherit our contextvars) can replay
                # it and detect the gateway platform / session for routing.
                server._pending_call_context = contextvars.copy_context()
                try:
                    result = await server.session.call_tool(tool_name, arguments=args)
                finally:
                    server._pending_call_context = None
            # The RPC round-trip completed — the session is demonstrably
            # healthy at the transport level (even if the tool itself
            # returned isError). Clear the rapid-drop budget (#62212).
            _mark_proven = getattr(server, "_mark_session_proven", None)
            if _mark_proven is not None:
                _mark_proven()
            # MCP CallToolResult has .content (list of content blocks) and
            # .is_error (.isError before mcp 2.0)
            if mcp_field(result, "is_error", "isError", False):
                error_text = ""
                for block in result.content or []:
                    if getattr(block, "text", None):
                        error_text += block.text
                        continue
                    # EmbeddedResource blocks inside error payloads carry
                    # their text under .resource.text — previously dropped,
                    # leaving a bare "MCP tool returned an error".
                    res_text = getattr(getattr(block, "resource", None), "text", None)
                    if res_text:
                        error_text += str(res_text)
                return tool_error(
                    _sanitize_error(error_text or "MCP tool returned an error")
                )

            # Collect text from content blocks. MCP tool results can also
            # include ImageContent blocks (screenshot / Blockbench / Playwright
            # etc.); cache those via the gateway's image-cache helper so they
            # flow through Hermes' MEDIA: tag convention and out to messaging
            # adapters that render images natively. Without this, image blocks
            # were silently dropped and the agent got an empty response.
            #
            # Distilled from #17915 (c3115644151) and #10848 (gnanirahulnutakki),
            # both too stale to cherry-pick. #10848's approach (integrate with
            # Hermes' MEDIA tag + cache_image_from_bytes) was the cleaner of
            # the two — plugs into existing infrastructure.
            parts: list[str] = []
            for block in result.content or []:
                if hasattr(block, "text") and block.text:
                    parts.append(strip_unicode_tags(block.text))
                    continue
                image_tag = _cache_mcp_image_block(block)
                if image_tag:
                    parts.append(image_tag)
                    continue
                audio_tag = _cache_mcp_audio_block(block)
                if audio_tag:
                    parts.append(audio_tag)
                    continue
                # ResourceLink / EmbeddedResource blocks (PDFs, archives,
                # office docs, ...). Previously these were silently dropped,
                # so document-oriented MCP tools appeared to return metadata
                # only (enterprise customer report, 2026-07).
                resource_text = _render_mcp_resource_block(block, server_name)
                if resource_text:
                    parts.append(resource_text)
                    continue
                # Benign empty renders (empty text blocks, empty text
                # resources, audio in a process without the gateway cache)
                # aren't data loss — log at debug. Warn only for genuinely
                # unrecognized block shapes.
                block_type = getattr(block, "type", None) or type(block).__name__
                if block_type in {"text", "resource", "audio", "image"}:
                    logger.debug(
                        "MCP %s: content block type %r rendered empty",
                        server_name,
                        block_type,
                    )
                else:
                    logger.warning(
                        "MCP %s: dropping unsupported content block type %r",
                        server_name,
                        block_type,
                    )
            text_result = "\n".join(parts) if parts else ""

            # Combine content + structuredContent when both are present.
            # MCP spec: content is model-oriented (text), structuredContent
            # is machine-oriented (JSON metadata).  For an AI agent, content
            # is the primary payload; structuredContent supplements it.
            #
            # Server-level `_meta` is also surfaced (ported from
            # MoonshotAI/kimi-code#2596): servers return namespaced metadata
            # there (validated contracts, browser-handoff payloads, ...) that
            # was previously invisible to the agent. Protocol-reserved keys
            # are dropped first (kimi-code#2600) — per the MCP spec's key-name
            # rules a prefix is reserved when a `modelcontextprotocol` or
            # `mcp` label is followed by at least one more label (e.g.
            # `modelcontextprotocol.io/...`, `tools.mcp.com/...`); those carry
            # host/protocol plumbing, not model-facing data. Unprefixed and
            # vendor-namespaced keys (`com.example.mcp/...`) pass through —
            # their semantics belong to the server.
            structured = mcp_field(result, "structured_content", "structuredContent")
            meta = _strip_reserved_meta_keys(mcp_field(result, "meta", "meta"))
            if structured is not None or meta is not None:
                payload: dict[str, Any] = {}
                if text_result:
                    payload["result"] = text_result
                if structured is not None:
                    if text_result:
                        payload["structuredContent"] = structured
                    else:
                        payload["result"] = structured
                if meta is not None:
                    payload["_meta"] = meta
                if "result" not in payload:
                    payload["result"] = text_result
                try:
                    return json.dumps(payload, ensure_ascii=False)
                except (TypeError, ValueError):
                    # Non-serializable metadata: drop the extras rather than
                    # failing the whole tool call.
                    return json.dumps({"result": text_result}, ensure_ascii=False)
            return json.dumps({"result": text_result}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            result = _call_once()
            # Check if the MCP tool itself returned an error
            try:
                parsed = json.loads(result)
                if "error" in parsed:
                    _bump_server_error(server_name)
                else:
                    _reset_server_error(server_name)  # success — reset
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)  # non-JSON = success
            return result
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:  # noqa: BLE001 - provider transports vary
            # Auth-specific recovery path: consult the manager, signal
            # reconnect if viable, retry once. Returns None to fall
            # through for non-auth exceptions.
            recovered = _handle_auth_error_and_retry(
                server_name,
                exc,
                _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            # Transport session expiry (#13383): same reconnect flow
            # but skips OAuth recovery because the access token is
            # still valid — only the server-side session is stale.
            recovered = _handle_session_expired_and_retry(
                server_name,
                exc,
                _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            _bump_server_error(server_name)
            logger.error(
                "MCP tool %s/%s call failed: %s",
                server_name,
                tool_name,
                exc,
            )
            return tool_error(
                _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
                )
            )

    return _handler
