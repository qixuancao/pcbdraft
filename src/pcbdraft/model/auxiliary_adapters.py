"""OpenAI-compatible auxiliary adapters for Codex, Anthropic, and Bedrock."""

# Provider SDK failures and callback hooks are intentionally best-effort at
# the same points as the original auxiliary-client implementation.
# ruff: noqa: BLE001, C901, S110

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pcbdraft.model.auxiliary_cancellation import (
    AuxiliaryExplicitCancellation,
    _aux_interrupt_cancel_requested,
    _aux_interrupt_protected,
    _aux_progress_active,
    _capture_aux_cancel_check,
    _captured_aux_cancel_requested,
    _notify_aux_progress,
)

if TYPE_CHECKING:
    from openai import OpenAI

logger = logging.getLogger("pcbdraft.model.auxiliary_client")

_runtime_main_value_hook: Callable[[str], Any] = lambda _field: ""
_evict_cached_client_instance_hook: Callable[[Any], bool] = lambda _target: False


def configure_auxiliary_adapter_runtime(
    *,
    runtime_main_value: Callable[[str], Any],
    evict_cached_client_instance: Callable[[Any], bool],
) -> None:
    """Install monolith-owned state/cache callbacks without a reverse import."""
    global _runtime_main_value_hook, _evict_cached_client_instance_hook
    _runtime_main_value_hook = runtime_main_value
    _evict_cached_client_instance_hook = evict_cached_client_instance


def _runtime_main_value(field: str) -> Any:
    return _runtime_main_value_hook(field)


def _evict_cached_client_instance(target: Any) -> bool:
    return _evict_cached_client_instance_hook(target)


