# Fallback execution deliberately preserves broad best-effort catches from the
# compatibility module: one broken candidate must not abort the remaining chain.
# ruff: noqa: BLE001, S110
"""Auxiliary-provider health, fallback planning, and chain execution.

``auxiliary_client`` remains the public compatibility surface and injects its
live namespace here.  Looking dependencies up for every call preserves legacy
monkeypatch paths without a reverse import.  Client construction/cache and
credential fetching stay owned by ``auxiliary_client``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from pcbdraft.model import model_metadata as _model_metadata

MINIMUM_CONTEXT_LENGTH = _model_metadata.MINIMUM_CONTEXT_LENGTH
get_model_context_length = _model_metadata.get_model_context_length

_AUX_UNHEALTHY_TTL_SECONDS = 600
_aux_unhealthy_until: dict[str, float] = {}
_aux_unhealthy_logged_at: dict[str, float] = {}
_AUX_UNHEALTHY_LABEL_ALIASES = {
    "openrouter": "openrouter",
    "nous": "nous",
    "custom": "local/custom",
    "local/custom": "local/custom",
    "openai-codex": "openai-codex",
    "codex": "openai-codex",
}

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_auxiliary_fallback_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("auxiliary fallback runtime has not been configured")
    return _runtime_namespace()


def _normalize_chain_label(provider: str) -> str:
    if not provider:
        return ""
    normalized = str(provider).strip().lower()
    aliases = _runtime()["_AUX_UNHEALTHY_LABEL_ALIASES"]
    return aliases.get(normalized, normalized)


def _mark_provider_unhealthy(provider: str, ttl: float | None = None) -> None:
    runtime = _runtime()
    label = runtime["_normalize_chain_label"](provider)
    if not label:
        return
    default_ttl = runtime["_AUX_UNHEALTHY_TTL_SECONDS"]
    effective_ttl = ttl if ttl is not None else default_ttl
    expires_at = time.time() + effective_ttl
    runtime["_aux_unhealthy_until"][label] = expires_at
    runtime["logger"].warning(
        "Auxiliary: marking %s unhealthy for %ds (payment / credit error). "
        "Subsequent auxiliary calls will skip it until %s.",
        label,
        int(effective_ttl),
        time.strftime("%H:%M:%S", time.localtime(expires_at)),
    )


def _is_provider_unhealthy(label: str) -> bool:
    if not label:
        return False
    runtime = _runtime()
    unhealthy_until = runtime["_aux_unhealthy_until"]
    expires_at = unhealthy_until.get(label)
    if expires_at is None:
        return False
    if time.time() >= expires_at:
        unhealthy_until.pop(label, None)
        runtime["_aux_unhealthy_logged_at"].pop(label, None)
        return False
    return True


def _log_skip_unhealthy(label: str, task: str | None = None) -> None:
    runtime = _runtime()
    now = time.time()
    logged_at = runtime["_aux_unhealthy_logged_at"]
    last = logged_at.get(label, 0.0)
    if now - last >= 60:
        logged_at[label] = now
        expires_at = runtime["_aux_unhealthy_until"].get(label, now)
        runtime["logger"].info(
            "Auxiliary %s: skipping %s (recently returned payment error, retry in %ds)",
            task or "call",
            label,
            max(0, int(expires_at - now)),
        )


def _reset_aux_unhealthy_cache() -> None:
    runtime = _runtime()
    runtime["_aux_unhealthy_until"].clear()
    runtime["_aux_unhealthy_logged_at"].clear()


def _fallback_chain_entry(task: str | None, fb_label: str) -> dict[str, Any] | None:
    if not task or not fb_label:
        return None
    match = re.match(r"fallback_chain\[(\d+)\]", fb_label)
    if not match:
        return None
    try:
        chain = _runtime()["_get_auxiliary_task_config"](task).get("fallback_chain")
        entry = chain[int(match.group(1))] if isinstance(chain, list) else None
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _fallback_entry_timeout(task: str | None, fb_label: str) -> float | None:
    entry = _runtime()["_fallback_chain_entry"](task, fb_label)
    raw = entry.get("timeout") if entry else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def _fallback_provider_from_label(label: str) -> str:
    match = re.match(
        r"(?:fallback_chain\[\d+\]|fallback_providers\[\d+\]|main-agent)\(([^)]+)\)$",
        label or "",
    )
    return match.group(1).strip() if match else str(label or "").strip()


class _FallbackDestination(NamedTuple):
    provider: str
    base_url: str
    api_mode: str | None
    model: str | None


def _complete_fallback_destination(
    provider: str,
    base_url: str,
    api_mode: str | None,
    model: str | None,
) -> _FallbackDestination:
    runtime = _runtime()
    if not api_mode:
        if runtime["_endpoint_speaks_anthropic_messages"](base_url):
            api_mode = "anthropic_messages"
        else:
            try:
                from pcbdraft.model.runtime_provider import resolve_runtime_provider

                resolved = resolve_runtime_provider(
                    requested=provider,
                    explicit_base_url=base_url or None,
                    target_model=model or "",
                )
                api_mode = str(resolved.get("api_mode") or "").strip() or None
            except Exception:
                pass
    destination_type = runtime["_FallbackDestination"]
    return destination_type(provider, base_url, api_mode, model)


def _fallback_destination_from_entry(
    entry: dict[str, Any],
    fb_client: Any,
    fb_model: str | None,
) -> _FallbackDestination:
    provider = str(entry.get("provider") or "").strip()
    base_url = str(
        entry.get("base_url") or getattr(fb_client, "base_url", "") or ""
    ).strip()
    api_mode = (
        str(entry.get("api_mode") or entry.get("transport") or "").strip() or None
    )
    model = fb_model or str(entry.get("model") or "").strip() or None
    return _runtime()["_complete_fallback_destination"](
        provider, base_url, api_mode, model
    )


def _fallback_destination(
    task: str | None,
    fb_client: Any,
    fb_model: str | None,
    fb_label: str,
) -> _FallbackDestination:
    runtime = _runtime()
    attached = getattr(fb_client, "_pcbdraft_fallback_destination", None)
    if isinstance(attached, runtime["_FallbackDestination"]):
        return attached
    provider = runtime["_fallback_provider_from_label"](fb_label)
    base_url = str(getattr(fb_client, "base_url", "") or "")
    entry = runtime["_fallback_chain_entry"](task, fb_label)
    if entry is not None:
        return runtime["_fallback_destination_from_entry"](entry, fb_client, fb_model)
    return runtime["_complete_fallback_destination"](provider, base_url, None, fb_model)


def _replan_synchronous_cache_sections(
    messages: list,
    tools: list | None,
    *,
    destination: _FallbackDestination,
) -> tuple[list, list]:
    from pcbdraft.agent.agent_runtime_helpers import (
        configured_cache_ttl,
        plan_cache_sections_for_destination,
    )

    return plan_cache_sections_for_destination(
        messages,
        tools,
        provider=destination.provider,
        base_url=destination.base_url,
        api_mode=destination.api_mode or "",
        model=destination.model or "",
        cache_ttl=configured_cache_ttl(),
    )


def _call_fallback_candidate_sync(
    fb_client: Any,
    fb_model: str | None,
    fb_label: str,
    *,
    task: str | None,
    messages: list,
    temperature: float | None,
    max_tokens: int | None,
    tools: list | None,
    effective_timeout: float,
    effective_extra_body: dict,
    reasoning_config: dict | None,
) -> Any | None:
    """Call one fallback candidate, refreshing a stale credential once."""

    runtime = _runtime()
    fallback_timeout = runtime["_fallback_entry_timeout"](task, fb_label)
    if fallback_timeout is not None and fallback_timeout != effective_timeout:
        runtime["logger"].info(
            "Auxiliary %s: %s using its configured timeout %.0fs "
            "(task-level was %.0fs)",
            task or "call",
            fb_label,
            fallback_timeout,
            effective_timeout,
        )
        effective_timeout = fallback_timeout
    destination = runtime["_fallback_destination"](task, fb_client, fb_model, fb_label)
    fallback_messages, fallback_tools = runtime["_replan_synchronous_cache_sections"](
        messages, tools, destination=destination
    )
    kwargs = runtime["_build_call_kwargs"](
        destination.provider,
        destination.model,
        fallback_messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=fallback_tools,
        timeout=effective_timeout,
        extra_body=effective_extra_body,
        reasoning_config=reasoning_config,
        base_url=destination.base_url,
        task=task,
    )
    try:
        return runtime["_validate_llm_response"](
            runtime["_relay_sync_completion"](
                fb_client,
                kwargs,
                provider=destination.provider,
                api_mode=destination.api_mode,
            ),
            task,
        )
    except Exception as fallback_error:
        if not runtime["_is_auth_error"](fallback_error):
            raise
        provider = runtime["_auth_refresh_provider_for_route"](
            destination.provider, destination.base_url
        )
        if provider not in {"auto", "", None} and runtime[
            "_refresh_provider_credentials"
        ](provider):
            retry_client, retry_model = runtime["_get_cached_client"](
                provider,
                destination.model,
                base_url=destination.base_url or None,
                api_mode=destination.api_mode,
            )
            if retry_client is not None:
                retry_destination = runtime["_FallbackDestination"](
                    provider,
                    destination.base_url
                    or str(getattr(retry_client, "base_url", "") or ""),
                    destination.api_mode,
                    retry_model or destination.model,
                )
                retry_messages, retry_tools = runtime[
                    "_replan_synchronous_cache_sections"
                ](messages, tools, destination=retry_destination)
                retry_kwargs = runtime["_build_call_kwargs"](
                    retry_destination.provider,
                    retry_destination.model,
                    retry_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=retry_tools,
                    timeout=effective_timeout,
                    extra_body=effective_extra_body,
                    reasoning_config=reasoning_config,
                    base_url=retry_destination.base_url,
                    task=task,
                )
                try:
                    return runtime["_validate_llm_response"](
                        runtime["_relay_sync_completion"](
                            retry_client,
                            retry_kwargs,
                            provider=retry_destination.provider,
                            api_mode=retry_destination.api_mode,
                        ),
                        task,
                    )
                except Exception as retry_error:
                    if not runtime["_is_auth_error"](retry_error):
                        raise
        runtime["_mark_provider_unhealthy"](provider or fb_label)
        runtime["logger"].warning(
            "Auxiliary %s: fallback candidate %s has a stale/unrefreshable "
            "credential (%s) — skipping to next fallback",
            task or "call",
            fb_label,
            fallback_error,
        )
        return None


async def _call_fallback_candidate_async(
    fb_client: Any,
    fb_model: str | None,
    fb_label: str,
    *,
    task: str | None,
    messages: list,
    temperature: float | None,
    max_tokens: int | None,
    tools: list | None,
    effective_timeout: float,
    effective_extra_body: dict,
    reasoning_config: dict | None,
) -> Any | None:
    runtime = _runtime()
    fallback_timeout = runtime["_fallback_entry_timeout"](task, fb_label)
    if fallback_timeout is not None and fallback_timeout != effective_timeout:
        runtime["logger"].info(
            "Auxiliary %s: %s using its configured timeout %.0fs "
            "(task-level was %.0fs)",
            task or "call",
            fb_label,
            fallback_timeout,
            effective_timeout,
        )
        effective_timeout = fallback_timeout
    destination = runtime["_fallback_destination"](task, fb_client, fb_model, fb_label)
    fallback_messages, fallback_tools = runtime["_replan_synchronous_cache_sections"](
        messages, tools, destination=destination
    )
    kwargs = runtime["_build_call_kwargs"](
        destination.provider,
        destination.model,
        fallback_messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=fallback_tools,
        timeout=effective_timeout,
        extra_body=effective_extra_body,
        reasoning_config=reasoning_config,
        base_url=destination.base_url,
        task=task,
    )
    try:
        return runtime["_validate_llm_response"](
            await runtime["_relay_async_completion"](
                fb_client,
                kwargs,
                provider=destination.provider,
                api_mode=destination.api_mode,
            ),
            task,
        )
    except Exception as fallback_error:
        if not runtime["_is_auth_error"](fallback_error):
            raise
        provider = runtime["_auth_refresh_provider_for_route"](
            destination.provider, destination.base_url
        )
        if provider not in {"auto", "", None} and runtime[
            "_refresh_provider_credentials"
        ](provider):
            retry_client, retry_model = runtime["_get_cached_client"](
                provider,
                destination.model,
                async_mode=True,
                base_url=destination.base_url or None,
                api_mode=destination.api_mode,
            )
            if retry_client is not None:
                retry_destination = runtime["_FallbackDestination"](
                    provider,
                    destination.base_url
                    or str(getattr(retry_client, "base_url", "") or ""),
                    destination.api_mode,
                    retry_model or destination.model,
                )
                retry_messages, retry_tools = runtime[
                    "_replan_synchronous_cache_sections"
                ](messages, tools, destination=retry_destination)
                retry_kwargs = runtime["_build_call_kwargs"](
                    retry_destination.provider,
                    retry_destination.model,
                    retry_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=retry_tools,
                    timeout=effective_timeout,
                    extra_body=effective_extra_body,
                    reasoning_config=reasoning_config,
                    base_url=retry_destination.base_url,
                    task=task,
                )
                try:
                    return runtime["_validate_llm_response"](
                        await runtime["_relay_async_completion"](
                            retry_client,
                            retry_kwargs,
                            provider=retry_destination.provider,
                            api_mode=retry_destination.api_mode,
                        ),
                        task,
                    )
                except Exception as retry_error:
                    if not runtime["_is_auth_error"](retry_error):
                        raise
        runtime["_mark_provider_unhealthy"](provider or fb_label)
        runtime["logger"].warning(
            "Auxiliary %s (async): fallback candidate %s has a "
            "stale/unrefreshable credential (%s) — skipping to next fallback",
            task or "call",
            fb_label,
            fallback_error,
        )
        return None


def _try_payment_fallback(
    failed_provider: str,
    task: str | None = None,
    reason: str = "payment error",
) -> tuple[Any | None, str | None, str]:
    runtime = _runtime()
    skip = failed_provider.lower().strip()
    main_provider = runtime["_read_main_provider"]()
    skip_labels = {skip}
    if main_provider and main_provider.lower() in skip:
        skip_labels.add(main_provider.lower())
    aliases = {
        "openrouter": "openrouter",
        "nous": "nous",
        "openai-codex": "openai-codex",
        "codex": "openai-codex",
        "custom": "local/custom",
        "local/custom": "local/custom",
    }
    skip_chain_labels = {aliases.get(label, label) for label in skip_labels}
    tried = []
    for label, try_fn in runtime["_get_provider_chain"]():
        if label in skip_chain_labels:
            continue
        if runtime["_is_provider_unhealthy"](label):
            runtime["_log_skip_unhealthy"](label, task)
            tried.append(f"{label} (unhealthy)")
            continue
        client, model = try_fn()
        if client is not None:
            runtime["logger"].info(
                "Auxiliary %s: %s on %s — falling back to %s (%s)",
                task or "call",
                reason,
                failed_provider,
                label,
                model or "default",
            )
            return client, model, label
        tried.append(label)
    runtime["logger"].warning(
        "Auxiliary %s: %s on %s and no fallback available (tried: %s)",
        task or "call",
        reason,
        failed_provider,
        ", ".join(tried),
    )
    return None, None, ""


def _try_main_agent_model_fallback(
    failed_provider: str,
    task: str | None = None,
    reason: str = "error",
    failed_model: str | None = None,
) -> tuple[Any | None, str | None, str]:
    runtime = _runtime()
    main_provider = (runtime["_read_main_provider"]() or "").strip()
    main_model = (runtime["_read_main_model"]() or "").strip()
    if main_provider.lower() == "moa":
        aggregator_provider, aggregator_model = runtime["_resolve_moa_aggregator"](
            main_model
        )
        if not aggregator_provider or not aggregator_model:
            return None, None, ""
        main_provider, main_model = aggregator_provider, aggregator_model
    if not main_provider or not main_model or main_provider.lower() in {"auto", ""}:
        return None, None, ""

    from pcbdraft.agent.backend_identity import (
        BackendIdentity,
        FailureScope,
        should_skip_candidate,
    )

    skip_model = (failed_model or "").strip().lower() or None
    if should_skip_candidate(
        BackendIdentity.build(provider=main_provider, model=main_model),
        BackendIdentity.build(provider=failed_provider, model=skip_model),
        FailureScope.MODEL if skip_model else FailureScope.CREDENTIAL,
    ):
        return None, None, ""
    if runtime["_is_provider_unhealthy"](main_provider):
        runtime["_log_skip_unhealthy"](main_provider, task)
        return None, None, ""
    try:
        client, resolved_model = runtime["resolve_provider_client"](
            provider=main_provider,
            model=main_model,
        )
    except Exception:
        client, resolved_model = None, None
    if client is None:
        return None, None, ""
    label = f"main-agent({main_provider})"
    runtime["logger"].info(
        "Auxiliary %s: %s on %s — falling back to main agent model %s (%s)",
        task or "call",
        reason,
        failed_provider,
        label,
        resolved_model or main_model,
    )
    return client, resolved_model or main_model, label


def _task_minimum_context_length(task: str | None) -> int | None:
    if not task:
        return None
    if task == "compression":
        return _runtime()["MINIMUM_CONTEXT_LENGTH"]
    return None


def _candidate_context_window(
    provider: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
) -> int | None:
    if not model:
        return None
    runtime = _runtime()
    try:
        context = runtime["get_model_context_length"](
            model,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
        )
    except Exception as exc:
        runtime["logger"].debug(
            "Auxiliary fallback: could not resolve context window for %s/%s: %s",
            provider,
            model,
            exc,
        )
        return None
    if isinstance(context, int) and context > 0:
        return context
    return None


def _try_configured_fallback_chain(
    task: str,
    failed_provider: str,
    reason: str = "error",
    failed_model: str | None = None,
) -> tuple[Any | None, str | None, str]:
    if not task:
        return None, None, ""
    runtime = _runtime()
    chain = runtime["_get_auxiliary_task_config"](task).get("fallback_chain")
    if not chain or not isinstance(chain, list):
        return None, None, ""

    from pcbdraft.agent.backend_identity import (
        BackendIdentity,
        FailureScope,
        should_skip_candidate,
    )

    skip_model = (failed_model or "").strip().lower() or None
    failed_identity = BackendIdentity.build(
        provider=failed_provider,
        model=skip_model,
    )
    failure_scope = FailureScope.MODEL if skip_model else FailureScope.CREDENTIAL
    tried = []
    minimum_context = runtime["_task_minimum_context_length"](task)
    for index, entry in enumerate(chain):
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider", "")).strip()
        if not provider:
            continue
        model_raw = str(entry.get("model", "")).strip()
        if should_skip_candidate(
            BackendIdentity.build(
                provider=provider,
                model=model_raw,
                base_url=str(entry.get("base_url") or ""),
            ),
            failed_identity,
            failure_scope,
        ):
            continue
        model = model_raw or None
        label = f"fallback_chain[{index}]({provider})"
        try:
            client, resolved_model = runtime["_resolve_fallback_entry"](entry)
        except Exception:
            client, resolved_model = None, None
        if client is not None:
            if minimum_context is not None and resolved_model:
                context = runtime["_candidate_context_window"](
                    provider,
                    resolved_model,
                    base_url=str(entry.get("base_url") or ""),
                    api_key=runtime["_fallback_entry_api_key"](entry) or "",
                )
                if context is not None and context < minimum_context:
                    runtime["logger"].info(
                        "Auxiliary %s: skipping %s (%s context=%d < min=%d), "
                        "continuing chain",
                        task,
                        label,
                        resolved_model,
                        context,
                        minimum_context,
                    )
                    tried.append(
                        f"{label} (context too small: {context}<{minimum_context})"
                    )
                    continue
            runtime["logger"].info(
                "Auxiliary %s: %s on %s — configured fallback to %s (%s)",
                task,
                reason,
                failed_provider,
                label,
                resolved_model or model or "default",
            )
            return client, resolved_model or model, label
        tried.append(label)
    if tried:
        runtime["logger"].debug(
            "Auxiliary %s: configured fallback_chain exhausted (tried: %s)",
            task,
            ", ".join(tried),
        )
    return None, None, ""


def _try_configured_fallback_for_unavailable_client(
    task: str | None,
    failed_provider: str,
) -> tuple[Any | None, str | None, str]:
    explicit = (failed_provider or "").strip().lower()
    if not task or not explicit or explicit in {"auto"}:
        return None, None, ""
    return _runtime()["_try_configured_fallback_chain"](
        task,
        explicit,
        reason="provider unavailable",
    )


def _try_main_fallback_chain(
    task: str | None,
    failed_provider: str = "",
    reason: str = "error",
) -> tuple[Any | None, str | None, str]:
    runtime = _runtime()
    try:
        from pcbdraft.model.configuration import load_config_readonly
        from pcbdraft.model.fallback_config import get_fallback_chain

        chain = get_fallback_chain(load_config_readonly())
    except Exception as exc:
        runtime["logger"].debug(
            "Auxiliary %s: could not load main fallback chain: %s",
            task or "call",
            exc,
        )
        return None, None, ""
    if not chain:
        return None, None, ""

    failed_normalized = (failed_provider or "").strip().lower()
    main_normalized = (runtime["_read_main_provider"]() or "").strip().lower()
    skip = {
        provider
        for provider in (failed_normalized, main_normalized, "auto")
        if provider
    }
    tried: list[str] = []
    minimum_context = runtime["_task_minimum_context_length"](task)
    for index, entry in enumerate(chain):
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue
        normalized = provider.lower()
        label = f"fallback_providers[{index}]({provider})"
        if normalized in skip:
            tried.append(f"{label} (skipped)")
            continue
        if runtime["_is_provider_unhealthy"](normalized):
            runtime["_log_skip_unhealthy"](normalized, task)
            tried.append(f"{label} (unhealthy)")
            continue
        try:
            client, resolved_model = runtime["_resolve_fallback_entry"](entry)
        except Exception as exc:
            runtime["logger"].debug(
                "Auxiliary %s: main fallback %s failed to resolve: %s",
                task or "call",
                label,
                exc,
            )
            client, resolved_model = None, None
        if client is not None:
            if minimum_context is not None:
                context = runtime["_candidate_context_window"](
                    provider,
                    resolved_model or model,
                    base_url=str(entry.get("base_url") or ""),
                    api_key=runtime["_fallback_entry_api_key"](entry) or "",
                )
                if context is not None and context < minimum_context:
                    runtime["logger"].info(
                        "Auxiliary %s: skipping %s (context=%d < min=%d), "
                        "continuing chain",
                        task or "call",
                        label,
                        context,
                        minimum_context,
                    )
                    tried.append(
                        f"{label} (context too small: {context}<{minimum_context})"
                    )
                    continue
            runtime["logger"].info(
                "Auxiliary %s: %s on %s — main fallback chain to %s (%s)",
                task or "call",
                reason,
                failed_provider or "auto",
                label,
                resolved_model or model,
            )
            return client, resolved_model or model, provider
        tried.append(label)
    if tried:
        runtime["logger"].debug(
            "Auxiliary %s: main fallback chain exhausted (tried: %s)",
            task or "call",
            ", ".join(tried),
        )
    return None, None, ""
