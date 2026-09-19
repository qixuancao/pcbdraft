# mypy: disable-error-code="attr-defined,has-type"
"""Own request-scoped OpenAI and Anthropic client lifecycles."""

# Socket teardown is deliberately best-effort so abort cleanup cannot mask
# the request error or interrupt that triggered it.
# ruff: noqa: BLE001

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pcbdraft.core.runtime_utils import base_url_host_matches
from pcbdraft.model.timeouts import get_provider_request_timeout

logger = logging.getLogger(__name__)

_base_url_host_matches_hook: Callable[[str, str], bool]
_provider_request_timeout_hook: Callable[[str, str], float | None]
_debug_hook: Callable[..., None]
_info_hook: Callable[..., None]
_warning_hook: Callable[..., None]


def configure_request_client_lifecycle_runtime(
    *,
    base_url_host_matches_fn: Callable[[str, str], bool] | None = None,
    provider_request_timeout: Callable[[str, str], float | None] | None = None,
    debug: Callable[..., None] | None = None,
    info: Callable[..., None] | None = None,
    warning: Callable[..., None] | None = None,
) -> None:
    """Inject late-bound helpers exposed by the legacy agent module."""
    global _base_url_host_matches_hook
    global _provider_request_timeout_hook
    global _debug_hook
    global _info_hook
    global _warning_hook

    if base_url_host_matches_fn is not None:
        _base_url_host_matches_hook = base_url_host_matches_fn
    if provider_request_timeout is not None:
        _provider_request_timeout_hook = provider_request_timeout
    if debug is not None:
        _debug_hook = debug
    if info is not None:
        _info_hook = info
    if warning is not None:
        _warning_hook = warning