class _CodexCompletionsAdapter:
    """Drop-in shim that accepts chat.completions.create() kwargs and
    routes them through the Codex Responses streaming API."""

    def __init__(self, real_client: OpenAI, model: str):
        self._client = real_client
        self._model = model

    def create(self, **kwargs) -> Any:
        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)

        # Separate system/instructions from replayable conversation messages,
        # then route the rest through the SINGLE shared chat->Responses
        # converter used by the main agent transport
        # (agent/transports/codex.py). Maintaining a private conversion loop
        # here let chat-style messages with role="tool" leak straight into
        # Responses input[] — which the Responses API rejects with
        # "Invalid value: 'tool'. Supported values are: 'assistant', 'system',
        # 'developer', and 'user'." (issue #5709, hit hard by flush_memories()
        # / compression replaying real session history that includes assistant
        # tool_calls + role="tool" results). The shared converter encodes
        # assistant tool calls as `function_call` items and tool results as
        # `function_call_output` items with a valid call_id, so every
        # Responses path normalizes tool history identically and cannot drift.
        from pcbdraft.core.runtime_utils import base_url_host_matches
        from pcbdraft.model.codex_responses_adapter import (
            _chat_messages_to_responses_input,
        )

        instructions = "You are a helpful assistant."
        replay_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "system":
                instructions = content if isinstance(content, str) else str(content)
            else:
                replay_messages.append(msg)

        # Copilot (githubcopilot.com) binds replayed codex_message_items ids
        # to a backend "connection" that doesn't survive credential
        # rotation/gateway restarts — replaying one gets HTTP 401 "input
        # item ID does not belong to this connection" (#32716). Auxiliary
        # calls (context compression, flush_memories, MoA aggregation) go
        # through this adapter instead of agent/transports/codex.py's
        # build_kwargs, so they need the same guard applied independently.
        _host_for_input = str(getattr(self._client, "base_url", "") or "")
        _is_github_for_input = base_url_host_matches(
            _host_for_input, "githubcopilot.com"
        )
        # Auxiliary calls never send ``context_management`` (native
        # compaction is a main-turn feature), so they must never replay a
        # compaction checkpoint from the replayed history nor let one
        # restructure this request — the summarizer/aggregator model is
        # usually not even the one that minted the blob.
        input_items = _chat_messages_to_responses_input(
            replay_messages,
            is_github_responses=_is_github_for_input,
            native_compaction_eligible=False,
        )

        resp_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items or [{"role": "user", "content": ""}],
            "store": False,
        }

        # Preserve the chat.completions timeout contract. This adapter is used
        # by auxiliary calls such as context compression; if the timeout is not
        # forwarded and enforced, a Codex Responses stream can sit behind a
        # dead-looking CLI until the user force-interrupts the whole session.
        timeout = kwargs.get("timeout")
        if timeout is not None:
            resp_kwargs["timeout"] = timeout

        # Note: the Codex endpoint (chatgpt.com/backend-api/codex) does NOT
        # support max_output_tokens or temperature — omit to avoid 400 errors.

        # Translate extra_body.reasoning (chat.completions shape) into the
        # Responses API's top-level reasoning + include fields.  Mirrors
        # agent/transports/codex.py::build_kwargs() so auxiliary callers
        # that configure reasoning via auxiliary.<task>.extra_body get the
        # same behavior as the main agent's Codex transport.
        extra_body = kwargs.get("extra_body") or {}
        if isinstance(extra_body, dict):
            reasoning_cfg = extra_body.get("reasoning")
            if isinstance(reasoning_cfg, dict):
                if reasoning_cfg.get("enabled") is False:
                    # Reasoning explicitly disabled — do not set reasoning
                    # or include.  The Codex backend still thinks by
                    # default, but we honor the caller's intent where the
                    # API allows it.
                    pass
                else:
                    # Truthy-only check mirrors agent/transports/codex.py
                    # build_kwargs(): falsy values (None, "", 0) fall back
                    # to the default rather than being forwarded to the
                    # Codex backend, which rejects e.g. {"effort": null}
                    # with a 400.
                    effort = reasoning_cfg.get("effort") or "medium"
                    # Codex backend rejects "minimal"; clamp to "low" to
                    # match the main-agent Codex transport behavior.
                    if effort == "minimal":
                        effort = "low"
                    resp_kwargs["reasoning"] = {
                        "effort": effort,
                        "summary": "auto",
                    }
                    resp_kwargs["include"] = ["reasoning.encrypted_content"]

        # Tools support for auxiliary callers (e.g. skills_hub) that pass function schemas
        tools = kwargs.get("tools")
        if tools:
            # xAI's Responses endpoint rejects ``pattern`` and ``format`` JSON Schema
            # keywords (HTTP 400). Strip them here to match the parity guarantee that
            # chat_completion_helpers.py provides for the main-agent xAI path.
            #
            # Deep-copy before sanitizing — ``list(tools)`` is only a shallow
            # copy of the outer list, but the sanitizers mutate the inner
            # parameter dicts in place.  Without a deep copy the caller's
            # tool registry permanently loses its slash-containing enum
            # constraints after the first auxiliary xAI call.  See #27907.
            try:
                from pcbdraft.tools.schema_sanitizer import (
                    strip_pattern_and_format,
                    strip_slash_enum,
                )

                tools = copy.deepcopy(list(tools))
                tools, _ = strip_pattern_and_format(tools)
                tools, _ = strip_slash_enum(tools)
            except Exception as exc:
                logger.warning(
                    "Auxiliary client: failed to sanitize tool schemas for "
                    "Codex/xAI Responses path: %s",
                    exc,
                )
            converted = []
            for t in tools:
                fn = t.get("function", {}) if isinstance(t, dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                converted.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                )
            if converted:
                resp_kwargs["tools"] = converted

        # Stable prompt-cache routing for the Codex/Responses aux path, mirroring
        # the main transport (agent/transports/codex.py::build_kwargs, which sets
        # prompt_cache_key = _content_cache_key(instructions, tools)). Without
        # this, MoA acting-aggregator and other auxiliary Responses calls stay
        # cache-cold while the main Responses transport is warm (issue #53735).
        # The key is content-addressed from the static prefix (instructions +
        # tool schemas) so it stays warm across turns/fires. Guard the top-level
        # field the same way the main transport does: xAI Responses takes the
        # key in extra_body (not top-level) and GitHub/Copilot Responses opts
        # out of cache-key routing entirely — for those hosts, skip it here.
        try:
            from pcbdraft.core.runtime_utils import base_url_host_matches
            from pcbdraft.model.transports.codex import (
                _cache_scope_from_session_id,
                _content_cache_key,
                _default_prompt_cache_retention_for_request,
            )

            _host_src = str(getattr(self._client, "base_url", "") or "")
            _is_xai = base_url_host_matches(_host_src, "x.ai") or base_url_host_matches(
                _host_src, "api.x.ai"
            )
            _is_github = base_url_host_matches(
                _host_src, "githubcopilot.com"
            ) or base_url_host_matches(_host_src, "models.github.ai")
            if not _is_xai and not _is_github and "prompt_cache_key" not in resp_kwargs:
                # Scope by the owning turn's conversation so two unrelated
                # sessions with the same instructions/tools (e.g. compression,
                # MoA, flush_memories firing back-to-back on different
                # sessions) don't bucket-share a prompt cache slot (#78941).
                # Prefer the rotation-stable logical scope threaded through
                # set_runtime_main() (compression-lineage root, #79017) and
                # fall back to the physical session id, mirroring the main
                # transport (agent/transports/codex.py::build_kwargs).
                _scope = _cache_scope_from_session_id(
                    _runtime_main_value("cache_scope")
                    or _runtime_main_value("session_id")
                )
                _cache_key = _content_cache_key(
                    instructions, resp_kwargs.get("tools"), _scope
                )
                if _cache_key:
                    resp_kwargs["prompt_cache_key"] = _cache_key
            if "prompt_cache_retention" not in resp_kwargs:
                _cache_retention = _default_prompt_cache_retention_for_request(
                    model,
                    _host_src,
                )
                if _cache_retention:
                    resp_kwargs["prompt_cache_retention"] = _cache_retention
        except Exception:
            logger.debug(
                "Codex auxiliary: prompt_cache_key derivation skipped", exc_info=True
            )

        # Stream and collect the response
        text_parts: list[str] = []
        tool_calls_raw: list[Any] = []
        usage = None
        total_timeout = (
            timeout if isinstance(timeout, (int, float)) and timeout > 0 else None
        )
        deadline = time.monotonic() + float(total_timeout) if total_timeout else None
        timed_out = threading.Event()
        timeout_timer: threading.Timer | None = None
        # A protected provider call may outlive its owning compression attempt:
        # the owner returns promptly on hard cancellation while this adapter is
        # still blocked in the SDK stream on its isolated worker. Timer threads
        # do not inherit this worker's thread-local protection state, so freeze
        # the hard-cancel source here, before creating the timer.
        protected_cancel_check = (
            _capture_aux_cancel_check() if _aux_interrupt_protected() else None
        )
        attempt_stream_lock = threading.Lock()
        attempt_stream: list[Any] = []

        def _timeout_message() -> str:
            return f"Codex auxiliary Responses stream exceeded {float(total_timeout):.1f}s total timeout"

        def _close_client_on_timeout() -> None:
            begin_timeout_cleanup = getattr(
                protected_cancel_check, "begin_timeout_cleanup", None
            )
            if callable(begin_timeout_cleanup):
                timeout_won = bool(begin_timeout_cleanup())
            else:
                timeout_won = not (
                    callable(protected_cancel_check)
                    and _captured_aux_cancel_requested(protected_cancel_check)
                )
            # Publish transport timeout only after the attempt-local decision is
            # fixed, so owner polling cannot observe completion in between.
            timed_out.set()
            if not timeout_won:
                # The request owner already hard-cancelled this attempt. The
                # OpenAI client is process-shared, so closing/evicting it here
                # would disrupt unrelated sessions. Wake only this attempt's
                # event stream when responses.create() returned one in time;
                # otherwise rely on the bounded SDK/provider timeout.
                with attempt_stream_lock:
                    stream = attempt_stream[0] if attempt_stream else None
                close_stream = getattr(stream, "close", None)
                if callable(close_stream):
                    try:
                        close_stream()
                    except Exception:
                        logger.debug(
                            "Codex auxiliary: cancelled attempt stream close "
                            "during timeout failed",
                            exc_info=True,
                        )
                return
            close = getattr(self._client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug(
                        "Codex auxiliary: client close during timeout failed",
                        exc_info=True,
                    )
            # The cached auxiliary client wraps this same ``self._client``
            # (or *is* a ``CodexAuxiliaryClient`` whose ``_real_client`` is
            # this instance).  After we close the httpx transport above, the
            # cache must drop that entry — otherwise the next auxiliary call
            # (compression retry, memory flush, etc.) reuses the dead client
            # and fails fast with a connection error.  See issue #23432.
            try:
                _evict_cached_client_instance(self._client)
            except Exception:
                logger.debug(
                    "Codex auxiliary: cache eviction on timeout failed", exc_info=True
                )

        def _check_cancelled() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                if not timed_out.is_set():
                    _close_client_on_timeout()
                raise TimeoutError(_timeout_message())
            try:
                from pcbdraft.tools.interrupt import is_interrupted

                # Honor interrupt protection for atomic aux tasks (compression):
                # a mid-flight gateway interrupt must NOT abort the summary call
                # and trigger a degraded fallback marker (#23975). Explicit host
                # cancellation has its own frozen exception; timeouts above still
                # fire and other aux tasks remain interruptible.
                if _aux_interrupt_cancel_requested():
                    raise AuxiliaryExplicitCancellation()
                if is_interrupted() and not _aux_interrupt_protected():
                    raise InterruptedError(
                        "Codex auxiliary Responses stream interrupted"
                    )
            except (InterruptedError, AuxiliaryExplicitCancellation):
                raise
            except Exception:
                # Interrupt state is a best-effort UX hook; never make it a
                # new failure mode for auxiliary calls.
                pass

        try:
            if total_timeout:
                timeout_timer = threading.Timer(
                    float(total_timeout), _close_client_on_timeout
                )
                timeout_timer.daemon = True
                timeout_timer.start()
            _check_cancelled()

            # Event-driven Responses streaming via the low-level
            # ``responses.create(stream=True)`` path.  The high-level
            # ``responses.stream(...)`` helper does post-hoc typed
            # reconstruction from ``response.completed.response.output``,
            # which the chatgpt.com Codex backend has been observed to
            # return as ``null`` (gpt-5.5, May 2026) — that crashes the SDK
            # with ``TypeError: 'NoneType' object is not iterable``.
            # Consuming raw events and assembling the final response
            # ourselves from ``response.output_item.done`` makes us
            # structurally immune to that drift.
            from pcbdraft.model.codex_runtime import _consume_codex_event_stream

            stream_kwargs = dict(resp_kwargs)
            stream_kwargs["stream"] = True

            def _on_each_event(_event: Any) -> None:
                # Re-check timeout/cancellation per event, matching the
                # cadence the old in-line ``_check_cancelled()`` used.
                # Each SSE event is also forward progress for hosts watching
                # a progress hook (gateway session hygiene): a reasoning
                # model streaming a long summary must not look hung.
                _notify_aux_progress()
                _check_cancelled()

            event_stream = self._client.responses.create(**stream_kwargs)
            with attempt_stream_lock:
                attempt_stream.append(event_stream)
            # The timer can fire while responses.create() is blocked. If the
            # cancelled attempt had no stream to close at that instant, close it
            # now that it is safely attempt-owned; never touch the shared client.
            if (
                timed_out.is_set()
                and callable(protected_cancel_check)
                and _captured_aux_cancel_requested(protected_cancel_check)
            ):
                close_fn = getattr(event_stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        logger.debug(
                            "Codex auxiliary: late cancelled attempt stream close failed",
                            exc_info=True,
                        )
            try:
                # Some Codex-compatible hosts accept ``stream=True`` but return
                # a completed Responses object instead of an SSE iterator. Do
                # not hand that object to the event consumer: typed Responses
                # (and compatibility shims such as SimpleNamespace) are not
                # event streams and may not be iterable at all.
                if hasattr(event_stream, "output"):
                    final = event_stream
                else:
                    final = _consume_codex_event_stream(
                        event_stream,
                        model=str(resp_kwargs.get("model") or model),
                        on_event=_on_each_event,
                    )
            finally:
                close_fn = getattr(event_stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
                with attempt_stream_lock:
                    attempt_stream.clear()

            if final is None:
                raise RuntimeError(
                    "Codex auxiliary Responses stream did not return a final response"
                )

            # Extract text and tool calls from the Responses output.
            # Items may be SimpleNamespace (raw-event path) or dicts
            # (some legacy fallback paths), so handle both shapes.
            def _item_get(obj: Any, key: str, default: Any = None) -> Any:
                val = getattr(obj, key, None)
                if val is None and isinstance(obj, dict):
                    val = obj.get(key, default)
                return val if val is not None else default

            for item in getattr(final, "output", None) or []:
                item_type = _item_get(item, "type")
                if item_type == "message":
                    for part in _item_get(item, "content") or []:
                        ptype = _item_get(part, "type")
                        if ptype in {"output_text", "text"}:
                            text_parts.append(_item_get(part, "text", ""))
                elif item_type == "function_call":
                    tool_calls_raw.append(
                        SimpleNamespace(
                            id=_item_get(item, "call_id", ""),
                            type="function",
                            function=SimpleNamespace(
                                name=_item_get(item, "name", ""),
                                arguments=_item_get(item, "arguments", "{}"),
                            ),
                        )
                    )

            resp_usage = getattr(final, "usage", None)
            if resp_usage:
                usage = SimpleNamespace(
                    prompt_tokens=getattr(resp_usage, "input_tokens", 0)
                    or (
                        resp_usage.get("input_tokens", 0)
                        if isinstance(resp_usage, dict)
                        else 0
                    ),
                    completion_tokens=getattr(resp_usage, "output_tokens", 0)
                    or (
                        resp_usage.get("output_tokens", 0)
                        if isinstance(resp_usage, dict)
                        else 0
                    ),
                    total_tokens=getattr(resp_usage, "total_tokens", 0)
                    or (
                        resp_usage.get("total_tokens", 0)
                        if isinstance(resp_usage, dict)
                        else 0
                    ),
                )
        except Exception as exc:
            if timed_out.is_set():
                raise TimeoutError(_timeout_message()) from exc
            logger.debug("Codex auxiliary Responses API call failed: %s", exc)
            raise
        finally:
            if timeout_timer is not None:
                timeout_timer.cancel()

        content = "".join(text_parts).strip() or None

        # Build a response that looks like chat.completions
        message = SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=tool_calls_raw or None,
        )
        choice = SimpleNamespace(
            index=0,
            message=message,
            finish_reason="stop" if not tool_calls_raw else "tool_calls",
        )
        return SimpleNamespace(
            choices=[choice],
            model=model,
            usage=usage,
        )


class _CodexChatShim:
    """Wraps the adapter to provide client.chat.completions.create()."""

    def __init__(self, adapter: _CodexCompletionsAdapter):
        self.completions = adapter


class CodexAuxiliaryClient:
    """OpenAI-client-compatible wrapper that routes through Codex Responses API.

    Consumers can call client.chat.completions.create(**kwargs) as normal.
    Also exposes .api_key and .base_url for introspection by async wrappers.
    """

    def __init__(self, real_client: OpenAI, model: str):
        self._real_client = real_client
        adapter = _CodexCompletionsAdapter(real_client, model)
        self.chat = _CodexChatShim(adapter)
        self.api_key = real_client.api_key
        self.base_url = real_client.base_url

    def close(self):
        self._real_client.close()


class _AsyncCodexCompletionsAdapter:
    """Async version of the Codex Responses adapter.

    Wraps the sync adapter via asyncio.to_thread() so async consumers
    (web_tools, session_search) can await it as normal.
    """

    def __init__(self, sync_adapter: _CodexCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio

        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncCodexChatShim:
    def __init__(self, adapter: _AsyncCodexCompletionsAdapter):
        self.completions = adapter


class AsyncCodexAuxiliaryClient:
    """Async-compatible wrapper matching AsyncOpenAI.chat.completions.create()."""

    def __init__(self, sync_wrapper: CodexAuxiliaryClient):
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncCodexCompletionsAdapter(sync_adapter)
        self.chat = _AsyncCodexChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url
        # Mirror the sync wrapper's _real_client so cache eviction by leaf
        # OpenAI client (e.g. _close_client_on_timeout in #23482) drops
        # this async entry too. Without this, sync and async cache entries
        # diverge on poisoning: the sync entry is evicted but the async
        # entry keeps reusing the closed transport, failing every
        # subsequent async aux call with 'Connection error' until the
        # gateway restarts.
        self._real_client = sync_wrapper._real_client


class _AnthropicCompletionsAdapter:
    """OpenAI-client-compatible adapter for Anthropic Messages API."""

    def __init__(
        self,
        real_client: Any,
        model: str,
        is_oauth: bool = False,
        base_url: str | None = None,
    ):
        self._client = real_client
        self._model = model
        self._is_oauth = is_oauth
        # Prefer the caller-supplied URL (AnthropicAuxiliaryClient keeps the
        # pre-strip Portal ``.../v1`` form). Only fall back to the SDK
        # client's host for Nous Portal — a blanket fallback would flip
        # MiniMax/Zhipu/etc. aux adapters from "unknown host = native
        # Anthropic" to third-party (stripping thinking signatures).
        self._base_url = base_url or None
        if not self._base_url:
            candidate = str(getattr(real_client, "base_url", "") or "") or None
            if candidate:
                try:
                    from pcbdraft.model.anthropic_adapter import (
                        _is_nous_portal_endpoint,
                    )

                    if _is_nous_portal_endpoint(candidate):
                        self._base_url = candidate
                except Exception:
                    pass

    def create(self, **kwargs) -> Any:
        from pcbdraft.model.anthropic_adapter import (
            build_anthropic_kwargs,
            create_anthropic_message,
        )
        from pcbdraft.model.transports import get_transport

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        reasoning_config = kwargs.get("_reasoning_config")
        # ZAI's Anthropic-compatible endpoint rejects max_tokens on vision
        # models (glm-4v-flash etc.) with error code 1210.  When the caller
        # signals this by setting _skip_zai_max_tokens in kwargs, omit it.
        _skip_mt = kwargs.pop("_skip_zai_max_tokens", False)
        if _skip_mt:
            max_tokens = None
        else:
            max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        temperature = kwargs.get("temperature")

        normalized_tool_choice = None
        if isinstance(tool_choice, str):
            normalized_tool_choice = tool_choice
        elif isinstance(tool_choice, dict):
            choice_type = str(tool_choice.get("type", "")).lower()
            if choice_type == "function":
                normalized_tool_choice = tool_choice.get("function", {}).get("name")
            elif choice_type in {"auto", "required", "none"}:
                normalized_tool_choice = choice_type

        # Reasoning priority: explicit per-call reasoning_config (MoA per-slot,
        # passed as _reasoning_config by _build_call_kwargs) wins over an
        # extra_body.reasoning dict (auxiliary.<task>.extra_body config).
        # build_anthropic_kwargs translates the config dict into the native
        # ``thinking`` field and handles models where thinking is mandatory.
        _reasoning_cfg = reasoning_config
        if _reasoning_cfg is None:
            _eb = kwargs.get("extra_body")
            if isinstance(_eb, dict):
                _rc = _eb.get("reasoning")
                if isinstance(_rc, dict):
                    _reasoning_cfg = _rc

        anthropic_kwargs = build_anthropic_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_config=_reasoning_cfg,
            tool_choice=normalized_tool_choice,
            is_oauth=self._is_oauth,
            # Portal routes on ``anthropic/<slug>`` catalog ids and replays
            # signed thinking like native Anthropic; both carve-outs key off
            # base_url. Omitting it normalizes the id to a bare Anthropic
            # slug and the Portal Messages route cannot resolve it.
            base_url=self._base_url,
        )
        # Opus 4.7+ rejects any non-default temperature/top_p/top_k; only set
        # temperature for models that still accept it. build_anthropic_kwargs
        # additionally strips these keys as a safety net — keep both layers.
        if temperature is not None:
            from pcbdraft.model.anthropic_adapter import _forbids_sampling_params

            if not _forbids_sampling_params(model):
                anthropic_kwargs["temperature"] = temperature

        # Pass through caller-supplied extra_body so providers behind
        # Anthropic-compatible gateways receive their per-vendor request
        # fields (thinking control, metadata, portal tags, ...). The dict
        # form is the documented Anthropic SDK passthrough for non-standard
        # request body keys; merge on top of whatever build_anthropic_kwargs
        # already produced (e.g. fast-mode ``speed``) so call-time settings
        # survive. Two exclusions:
        #   - ``reasoning``: the OpenAI-shaped config dict is TRANSLATED into
        #     the native ``thinking`` field above (build_anthropic_kwargs);
        #     forwarding the raw field alongside would double-specify
        #     reasoning and 400 on strict gateways.
        #   - ``_``-prefixed keys: private Hermes plumbing (_reasoning_config
        #     et al.), never wire fields.
        caller_extra_body = kwargs.get("extra_body")
        if caller_extra_body and isinstance(caller_extra_body, dict):
            passthrough = {
                k: v
                for k, v in caller_extra_body.items()
                if k != "reasoning" and not str(k).startswith("_")
            }
            if passthrough:
                existing = anthropic_kwargs.get("extra_body") or {}
                if not isinstance(existing, dict):
                    existing = {}
                anthropic_kwargs["extra_body"] = {**existing, **passthrough}

        response = create_anthropic_message(
            self._client,
            anthropic_kwargs,
            # Tick the aux forward-progress hook per streamed event so hosts
            # watching liveness (gateway session hygiene) don't kill a
            # slow-but-generating summary model. No-op when no hook is
            # installed (None keeps the fast get_final_message path).
            on_stream_event=(
                (lambda _event: _notify_aux_progress())
                if _aux_progress_active()
                else None
            ),
        )
        _transport = get_transport("anthropic_messages")
        _nr = _transport.normalize_response(response, strip_tool_prefix=self._is_oauth)

        # ToolCall already duck-types as OpenAI shape (.type, .function.name,
        # .function.arguments) via properties, so no wrapping needed.
        assistant_message = SimpleNamespace(
            content=_nr.content,
            tool_calls=_nr.tool_calls,
            reasoning=_nr.reasoning,
        )
        finish_reason = _nr.finish_reason

        usage = None
        if hasattr(response, "usage") and response.usage:
            prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "output_tokens", 0) or 0
            total_tokens = getattr(response.usage, "total_tokens", 0) or (
                prompt_tokens + completion_tokens
            )
            usage = SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        choice = SimpleNamespace(
            index=0,
            message=assistant_message,
            finish_reason=finish_reason,
        )
        return SimpleNamespace(
            choices=[choice],
            model=model,
            usage=usage,
        )


class _AnthropicChatShim:
    def __init__(self, adapter: _AnthropicCompletionsAdapter):
        self.completions = adapter


class AnthropicAuxiliaryClient:
    """OpenAI-client-compatible wrapper over a native Anthropic client."""

    def __init__(
        self,
        real_client: Any,
        model: str,
        api_key: str,
        base_url: str,
        is_oauth: bool = False,
    ):
        self._real_client = real_client
        adapter = _AnthropicCompletionsAdapter(
            real_client,
            model,
            is_oauth=is_oauth,
            base_url=base_url,
        )
        self.chat = _AnthropicChatShim(adapter)
        self.api_key = api_key
        self.base_url = base_url

    def close(self):
        close_fn = getattr(self._real_client, "close", None)
        if callable(close_fn):
            close_fn()


class _AsyncAnthropicCompletionsAdapter:
    def __init__(self, sync_adapter: _AnthropicCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio

        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncAnthropicChatShim:
    def __init__(self, adapter: _AsyncAnthropicCompletionsAdapter):
        self.completions = adapter


class AsyncAnthropicAuxiliaryClient:
    def __init__(self, sync_wrapper: AnthropicAuxiliaryClient):
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncAnthropicCompletionsAdapter(sync_adapter)
        self.chat = _AsyncAnthropicChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url
        # See AsyncCodexAuxiliaryClient: mirror _real_client so cache
        # eviction on a poisoned underlying client also drops this entry.
        self._real_client = sync_wrapper._real_client


class _BedrockCompletionsAdapter:
    """Translates ``chat.completions.create(**kwargs)`` into Bedrock Converse."""

    def __init__(self, region: str, model: str):
        self._region = region
        self._model = model

    def create(self, **kwargs) -> Any:
        from pcbdraft.model.bedrock_adapter import call_converse

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)
        max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        # OpenAI accepts ``stop`` as str or list; Converse requires a list.
        stop = kwargs.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        if kwargs.get("tool_choice") is not None:
            # Converse's toolChoice isn't wired through call_converse();
            # no in-tree auxiliary caller passes tool_choice today. Surface
            # the drop instead of silently ignoring it.
            logger.debug(
                "BedrockAuxiliaryClient: tool_choice=%r not supported by the "
                "Converse shim — ignored.",
                kwargs.get("tool_choice"),
            )
        if kwargs.get("stream"):
            # Converse streaming isn't wired through this shim. Return a
            # complete response instead — call_llm's streaming consumer
            # detects a final object and downgrades to non-live output.
            logger.debug(
                "BedrockAuxiliaryClient: stream=True requested for %s — "
                "returning a complete response (Converse shim does not "
                "stream); caller downgrades to non-streaming.",
                model,
            )
        return call_converse(
            region=self._region,
            model=model,
            messages=messages,
            tools=kwargs.get("tools"),
            # Omitted/None caller cap → None: build_converse_kwargs then omits
            # inferenceConfig.maxTokens so Bedrock uses the model's maximum
            # allowed output, matching the no-cap-by-default policy every
            # other aux wire already follows (#10809: vision descriptions
            # stayed capped at the shim's old hardcoded 4096 on Bedrock).
            # Truthiness (not `is None`) is deliberate — it matches the
            # sibling Anthropic shim's reading of max_tokens above, so a
            # nonsense explicit 0 is treated as "no cap" on both wires.
            max_tokens=int(max_tokens) if max_tokens else None,
            temperature=kwargs.get("temperature"),
            top_p=kwargs.get("top_p"),
            stop_sequences=stop,
        )


class _BedrockChatShim:
    def __init__(self, adapter: _BedrockCompletionsAdapter):
        self.completions = adapter


class BedrockAuxiliaryClient:
    """OpenAI-client-compatible wrapper over AWS Bedrock Converse API."""

    def __init__(self, region: str, model: str):
        self._region = region
        self._model = model
        adapter = _BedrockCompletionsAdapter(region, model)
        self.chat = _BedrockChatShim(adapter)
        self.api_key = "aws-sdk"
        self.base_url = f"https://bedrock-runtime.{region}.amazonaws.com"

    def close(self):
        pass


class _AsyncBedrockCompletionsAdapter:
    def __init__(self, sync_adapter: _BedrockCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio

        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncBedrockChatShim:
    def __init__(self, adapter: _AsyncBedrockCompletionsAdapter):
        self.completions = adapter


class AsyncBedrockAuxiliaryClient:
    def __init__(self, sync_wrapper: BedrockAuxiliaryClient):
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncBedrockCompletionsAdapter(sync_adapter)
        self.chat = _AsyncBedrockChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url


__all__ = [
    "AnthropicAuxiliaryClient",
    "AsyncAnthropicAuxiliaryClient",
    "AsyncBedrockAuxiliaryClient",
    "AsyncCodexAuxiliaryClient",
    "BedrockAuxiliaryClient",
    "CodexAuxiliaryClient",
    "_AnthropicChatShim",
    "_AnthropicCompletionsAdapter",
    "_AsyncAnthropicChatShim",
    "_AsyncAnthropicCompletionsAdapter",
    "_AsyncBedrockChatShim",
    "_AsyncBedrockCompletionsAdapter",
    "_AsyncCodexChatShim",
    "_AsyncCodexCompletionsAdapter",
    "_BedrockChatShim",
    "_BedrockCompletionsAdapter",
    "_CodexChatShim",
    "_CodexCompletionsAdapter",
    "configure_auxiliary_adapter_runtime",
]
