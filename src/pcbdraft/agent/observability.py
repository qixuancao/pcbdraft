"""Built-in conversation trace, schema projection, and PCB write constraints.

These lifecycle handlers run regardless of external plugin configuration.
They retain project/revision binding, bounded tool budgets and a single PCB
write per model decision while permitting parallel inspection.
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ("PLUGIN_NAME", "register")

PLUGIN_NAME = "pcbdraft-debug"
_MAX_DECISION_KEYS = 2048
_PCB_TOOL_CALL_LIMIT_ENV = "PCBDRAFT_PCB_TOOL_CALL_LIMIT"
_decision_lock = threading.Lock()
_pcb_write_decisions: OrderedDict[tuple[str, str, str], None] = OrderedDict()
_pcb_tool_call_counts: dict[str, int] = {}
_pcb_tool_budget_exhausted: set[str] = set()
_context_lock = threading.Lock()


@dataclass(frozen=True)
class _ContextSnapshot:
    turn_id: str
    active_tokens: int
    active_bytes: int


_context_snapshots: dict[str, _ContextSnapshot] = {}


def _record(event: str, **fields: Any) -> None:
    from pcbdraft.core.debug_trace import record_event

    record_event(event, **fields)


def register(ctx: Any) -> None:
    """Wire the debug trace observer hooks onto one Hermes plugin context."""

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_middleware("llm_request", _project_pcb_tool_schemas)
    ctx.register_middleware("tool_execution", _one_pcb_write_per_decision)
    _record("plugin_loaded", plugin=PLUGIN_NAME)


def _clear_session_decisions(session_id: str) -> None:
    with _decision_lock:
        for key in tuple(_pcb_write_decisions):
            if key[0] == session_id:
                _pcb_write_decisions.pop(key, None)
        _pcb_tool_call_counts.pop(session_id, None)
        _pcb_tool_budget_exhausted.discard(session_id)
    with _context_lock:
        _context_snapshots.pop(session_id, None)


def _provider_tool_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    function = value.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
    else:
        name = value.get("name")
    return name if isinstance(name, str) and name else None


def _canonical_provider_schema(value: Mapping[str, Any], spec: Any) -> dict[str, Any]:
    """Replace one provider declaration with its canonical registry schema."""

    projected = dict(value)
    function = value.get("function")
    if isinstance(function, Mapping):
        projected["function"] = {
            **dict(function),
            "name": spec.external_name,
            "description": spec.protocol_description,
            "parameters": spec.input_schema,
        }
    elif "input_schema" in value:
        projected.update(
            {
                "name": spec.external_name,
                "description": spec.protocol_description,
                "input_schema": spec.input_schema,
            }
        )
    else:
        projected.update(
            {
                "name": spec.external_name,
                "description": spec.protocol_description,
                "parameters": spec.input_schema,
                "strict": True,
            }
        )
    return projected


def _project_pcb_tool_schemas(
    request: dict[str, Any], session_id: str, **_kwargs: Any
) -> dict[str, Any]:
    """Filter provider schemas by current native evidence, never execution authority."""

    from pcbdraft.agent.tool_bindings import (
        ModelToolProjection,
        get_current_project_id,
        get_session_project_id,
        model_tool_projection,
    )
    from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY

    tools = request.get("tools")
    if not isinstance(tools, list):
        return {"request": request, "source": "pcbdraft_stage_projection"}
    try:
        projection = model_tool_projection(session_id)
    except Exception:  # noqa: BLE001 - Hermes request middleware otherwise fails open
        project_id = get_session_project_id(session_id) or get_current_project_id()
        projection = ModelToolProjection(
            None,
            project_id,
            None,
            None,
            DEFAULT_PCB_TOOL_REGISTRY.projected_specs(
                None, project_bound=project_id is not None
            ),
        )
        _record(
            "tool_schema_projection_fallback",
            session_id=session_id,
            project_id=project_id,
            reason="stage_evidence_unavailable",
        )
    allowed = {spec.external_name: spec for spec in projection.specs}
    filtered: list[Any] = []
    pcb_before = 0
    pcb_after = 0
    for item in tools:
        name = _provider_tool_name(item)
        if name is None or not name.startswith("pcb_"):
            filtered.append(item)
            continue
        pcb_before += 1
        spec = allowed.get(name)
        if spec is None or not isinstance(item, Mapping):
            continue
        filtered.append(_canonical_provider_schema(item, spec))
        pcb_after += 1
    projected_request = dict(request)
    projected_request["tools"] = filtered
    _record(
        "tool_schema_projection",
        session_id=session_id,
        project_id=projection.project_id,
        stage=projection.stage or "unknown",
        live_revision=projection.live_revision,
        design_revision=projection.design_revision,
        pcb_tool_count_before=pcb_before,
        pcb_tool_count_after=pcb_after,
    )
    return {
        "request": projected_request,
        "source": "pcbdraft_stage_projection",
        "reason": projection.stage or "unknown",
    }


def _one_pcb_write_per_decision(
    tool_name: str,
    args: dict[str, Any],
    next_call: Callable[[dict[str, Any]], Any],
    session_id: str,
    turn_id: str,
    api_request_id: str,
    **_kwargs: Any,
) -> Any:
    """Dispatch any reads and at most one PCB write per provider response."""

    if not tool_name.startswith("pcb_"):
        return next_call(args)
    from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
    from pcbdraft.core.errors import ValidationError

    try:
        is_read = DEFAULT_PCB_TOOL_REGISTRY.resolve(tool_name).effect == "read"
    except ValidationError:
        payload = {
            "tool": tool_name,
            "success": False,
            "blocked": True,
            "policy": "closed_pcb_toolbox",
            "error": "Unknown PCB tools are blocked before dispatch.",
        }
        _record(
            "tool_policy_blocked",
            tool_name=tool_name,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            policy=payload["policy"],
        )
        return json.dumps(payload, ensure_ascii=False)
    raw_limit = os.environ.get(_PCB_TOOL_CALL_LIMIT_ENV)
    limit = int(raw_limit) if raw_limit is not None and raw_limit.isdecimal() else None
    if limit is not None and limit > 0:
        session_key = str(session_id)
        with _decision_lock:
            consumed = _pcb_tool_call_counts.get(session_key, 0)
            budget_blocked = consumed >= limit
            if budget_blocked:
                _pcb_tool_budget_exhausted.add(session_key)
            else:
                consumed += 1
                _pcb_tool_call_counts[session_key] = consumed
        if budget_blocked:
            payload = {
                "tool": tool_name,
                "success": False,
                "blocked": True,
                "policy": "pcb_tool_call_budget",
                "error": (
                    f"The session PCB tool-call budget of {limit} is exhausted. "
                    "No further PCB tool side effects will be executed."
                ),
            }
            _record(
                "pcb_tool_budget_exhausted",
                tool_name=tool_name,
                session_id=session_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                limit=limit,
                consumed=consumed,
            )
            return json.dumps(payload, ensure_ascii=False)
    if is_read:
        return next_call(args)
    key = (str(session_id), str(turn_id), str(api_request_id))
    with _decision_lock:
        blocked = key in _pcb_write_decisions
        if not blocked:
            _pcb_write_decisions[key] = None
            while len(_pcb_write_decisions) > _MAX_DECISION_KEYS:
                _pcb_write_decisions.popitem(last=False)
    if not blocked:
        return next_call(args)
    payload = {
        "tool": tool_name,
        "success": False,
        "blocked": True,
        "policy": "one_pcb_write_per_model_decision",
        "error": (
            "Only one PCB write transaction is executed from each model decision. "
            "Read-only PCB queries may be batched, but independent writes require "
            "a new decision."
        ),
    }
    _record(
        "tool_policy_blocked",
        tool_name=tool_name,
        session_id=session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
        policy=payload["policy"],
    )
    return json.dumps(payload, ensure_ascii=False)


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _positive_number(value: object) -> float | None:
    """Return priced evidence only when it is positive and explicit."""

    result = _nonnegative_number(value)
    return result if result is not None and result > 0 else None


def _first_int(value: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        result = _nonnegative_int(value.get(name))
        if result is not None:
            return result
    return None


def _cached_tokens(usage: Mapping[str, Any]) -> int | None:
    direct = _first_int(
        usage,
        "cache_read_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
    )
    if direct is not None:
        return direct
    for key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(key)
        if isinstance(details, Mapping):
            nested = _first_int(details, "cached_tokens", "cache_read_tokens")
            if nested is not None:
                return nested
    return None


def _cost_metrics(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Separate token accounting from factual, never-inferred monetary cost."""

    input_tokens = _first_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = _first_int(usage, "output_tokens", "completion_tokens")
    cache_read_tokens = _cached_tokens(usage)
    uncached_input_tokens = _first_int(usage, "uncached_input_tokens")
    if (
        uncached_input_tokens is None
        and input_tokens is not None
        and cache_read_tokens is not None
        and cache_read_tokens <= input_tokens
    ):
        uncached_input_tokens = input_tokens - cache_read_tokens

    raw_status = usage.get("cost_status")
    actual_value = None
    actual_currency = None
    if raw_status in {None, "actual", "reported"}:
        actual_value = _positive_number(usage.get("actual_cost_usd"))
        if actual_value is not None:
            actual_currency = "USD"
    if actual_value is None and raw_status in {"actual", "reported"}:
        actual_value = _positive_number(usage.get("cost_amount"))
        currency = usage.get("cost_currency")
        actual_currency = (
            currency if actual_value is not None and isinstance(currency, str) else None
        )
    actual_status = "reported" if actual_value is not None else "unknown"
    return {
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "actual_cost_value": actual_value,
        "actual_cost_currency": actual_currency,
        "actual_cost_status": actual_status,
        "provider_cost_status": raw_status if isinstance(raw_status, str) else None,
        "cost_source": (
            usage.get("cost_source")
            if isinstance(usage.get("cost_source"), str)
            else "provider_usage"
            if actual_value is not None
            else "unavailable"
        ),
    }