class RequestClientLifecycleMixin:
    """Create, cache, close, and abort clients owned by one request."""

    # Close reasons the request workers' own ``finally`` unwind reports for
    # a request that produced a response — the only closes that both come
    # from the thread that owns the pool's FDs AND attest a healthy pool.
    # Only these may keep the wire client for the next call, and poisoning
    # still wins: a cross-thread abort (#29507) marks the slot so even a
    # worker-finally close discards it. Every other reason (error cleanups,
    # stale/interrupt kills, retry cleanups) gets a real close, so a retry
    # after a request error always builds a fresh pool.
    _REQUEST_CLIENT_REUSE_REASONS = frozenset(
        {
            "request_complete",
            "stream_request_complete",
        }
    )

    def _request_client_cache_ref(self) -> dict:
        # Lazy init — tests build agents via AIAgent.__new__ without __init__.
        cache = getattr(self, "_request_client_cache", None)
        if cache is None:
            cache = {"client": None, "kwargs": None, "poisoned": False, "in_use": False}
            self._request_client_cache = cache
        return cache

    def _create_request_openai_client(
        self, *, reason: str, api_kwargs: dict | None = None
    ) -> Any:
        from unittest.mock import Mock

        primary_client = self._ensure_primary_openai_client(reason=reason)
        if self.provider == "moa":
            return primary_client
        if isinstance(primary_client, Mock):
            return primary_client
        with self._openai_client_lock():
            request_kwargs = dict(self._client_kwargs)
        # Per-request OpenAI-wire clients (used by both the non-streaming
        # chat-completions path and the streaming chat-completions path
        # in `_interruptible_api_call`) should not run the SDK's built-in
        # retry loop: the agent's outer loop owns retries with credential
        # rotation, provider fallback, and backoff that the SDK can't
        # see. Leaving SDK retries on (default 2) compounds with our outer
        # retries and lets a single hung provider request stretch to ~3x
        # the per-call timeout before our stale detector reports it.
        # Shared/primary clients and Anthropic / Bedrock paths are
        # unaffected (they don't go through here).
        request_kwargs["max_retries"] = 0
        if _base_url_host_matches_hook(
            str(request_kwargs.get("base_url", "")), "githubcopilot.com"
        ) and self._api_kwargs_have_image_parts(api_kwargs or {}):
            request_kwargs["default_headers"] = self._copilot_headers_for_request(
                is_vision=True
            )
        # Reuse the cached wire client while the effective kwargs are
        # unchanged: constructing openai.OpenAI + its httpx pool costs
        # ~19-35ms per LLM call (fresh TCP+TLS handshake), ~5x per turn.
        # The cache is a single checked-out slot: `in_use` prevents two
        # concurrent calls from sharing one pool's close/abort lifecycle
        # (a second concurrent call gets a fresh untracked client with
        # the old build-per-request behavior).
        stale = None
        with self._openai_client_lock():
            cache = self._request_client_cache_ref()
            cached = cache["client"]
            if cached is not None and not cache["in_use"]:
                if (
                    not cache["poisoned"]
                    and cache["kwargs"] == request_kwargs
                    and not self._is_openai_client_closed(cached)
                ):
                    cache["in_use"] = True
                    return cached
                # kwargs changed (credential rotation, provider failover),
                # poisoned by a cross-thread abort (#29507), or externally
                # closed — never reuse; discard and rebuild below.
                stale = cached
                cache["client"] = None
                cache["kwargs"] = None
                cache["poisoned"] = False
        if stale is not None:
            # Safe to close from this thread: in_use was False, so no
            # worker thread owns the pool's FDs (#29507 concerns clients
            # with an in-flight request on another thread).
            self._close_openai_client(
                stale, reason=f"reuse_evict:{reason}", shared=False
            )
        client = self._create_openai_client(request_kwargs, reason=reason, shared=False)
        with self._openai_client_lock():
            cache = self._request_client_cache_ref()
            if cache["client"] is None:
                cache["client"] = client
                # Snapshot nested dicts (default_headers): rotation sites
                # assign fresh inner dicts today, but an aliased inner
                # object would compare equal even after in-place mutation.
                cache["kwargs"] = {
                    k: dict(v) if isinstance(v, dict) else v
                    for k, v in request_kwargs.items()
                }
                cache["poisoned"] = False
                cache["in_use"] = True
            # else: a concurrent call holds the slot — hand this client
            # out untracked; _close_request_openai_client fully closes
            # untracked clients, preserving the per-request lifecycle.
        return client

    def _close_request_openai_client(self, client: Any, *, reason: str) -> None:
        with self._openai_client_lock():
            cache = self._request_client_cache_ref()
            if cache["client"] is client:
                if (
                    reason in self._REQUEST_CLIENT_REUSE_REASONS
                    and not cache["poisoned"]
                ):
                    # Clean finish on the owning thread — keep the wire client
                    # (and its warm httpx pool) for the next sequential call.
                    cache["in_use"] = False
                    return
                # Failure / kill / abort outcome: drop the slot and fall
                # through to a real close. This runs on the owning worker
                # thread, which is where the FD release belongs (#29507).
                cache["client"] = None
                cache["kwargs"] = None
                cache["poisoned"] = False
                cache["in_use"] = False
        self._close_openai_client(client, reason=reason, shared=False)

    def _abort_request_openai_client(self, client: Any, *, reason: str) -> None:
        """Cross-thread abort: shut sockets down without releasing FDs.

        Companion to :meth:`_close_request_openai_client` for stranger-thread
        callers (interrupt-check loop, stale-call detector). Calling
        ``client.close()`` from a thread that does not own the active httpx
        connection raced the still-live SSL BIO and corrupted unrelated file
        descriptors when the kernel recycled the just-freed TCP FD (#29507).

        Here we only ``shutdown(SHUT_RDWR)`` the pool's sockets. That unblocks
        the owning worker thread's pending ``recv``/``send`` with an EOF or
        ``EPIPE`` so it can unwind and close ``client`` from its own context
        — which is where the FD release belongs.
        """
        if client is None:
            return
        # A pool whose sockets were shut down from a stranger thread must
        # never be reused: poison the cache slot so the owner-thread close
        # discards it and the next create builds a fresh client.
        with self._openai_client_lock():
            cache = self._request_client_cache_ref()
            if cache["client"] is client:
                cache["poisoned"] = True
        try:
            shutdown_count = self._force_close_tcp_sockets(client)
            # tcp_force_closed=0 means the stranger-thread abort found no
            # sockets to shut down — the worker stays blocked in recv and the
            # provider keeps the slot (#72975). Surface that as WARNING so it
            # cannot be mistaken for a successful abort in the logs.
            _log = _warning_hook if shutdown_count == 0 else _info_hook
            _log(
                "OpenAI client aborted (%s, shared=False, tcp_force_closed=%d, "
                "deferred_close=stranger_thread) %s%s",
                reason,
                shutdown_count,
                self._client_log_context(),
                (
                    " — no sockets found; in-flight request may keep running "
                    "until the provider finishes"
                    if shutdown_count == 0
                    else ""
                ),
            )
        except Exception as exc:
            _debug_hook(
                "OpenAI client abort failed (%s, shared=False) %s error=%s",
                reason,
                self._client_log_context(),
                exc,
            )

    def _request_anthropic_client_cache_ref(self) -> dict:
        # Lazy init — tests build agents via AIAgent.__new__ without __init__.
        cache = getattr(self, "_request_anthropic_client_cache", None)
        if cache is None:
            cache = {"client": None, "key": None, "poisoned": False, "in_use": False}
            self._request_anthropic_client_cache = cache
        return cache

    def _request_anthropic_client_key(self) -> tuple:
        """Cache key covering everything that forces a fresh client: credential
        rotation, base URL / region changes, timeout changes (model switch),
        and the 1M-context beta flag."""
        if getattr(self, "provider", None) == "bedrock":
            region = getattr(self, "_bedrock_region", "us-east-1") or "us-east-1"
            return ("bedrock", region)
        return (
            "direct",
            self._anthropic_api_key,
            getattr(self, "_anthropic_base_url", None),
            _provider_request_timeout_hook(self.provider, self.model),
            bool(getattr(self, "_oauth_1m_beta_disabled", False)),
        )

    def _create_request_anthropic_client(self, *, reason: str) -> Any:
        """Build (or reuse) a request-local Anthropic client for one in-flight call.

        The shared ``_anthropic_client`` stays the long-lived primary, but the
        stale/interrupt watchdog runs on the poll thread and must never call
        ``close()`` on the client whose TLS socket a worker thread is still
        reading: releasing that FD from a stranger thread lets the kernel
        recycle it under a still-live SSL BIO, which then writes a TLS record
        into an unrelated SQLite header (#29507 / #67142). A per-request client
        lets the stranger thread ``shutdown()`` the socket while the owning
        worker performs the SDK-level close from its own context — the same
        ownership contract the OpenAI-wire path already uses.

        Also mirrors the OpenAI-wire path's single-slot cache
        (``_create_request_openai_client``): building ``anthropic.Anthropic``
        means a fresh httpx pool and TCP+TLS handshake per call, so the client
        is kept warm across sequential calls whose cache key (credentials,
        base URL/region, timeout, 1M-beta flag) hasn't changed. ``in_use``
        keeps a second concurrent call from sharing one pool's close/abort
        lifecycle — it gets a fresh untracked client instead.

        Mirrors ``_rebuild_anthropic_client`` construction (direct + Bedrock,
        1M-beta drop) but returns a fresh/cached client instead of swapping
        the shared one.
        """
        if self.api_mode == "anthropic_messages":
            self._try_refresh_anthropic_client_credentials()
        key = self._request_anthropic_client_key()

        stale = None
        with self._openai_client_lock():
            cache = self._request_anthropic_client_cache_ref()
            cached = cache["client"]
            if cached is not None and not cache["in_use"]:
                if (
                    not cache["poisoned"]
                    and cache["key"] == key
                    and not self._is_openai_client_closed(cached)
                ):
                    cache["in_use"] = True
                    return cached
                # Key changed (credential rotation, base URL/region, timeout,
                # 1M-beta flip), poisoned by a cross-thread abort, or
                # externally closed — never reuse; discard and rebuild below.
                stale = cached
                cache["client"] = None
                cache["key"] = None
                cache["poisoned"] = False
        if stale is not None:
            # Safe to close from this thread: in_use was False, so no worker
            # thread owns the pool's FDs (same #29507 reasoning as OpenAI).
            self._close_request_anthropic_client(stale, reason=f"reuse_evict:{reason}")

        if key[0] == "bedrock":
            from pcbdraft.model.anthropic_adapter import build_anthropic_bedrock_client

            client = build_anthropic_bedrock_client(key[1])
        else:
            from pcbdraft.model.anthropic_adapter import build_anthropic_client

            client = build_anthropic_client(
                self._anthropic_api_key,
                getattr(self, "_anthropic_base_url", None),
                timeout=_provider_request_timeout_hook(self.provider, self.model),
                drop_context_1m_beta=key[4],
            )
        _debug_hook(
            "Anthropic request client created (%s, shared=False) provider=%s model=%s",
            reason,
            getattr(self, "provider", None),
            getattr(self, "model", None),
        )
        with self._openai_client_lock():
            cache = self._request_anthropic_client_cache_ref()
            if cache["client"] is None:
                cache["client"] = client
                cache["key"] = key
                cache["poisoned"] = False
                cache["in_use"] = True
            # else: a concurrent call holds the slot — hand this client out
            # untracked; _close_request_anthropic_client fully closes
            # untracked clients, preserving the per-request lifecycle.
        return client

    def _close_request_anthropic_client(self, client: Any, *, reason: str) -> None:
        """Owner-thread close of a request-local Anthropic client.

        On a clean finish (``reason`` in ``_REQUEST_CLIENT_REUSE_REASONS``)
        the pool is kept warm in the cache slot for the next sequential call,
        mirroring ``_close_request_openai_client``. Any other outcome
        (error / kill / abort / stale-slot eviction) force-closes the pool's
        TCP sockets first (CLOSE-WAIT hygiene, parity with
        ``_close_openai_client``), then does the graceful SDK close. Safe
        because the caller owns the connection.
        """
        if client is None:
            return
        with self._openai_client_lock():
            cache = self._request_anthropic_client_cache_ref()
            if cache["client"] is client:
                if (
                    reason in self._REQUEST_CLIENT_REUSE_REASONS
                    and not cache["poisoned"]
                ):
                    cache["in_use"] = False
                    return
                cache["client"] = None
                cache["key"] = None
                cache["poisoned"] = False
                cache["in_use"] = False
        try:
            self._force_close_tcp_sockets(client)
            client.close()
            _info_hook(
                "Anthropic client closed (%s, shared=False) provider=%s model=%s",
                reason,
                getattr(self, "provider", None),
                getattr(self, "model", None),
            )
        except Exception as exc:
            _debug_hook(
                "Anthropic client close failed (%s, shared=False) provider=%s model=%s error=%s",
                reason,
                getattr(self, "provider", None),
                getattr(self, "model", None),
                exc,
            )

    def _abort_request_anthropic_client(self, client: Any, *, reason: str) -> None:
        """Cross-thread abort for request-local Anthropic clients.

        Stranger threads (the interrupt-check / stale-stream detector loop)
        must not call the SDK ``close()`` — that races the owning worker's live
        SSL BIO and can recycle a TLS FD into a SQLite header (#29507 /
        #67142). Only ``shutdown(SHUT_RDWR)`` the pool's sockets so the worker
        unblocks and releases the FD from its own thread.
        """
        if client is None:
            return
        # A pool whose sockets were shut down from a stranger thread must
        # never be reused: poison the cache slot so the owner-thread close
        # discards it and the next create builds a fresh client.
        with self._openai_client_lock():
            cache = self._request_anthropic_client_cache_ref()
            if cache["client"] is client:
                cache["poisoned"] = True
        try:
            shutdown_count = self._force_close_tcp_sockets(client)
            # Same visibility contract as the OpenAI abort path (#72975):
            # zero sockets shut down means the abort did not unblock the
            # worker — log WARNING, not a success-shaped INFO.
            _log = _warning_hook if shutdown_count == 0 else _info_hook
            _log(
                "Anthropic client aborted (%s, shared=False, tcp_force_closed=%d, "
                "deferred_close=stranger_thread) provider=%s model=%s%s",
                reason,
                shutdown_count,
                getattr(self, "provider", None),
                getattr(self, "model", None),
                (
                    " — no sockets found; in-flight request may keep running "
                    "until the provider finishes"
                    if shutdown_count == 0
                    else ""
                ),
            )
        except Exception as exc:
            _debug_hook(
                "Anthropic client abort failed (%s, shared=False) provider=%s model=%s error=%s",
                reason,
                getattr(self, "provider", None),
                getattr(self, "model", None),
                exc,
            )


_base_url_host_matches_hook = base_url_host_matches
_provider_request_timeout_hook = get_provider_request_timeout
_debug_hook = logger.debug
_info_hook = logger.info
_warning_hook = logger.warning
