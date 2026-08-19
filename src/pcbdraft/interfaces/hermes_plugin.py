"""Hermes plugin body for the PCBDraft agent debug trace.

The plugin itself is installed by :func:`pcbdraft.interfaces.hermes_cli.install_debug_plugin`
into the PCBDraft-owned Hermes home (``plugins/pcbdraft-debug/``).  Its
``register`` function subscribes to the vendor Hermes lifecycle hooks and
forwards every conversation step to :mod:`pcbdraft.core.debug_trace`:

* ``on_session_start`` / ``on_session_end`` — session boundaries;
* ``pre_api_request`` — each model request (messages, tools, model, provider);
* ``post_api_request`` — each model response (reply text, tool calls, usage);
* ``api_request_error`` — each provider failure with retry metadata;
* ``pre_tool_call`` / ``post_tool_call`` — each tool call and its result;
* ``post_llm_call`` — the finalized turn reply.

Hook payloads evolve additively upstream, so every callback declares only the
keyword arguments it consumes (the Hermes dispatcher signature-inspects
callbacks and withholds undeclared additive fields).  All callbacks are pure
observers: they return ``None`` and never influence the agent loop.
"""

from __future__ import annotations

from typing import Any

__all__ = ("PLUGIN_NAME", "register")

PLUGIN_NAME = "pcbdraft-debug"


def _record(event: str, **fields: Any) -> None:
    from pcbdraft.core.debug_trace import record_event

    record_event(event, **fields)


def register(ctx: Any) -> None:
    """Wire the debug trace observer hooks onto one Hermes plugin context."""

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    _record("plugin_loaded", plugin=PLUGIN_NAME)


def _on_session_start(session_id: str, model: str, platform: str) -> None:
    _record("session_start", session_id=session_id, model=model, platform=platform)


def _on_session_end(
    session_id: str,
    turn_id: str,
    completed: bool,
    failed: bool,
    interrupted: bool,
    turn_exit_reason: str,
    model: str,
) -> None:
    _record(
        "session_end",
        session_id=session_id,
        turn_id=turn_id,
        completed=completed,
        failed=failed,
        interrupted=interrupted,
        turn_exit_reason=turn_exit_reason,
        model=model,
    )


def _on_pre_api_request(
    turn_id: str,
    api_request_id: str,
    session_id: str,
    api_call_count: int,
    model: str,
    provider: str,
    base_url: str,
    message_count: int,
    tool_count: int,
    approx_input_tokens: int,
    request: dict[str, Any],
    retry_count: int,
) -> None:
    _record(
        "model_request",
        turn_id=turn_id,
        api_request_id=api_request_id,
        session_id=session_id,
        api_call_count=api_call_count,
        model=model,
        provider=provider,
        base_url=base_url,
        message_count=message_count,
        tool_count=tool_count,
        approx_input_tokens=approx_input_tokens,
        retry_count=retry_count,
        request=request,
    )


def _on_post_api_request(
    turn_id: str,
    api_request_id: str,
    session_id: str,
    api_call_count: int,
    model: str,
    provider: str,
    api_duration: float,
    finish_reason: str,
    response: dict[str, Any],
    usage: dict[str, Any],
) -> None:
    _record(
        "model_response",
        turn_id=turn_id,
        api_request_id=api_request_id,
        session_id=session_id,
        api_call_count=api_call_count,
        model=model,
        provider=provider,
        api_duration_seconds=round(api_duration, 3),
        finish_reason=finish_reason,
        usage=usage,
        response=response,
    )


def _on_api_request_error(
    turn_id: str,
    api_request_id: str,
    session_id: str,
    api_call_count: int,
    model: str,
    provider: str,
    status_code: int,
    retry_count: int,
    max_retries: int,
    retryable: bool,
    reason: str,
    error: dict[str, Any],
    api_duration: float,
) -> None:
    _record(
        "model_error",
        turn_id=turn_id,
        api_request_id=api_request_id,
        session_id=session_id,
        api_call_count=api_call_count,
        model=model,
        provider=provider,
        http_status=status_code,
        retry_count=retry_count,
        max_retries=max_retries,
        retryable=retryable,
        failover_reason=reason,
        error=error,
        api_duration_seconds=round(api_duration, 3),
    )


def _on_pre_tool_call(
    tool_name: str,
    args: dict[str, Any],
    session_id: str,
    turn_id: str,
) -> None:
    _record(
        "tool_start",
        tool_name=tool_name,
        args=args,
        session_id=session_id,
        turn_id=turn_id,
    )


def _on_post_tool_call(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    session_id: str,
    tool_call_id: str,
    turn_id: str,
    duration_ms: int,
    status: str,
    error_type: str,
    error_message: str,
) -> None:
    _record(
        "tool_end",
        tool_name=tool_name,
        args=args,
        session_id=session_id,
        tool_call_id=tool_call_id,
        turn_id=turn_id,
        duration_ms=duration_ms,
        status=status,
        error_type=error_type,
        error_message=error_message,
        result=result,
    )


def _on_post_llm_call(
    session_id: str,
    turn_id: str,
    user_message: str,
    assistant_response: str,
    model: str,
) -> None:
    _record(
        "turn_complete",
        session_id=session_id,
        turn_id=turn_id,
        model=model,
        user_message=user_message,
        assistant_response=assistant_response,
    )