def _context_quality_metrics(
    *,
    session_id: str,
    turn_id: str,
    active_tokens: int,
    active_bytes: int,
    message_count: int,
) -> dict[str, Any]:
    """Estimate retained/new active context without consulting cache billing."""

    active_tokens = max(0, int(active_tokens))
    active_bytes = max(0, int(active_bytes))
    with _context_lock:
        previous = _context_snapshots.get(session_id)
        _context_snapshots[session_id] = _ContextSnapshot(
            turn_id, active_tokens, active_bytes
        )
    previous_tokens = previous.active_tokens if previous is not None else 0
    previous_bytes = previous.active_bytes if previous is not None else 0
    repeated_tokens = min(previous_tokens, active_tokens)
    repeated_bytes = min(previous_bytes, active_bytes)
    return {
        "active_context_token_estimate": active_tokens,
        "active_context_bytes": active_bytes,
        "active_message_count": max(0, int(message_count)),
        "repeated_content_token_estimate": repeated_tokens,
        "repeated_content_bytes_estimate": repeated_bytes,
        "repeated_content_ratio": (
            round(repeated_bytes / active_bytes, 6) if active_bytes else 0.0
        ),
        "newly_added_tokens_estimate": max(0, active_tokens - previous_tokens),
        "newly_added_bytes": max(0, active_bytes - previous_bytes),
        "previous_turn_id": previous.turn_id if previous is not None else None,
    }


def _on_session_start(session_id: str, model: str, platform: str) -> None:
    from pcbdraft.agent.tool_bindings import reset_session_project_context

    reset_session_project_context(session_id)
    _clear_session_decisions(session_id)
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
    from pcbdraft.agent.tool_bindings import (
        get_service,
        get_session_project_id,
        reset_session_project_context,
    )
    from pcbdraft.services.progress import ProcessStatus

    project_id = get_session_project_id(session_id)
    if project_id is not None:
        normalized_reason = turn_exit_reason.lower()
        with _decision_lock:
            pcb_tool_budget_exhausted = session_id in _pcb_tool_budget_exhausted
        if "timeout" in normalized_reason or "timed_out" in normalized_reason:
            process_status = ProcessStatus.TIMED_OUT
            termination_reason: str | None = "timed_out"
        elif interrupted:
            process_status = ProcessStatus.CANCELLED
            termination_reason = "cancelled"
        elif pcb_tool_budget_exhausted:
            process_status = ProcessStatus.EXITED
            termination_reason = "budget_exhausted:pcb_tool_calls"
        elif (
            "max_iterations_reached" in normalized_reason
            or normalized_reason == "budget_exhausted"
        ):
            process_status = ProcessStatus.EXITED
            termination_reason = "budget_exhausted:model_turns"
        elif failed:
            # Hermes reached its normal turn finalizer and invoked this hook;
            # the model/tool turn failed, but the host process did not crash.
            process_status = ProcessStatus.EXITED
            termination_reason = "tool_failure"
        else:
            process_status = ProcessStatus.EXITED
            termination_reason = (
                "no_progress"
                if "no_progress" in normalized_reason
                else "human_intervention_required"
                if "strategy_change_required" in normalized_reason
                or "blocked" in normalized_reason
                or "guardrail_halt" in normalized_reason
                else "tool_failure"
                if not completed
                else None
            )
        recorder = getattr(get_service(), "record_product_session_terminal", None)
        if callable(recorder):
            try:
                product_receipt = recorder(
                    project_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    process_status=process_status,
                    termination_reason=termination_reason,
                )
                _record(
                    "product_session_terminal",
                    session_id=session_id,
                    turn_id=turn_id,
                    project_id=project_id,
                    process_status=product_receipt.get("process_status"),
                    task_outcome=product_receipt.get("task_outcome"),
                    termination_reason=product_receipt.get("termination_reason"),
                    stage_reached=product_receipt.get("stage_reached"),
                    release_gate_passed=product_receipt.get("release_gate_passed"),
                    artifact=product_receipt.get("artifact"),
                )
            except Exception as exc:  # noqa: BLE001 - teardown must release binding
                # Session teardown must still release the trusted binding; the
                # missing product receipt remains explicit in the debug trace.
                _record(
                    "product_session_terminal_failed",
                    session_id=session_id,
                    turn_id=turn_id,
                    project_id=project_id,
                    error=str(exc),
                )

    reset_session_project_context(session_id)
    _clear_session_decisions(session_id)
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


def _on_session_finalize(session_id: str) -> None:
    """Clear PCBDraft trackers for a trusted in-process session rotation."""

    from pcbdraft.agent.tool_bindings import reset_session_project_context

    reset_session_project_context(session_id)
    _clear_session_decisions(session_id)
    _record("session_finalize", session_id=session_id)


def _on_session_reset(session_id: str) -> None:
    """Ensure a newly rotated Hermes session starts with empty trackers."""

    from pcbdraft.agent.tool_bindings import reset_session_project_context

    reset_session_project_context(session_id)
    _clear_session_decisions(session_id)
    _record("session_reset", session_id=session_id)


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
    request_char_count: int,
    request: dict[str, Any],
    retry_count: int,
) -> None:
    context_quality = _context_quality_metrics(
        session_id=session_id,
        turn_id=turn_id,
        active_tokens=approx_input_tokens,
        active_bytes=request_char_count,
        message_count=message_count,
    )
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
        context_quality=context_quality,
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
    cost = _cost_metrics(usage)
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
        cost_metrics=cost,
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


# Built-in lifecycle contracts are independent of plugin discovery and settings.
BUILTIN_HOOKS = {
    "on_session_start": (_on_session_start,),
    "on_session_end": (_on_session_end,),
    "on_session_finalize": (_on_session_finalize,),
    "on_session_reset": (_on_session_reset,),
    "pre_api_request": (_on_pre_api_request,),
    "post_api_request": (_on_post_api_request,),
    "api_request_error": (_on_api_request_error,),
    "pre_tool_call": (_on_pre_tool_call,),
    "post_tool_call": (_on_post_tool_call,),
    "post_llm_call": (_on_post_llm_call,),
}
BUILTIN_MIDDLEWARE = {
    "llm_request": (_project_pcb_tool_schemas,),
    "tool_execution": (_one_pcb_write_per_decision,),
}
