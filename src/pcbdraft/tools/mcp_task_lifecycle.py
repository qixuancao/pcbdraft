# The compatibility behavior intentionally retains broad transport catches and
# best-effort cancellation cleanup from the original implementation.
# ruff: noqa: BLE001, S110
"""Transport and lifecycle ownership for one MCP server task.

The public :class:`MCPServerTask` remains in ``mcp_tool``.  That compatibility
class injects its live module namespace through ``_mcp_task_lifecycle_runtime``
so historical monkeypatch targets (SDK clients, clocks, process bookkeeping,
and logging) remain effective.  This module never imports ``mcp_tool``.

Authentication retry policy, configuration loading, tool discovery, and tool
registration remain owned by the compatibility module.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from typing import Any


class MCPTaskLifecycleMixin:
    """Own MCP transport setup, teardown, and per-server lifecycle state."""

    __slots__ = ()
    _MCP_CONTENT_TYPES = ("application/json", "text/event-stream")

    @staticmethod
    def _mcp_task_lifecycle_runtime() -> dict[str, Any]:
        """Return the compatibility module's live namespace."""

        raise NotImplementedError

    def _dep(self, name: str) -> Any:
        return self._mcp_task_lifecycle_runtime()[name]

    def __init__(self, name: str):
        self.name = name
        self.session: Any | None = None
        self.tool_timeout: float = self._dep("_DEFAULT_TOOL_TIMEOUT")
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._tools: list = []
        self._error: Exception | None = None
        self._config: dict = {}
        self._sampling: Any | None = None
        self._elicitation: Any | None = None
        self._registered_tool_names: list[str] = []
        self._reconnect_retries = 0
        self._session_proven = False
        self._was_parked = False
        self._auth_type = ""
        self._refresh_lock = asyncio.Lock()
        self._rpc_lock = asyncio.Lock()
        self._pending_refresh_tasks: set[asyncio.Task] = set()
        self._pending_call_context: contextvars.Context | None = None
        now = time.monotonic()
        self._lifecycle_started_at = now
        self._last_tool_call_at = now
        self._idle_timeout_seconds: float | None = None
        self._max_lifetime_seconds: float | None = None
        self._recycled_reason: str | None = None
        self.initialize_result: Any | None = None
        self._list_cache_meta: dict = {}
        self._ping_unsupported = False

    def _is_http(self) -> bool:
        return "url" in self._config

    async def _negotiate_session(self, session: Any, connect_timeout: float):
        """Negotiate handshake-era or stateless MCP protocol semantics."""

        logger = self._dep("logger")
        mode = str((self._config or {}).get("protocol", "auto")).lower().strip()
        if mode in ("stateless", "modern", "2026-07-28"):
            try:
                return await asyncio.wait_for(
                    session.discover(), timeout=connect_timeout
                )
            except (TimeoutError, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.info(
                    "MCP server '%s': server/discover rejected (%s) despite "
                    "protocol=%s — falling back to the legacy handshake",
                    self.name,
                    exc,
                    mode,
                )
                return await asyncio.wait_for(
                    session.initialize(), timeout=connect_timeout
                )
        if mode in ("legacy", "handshake"):
            return await asyncio.wait_for(session.initialize(), timeout=connect_timeout)
        if mode != "auto":
            logger.warning(
                "MCP server '%s': unknown protocol=%r — treating as 'auto' "
                "(valid: auto, stateless, legacy)",
                self.name,
                mode,
            )
        try:
            return await asyncio.wait_for(session.initialize(), timeout=connect_timeout)
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:
            if not self._dep("_handshake_rejected_as_modern")(exc):
                raise
            if not hasattr(session, "discover"):
                raise
            logger.info(
                "MCP server '%s': legacy handshake rejected (%s) — "
                "retrying via server/discover (2026-07-28 stateless server)",
                self.name,
                exc,
            )
            return await asyncio.wait_for(session.discover(), timeout=connect_timeout)

    def _is_recycled_stdio(self) -> bool:
        return not self._is_http() and self._recycled_reason is not None

    def mark_tool_call(self) -> None:
        self._last_tool_call_at = time.monotonic()

    def _mark_lifecycle_started(self) -> None:
        now = time.monotonic()
        self._lifecycle_started_at = now
        self._last_tool_call_at = now
        self._recycled_reason = None

    def _stdio_recycle_reason(self, now: float | None = None) -> str | None:
        if self._is_http() or self._rpc_lock.locked():
            return None
        now = time.monotonic() if now is None else now
        if (
            self._max_lifetime_seconds is not None
            and now - self._lifecycle_started_at >= self._max_lifetime_seconds
        ):
            return "max_lifetime_seconds"
        if (
            self._idle_timeout_seconds is not None
            and now - self._last_tool_call_at >= self._idle_timeout_seconds
        ):
            return "idle_timeout_seconds"
        return None

    def _next_stdio_recycle_deadline(self) -> float | None:
        if self._is_http() or self._rpc_lock.locked():
            return None
        deadlines = []
        if self._max_lifetime_seconds is not None:
            deadlines.append(self._lifecycle_started_at + self._max_lifetime_seconds)
        if self._idle_timeout_seconds is not None:
            deadlines.append(self._last_tool_call_at + self._idle_timeout_seconds)
        return min(deadlines) if deadlines else None

    def _mark_stdio_recycled(self, reason: str) -> None:
        self._recycled_reason = reason
        self.session = None

    async def _keepalive_probe(self) -> None:
        logger = self._dep("logger")
        if not self._ping_unsupported:
            try:
                await asyncio.wait_for(self.session.send_ping(), timeout=30.0)
                return
            except Exception as exc:
                if not self._dep("_is_method_not_found_error")(exc):
                    raise
                if not self._advertises_tools():
                    raise
                self._ping_unsupported = True
                logger.info(
                    "MCP server '%s': does not implement the optional 'ping' "
                    "utility (-32601); using 'list_tools' for keepalive on "
                    "this connection.",
                    self.name,
                )
        await asyncio.wait_for(self.session.list_tools(), timeout=30.0)

    def _mark_session_proven(self) -> None:
        if not self._session_proven:
            self._session_proven = True
            self._reconnect_retries = 0
            if self._was_parked:
                self._was_parked = False
                self._dep("logger").warning(
                    "MCP server '%s': revived — session healthy again after "
                    "parking (state: parked → connected)",
                    self.name,
                )

    async def _wait_for_lifecycle_event(self) -> str:
        keepalive_interval = max(
            self._dep("_MIN_KEEPALIVE_INTERVAL"),
            float(
                self._config.get(
                    "keepalive_interval",
                    self._dep("_DEFAULT_KEEPALIVE_INTERVAL"),
                )
            ),
        )
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        reconnect_task = asyncio.create_task(self._reconnect_event.wait())
        try:
            while True:
                recycle_reason = self._stdio_recycle_reason()
                if recycle_reason is not None:
                    self._mark_stdio_recycled(recycle_reason)
                    return "recycle"
                timeout = keepalive_interval
                recycle_deadline = self._next_stdio_recycle_deadline()
                if recycle_deadline is not None:
                    timeout = max(
                        0.0, min(timeout, recycle_deadline - time.monotonic())
                    )
                done, _pending = await asyncio.wait(
                    {shutdown_task, reconnect_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
                recycle_reason = self._stdio_recycle_reason()
                if recycle_reason is not None:
                    self._mark_stdio_recycled(recycle_reason)
                    return "recycle"
                if self.session:
                    try:
                        await self._keepalive_probe()
                    except Exception as exc:
                        root = self._dep("_unwrap_exception_group")(exc)
                        self._dep("logger").warning(
                            "MCP server '%s' keepalive failed, triggering "
                            "reconnect (state: connected → degraded): %s: %s",
                            self.name,
                            type(root).__name__,
                            root,
                        )
                        self._reconnect_event.set()
                        break
                    self._mark_session_proven()
        finally:
            for task in (shutdown_task, reconnect_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
        if self._shutdown_event.is_set():
            return "shutdown"
        self._reconnect_event.clear()
        return "reconnect"

    async def _wait_for_reconnect_or_shutdown(
        self, timeout: float | None = None
    ) -> str:
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        reconnect_task = asyncio.ensure_future(self._reconnect_event.wait())
        try:
            await asyncio.wait(
                {shutdown_task, reconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout,
            )
        finally:
            for task in (shutdown_task, reconnect_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
        if self._shutdown_event.is_set():
            return "shutdown"
        self._reconnect_event.clear()
        return "reconnect"

    def _reconnect_or_reraise_group(self, group: BaseExceptionGroup) -> str:
        if self._shutdown_event.is_set():
            raise group
        fatal, _rest = group.split((KeyboardInterrupt, SystemExit))
        if fatal is not None:
            raise group
        cancelled, _rest = group.split(asyncio.CancelledError)
        if cancelled is not None:
            raise group
        if not self._ready.is_set():
            raise group
        self._dep("logger").debug(
            "MCP server '%s': transport TaskGroup exited after a live session "
            "(%r) — reconnecting immediately instead of backing off",
            self.name,
            group,
        )
        return "reconnect"

    async def start(self, config: dict):
        self._task = asyncio.ensure_future(self.run(config))
        try:
            await self._ready.wait()
        except asyncio.CancelledError:
            if self._task and not self._task.done():
                self._task.cancel()
            raise
        if self._error:
            raise self._error

    async def shutdown(self):
        self._shutdown_event.set()
        self._reconnect_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._dep("logger").warning(
                    "MCP server '%s' shutdown timed out, cancelling task",
                    self.name,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        if self._pending_refresh_tasks:
            for task in list(self._pending_refresh_tasks):
                task.cancel()
            await asyncio.gather(*self._pending_refresh_tasks, return_exceptions=True)
            self._pending_refresh_tasks.clear()
        self._deregister_tools()
        self.session = None

    def _deregister_tools(self) -> None:
        from pcbdraft.tools.registry import registry

        forget = self._dep("_forget_mcp_tool_server")
        for tool_name in list(getattr(self, "_registered_tool_names", [])):
            registry.deregister(tool_name)
            forget(tool_name)
        self._registered_tool_names = []

    async def _wait_for_lazy_reconnect(self) -> None:
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        reconnect_task = asyncio.create_task(self._reconnect_event.wait())
        try:
            await asyncio.wait(
                {shutdown_task, reconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (shutdown_task, reconnect_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _run_stdio(self, config: dict):
        """Run one stdio transport cycle and account for its process tree."""

        runtime = self._mcp_task_lifecycle_runtime()
        logger = runtime["logger"]
        if config.get("identity_header") is not None:
            logger.warning(
                "MCP server '%s': identity_header is only supported on "
                "HTTP/SSE transports — ignored for stdio servers",
                self.name,
            )
        if not runtime["_ensure_mcp_sdk"]():
            raise ImportError(
                f"MCP server '{self.name}' requires the 'mcp' Python SDK, but "
                "it is not installed. Run `pcbdraft doctor` for dependency "
                "diagnostics, then retry."
            )

        command = config.get("command")
        args = config.get("args", [])
        if not command:
            raise ValueError(f"MCP server '{self.name}' has no 'command' in config")
        safe_env = runtime["_build_safe_env"](config.get("env"))
        command, safe_env = runtime["_resolve_stdio_command"](command, safe_env)

        from pcbdraft.tools.osv_check import check_package_for_malware

        malware_timeout = runtime["_OSV_MALWARE_CHECK_TIMEOUT_S"]
        try:
            malware_error = await asyncio.wait_for(
                asyncio.to_thread(check_package_for_malware, command, args),
                timeout=malware_timeout,
            )
        except TimeoutError:
            logger.warning(
                "MCP server '%s': OSV malware preflight timed out after %.0fs "
                "(network slow/unreachable) — proceeding without the check.",
                self.name,
                malware_timeout,
            )
            malware_error = None
        if malware_error:
            raise ValueError(f"MCP server '{self.name}': {malware_error}")

        command, args = runtime["_wrap_command_with_watchdog"](command, args)
        server_params = runtime["StdioServerParameters"](
            command=command,
            args=args,
            env=safe_env if safe_env else None,
            cwd=config.get("cwd"),
            encoding_error_handler="replace",
        )
        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            sampling_kwargs.update(self._elicitation.session_kwargs())
        if (
            runtime["_MCP_NOTIFICATION_TYPES"]
            and runtime["_MCP_MESSAGE_HANDLER_SUPPORTED"]
        ):
            sampling_kwargs["message_handler"] = self._make_message_handler()
        if runtime["_MCP_LOGGING_CALLBACK_SUPPORTED"]:
            sampling_kwargs["logging_callback"] = self._make_logging_callback()

        await asyncio.to_thread(runtime["_kill_orphaned_mcp_children"])
        pids_before = runtime["_snapshot_child_pids"]()
        new_pids: set[int] = set()
        runtime["_write_stderr_log_header"](self.name)
        errlog = runtime["_get_mcp_stderr_log"]()
        try:
            async with runtime["stdio_client"](server_params, errlog=errlog) as (
                read_stream,
                write_stream,
            ):
                new_pids = runtime["_filter_mcp_children"](
                    runtime["_snapshot_child_pids"]() - pids_before
                )
                if new_pids:
                    new_pgids: dict[int, int] = {}
                    for pid in new_pids:
                        try:
                            new_pgids[pid] = os.getpgid(pid)
                        except (AttributeError, ProcessLookupError, OSError):
                            pass
                    with runtime["_lock"]:
                        for pid in new_pids:
                            runtime["_stdio_pids"][pid] = self.name
                        runtime["_stdio_pgids"].update(new_pgids)
                async with runtime["ClientSession"](
                    read_stream,
                    write_stream,
                    client_info=runtime["_pcbdraft_mcp_client_info"](),
                    **sampling_kwargs,
                ) as session:
                    connect_timeout = float(
                        config.get(
                            "connect_timeout", runtime["_DEFAULT_CONNECT_TIMEOUT"]
                        )
                    )
                    self.initialize_result = await self._negotiate_session(
                        session, connect_timeout
                    )
                    self.session = session
                    self._mark_lifecycle_started()
                    await self._discover_tools()
                    self._ready.set()
                    runtime["_reset_server_error"](self.name)
                    self._session_proven = False
                    return await self._wait_for_lifecycle_event()
        finally:
            if new_pids:
                from pcbdraft.core.runtime_process import _pid_exists

                killpg = getattr(os, "killpg", None)
                with runtime["_lock"]:
                    for pid in new_pids:
                        runtime["_stdio_pids"].pop(pid, None)
                    for pid in new_pids:
                        pid_alive = _pid_exists(pid)
                        pgroup_alive = False
                        pgid = runtime["_stdio_pgids"].get(pid)
                        if not pid_alive and pgid is not None and killpg is not None:
                            try:
                                killpg(pgid, 0)
                                pgroup_alive = True
                            except (ProcessLookupError, PermissionError, OSError):
                                pgroup_alive = False
                        if pid_alive or pgroup_alive:
                            runtime["_orphan_stdio_pids"].add(pid)
                            runtime["_orphan_stdio_pid_servers"][pid] = self.name
                        else:
                            runtime["_stdio_pgids"].pop(pid, None)

    async def _preflight_content_type(
        self,
        url: str,
        *,
        headers: dict | None = None,
        ssl_verify: bool = True,
        client_cert: Any = None,
        timeout: float = 5.0,
    ) -> None:
        """Reject an unambiguous HTML/non-MCP endpoint before SDK startup."""

        try:
            import httpx
        except ImportError:
            return
        client_kwargs: dict[str, Any] = {
            "verify": ssl_verify,
            "follow_redirects": True,
            "timeout": httpx.Timeout(timeout),
        }
        if client_cert is not None:
            client_kwargs["cert"] = client_cert
        probe_headers = dict(headers) if headers else {}
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.head(url, headers=probe_headers)
                if response.status_code in (405, 501):
                    response = await client.get(url, headers=probe_headers)
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if (
                    content_type
                    and content_type not in self._MCP_CONTENT_TYPES
                    and 200 <= response.status_code < 300
                ):
                    post_response = await client.post(
                        url,
                        headers={
                            **probe_headers,
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                        },
                        content=(
                            '{"jsonrpc":"2.0","id":"_probe",'
                            '"method":"initialize",'
                            '"params":{"protocolVersion":"2025-03-26",'
                            '"capabilities":{},'
                            '"clientInfo":{"name":"pcbdraft-probe",'
                            '"version":"0.1"}}}'
                        ),
                    )
                    if 200 <= post_response.status_code < 300:
                        post_type = (
                            post_response.headers.get("content-type", "")
                            .split(";")[0]
                            .strip()
                            .lower()
                        )
                        if post_type in self._MCP_CONTENT_TYPES:
                            response = post_response
        except httpx.HTTPError:
            return
        if not (200 <= response.status_code < 300):
            return
        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        if not content_type or content_type in self._MCP_CONTENT_TYPES:
            return
        error_type = self._dep("NonMcpEndpointError")
        raise error_type(
            f"MCP server '{self.name}' at {url} returned Content-Type "
            f"'{content_type}', not an MCP response (expected one of: "
            f"{', '.join(self._MCP_CONTENT_TYPES)}). The URL most likely "
            "points at a web page rather than an MCP endpoint — check it "
            "resolves to a Streamable HTTP / SSE endpoint "
            "(e.g. https://host/mcp, not https://host/)."
        )

    def _transport_session_kwargs(self, runtime: dict[str, Any]) -> dict[str, Any]:
        kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            kwargs.update(self._elicitation.session_kwargs())
        if (
            runtime["_MCP_NOTIFICATION_TYPES"]
            and runtime["_MCP_MESSAGE_HANDLER_SUPPORTED"]
        ):
            kwargs["message_handler"] = self._make_message_handler()
        if runtime["_MCP_LOGGING_CALLBACK_SUPPORTED"]:
            kwargs["logging_callback"] = self._make_logging_callback()
        return kwargs

    async def _serve_http_streams(
        self,
        read_stream: Any,
        write_stream: Any,
        *,
        connect_timeout: float,
        sampling_kwargs: dict[str, Any],
    ) -> str:
        runtime = self._mcp_task_lifecycle_runtime()
        async with runtime["ClientSession"](
            read_stream,
            write_stream,
            client_info=runtime["_pcbdraft_mcp_client_info"](),
            **sampling_kwargs,
        ) as session:
            self.initialize_result = await self._negotiate_session(
                session, connect_timeout
            )
            self.session = session
            await self._discover_tools()
            self._ready.set()
            runtime["_reset_server_error"](self.name)
            self._session_proven = False
            return await self._wait_for_lifecycle_event()

    async def _run_http(self, config: dict):
        """Run one SSE or Streamable HTTP transport cycle."""

        runtime = self._mcp_task_lifecycle_runtime()
        logger = runtime["logger"]
        runtime["_ensure_mcp_sdk"]()
        if not runtime["_MCP_HTTP_AVAILABLE"]:
            raise ImportError(
                f"MCP server '{self.name}' requires HTTP transport but "
                "mcp.client.streamable_http is not available. Upgrade the "
                "mcp package to get HTTP support."
            )

        url = config["url"]
        headers = dict(config.get("headers") or {})
        strict_headers = bool(config.get("strict_redirect_headers"))
        configured_header_names = {key.lower() for key in headers}
        headers = runtime["_apply_identity_header"](self.name, config, headers)
        if not any(key.lower() == "mcp-protocol-version" for key in headers):
            headers["mcp-protocol-version"] = runtime["LATEST_HANDSHAKE_VERSION"]
        connect_timeout = float(
            config.get("connect_timeout", runtime["_DEFAULT_CONNECT_TIMEOUT"])
        )
        ssl_verify = config.get("ssl_verify", True)
        client_cert = runtime["_resolve_client_cert"](self.name, config)

        oauth_auth = None
        if self._auth_type == "oauth":
            try:
                from pcbdraft.tools.mcp_oauth_manager import get_manager

                oauth_auth = get_manager().get_or_build_provider(
                    self.name, url, config.get("oauth")
                )
            except Exception as exc:
                logger.warning("MCP OAuth setup failed for '%s': %s", self.name, exc)
                raise

        sampling_kwargs = self._transport_session_kwargs(runtime)
        if config.get("transport") == "sse":
            if strict_headers:
                raise ValueError(
                    f"MCP server '{self.name}': strict_redirect_headers is "
                    "not supported on the SSE transport."
                )
            sse_client = runtime["sse_client"]
            if sse_client is None:
                raise ImportError(
                    f"MCP server '{self.name}' requires SSE transport but "
                    "mcp.client.sse.sse_client is not available. Upgrade the "
                    "mcp package to get SSE support."
                )
            sse_kwargs: dict[str, Any] = {
                "url": url,
                "headers": headers or None,
                "timeout": connect_timeout,
                "sse_read_timeout": 300.0,
            }
            if oauth_auth is not None:
                sse_kwargs["auth"] = oauth_auth
            if client_cert is not None or ssl_verify is not True:
                httpx_module = runtime["sdk_httpx"]()
                cert_for_factory = client_cert
                verify_for_factory = ssl_verify

                def mcp_http_client_factory(headers=None, timeout=None, auth=None):
                    kwargs: dict[str, Any] = {
                        "follow_redirects": True,
                        "verify": verify_for_factory,
                        "timeout": timeout
                        if timeout is not None
                        else httpx_module.Timeout(30.0, read=300.0),
                    }
                    if headers is not None:
                        kwargs["headers"] = headers
                    if auth is not None:
                        kwargs["auth"] = auth
                    if cert_for_factory is not None:
                        kwargs["cert"] = cert_for_factory
                    return httpx_module.AsyncClient(**kwargs)

                sse_kwargs["httpx_client_factory"] = mcp_http_client_factory
            try:
                async with sse_client(**sse_kwargs) as (
                    read_stream,
                    write_stream,
                ):
                    reason = await self._serve_http_streams(
                        read_stream,
                        write_stream,
                        connect_timeout=connect_timeout,
                        sampling_kwargs=sampling_kwargs,
                    )
                    if reason == "reconnect":
                        logger.info(
                            "MCP server '%s': reconnect requested — "
                            "tearing down SSE session",
                            self.name,
                        )
            except BaseExceptionGroup as group:
                reason = self._reconnect_or_reraise_group(group)
            return reason

        if runtime["_MCP_NEW_HTTP"]:
            httpx_module = runtime["sdk_httpx"]()
            original_url = httpx_module.URL(url)
            redirect_hook = runtime["_make_redirect_header_stripper"](
                original_url,
                strict=strict_headers,
                configured_header_names=configured_header_names,
            )
            client_kwargs: dict[str, Any] = {
                "follow_redirects": True,
                "timeout": httpx_module.Timeout(connect_timeout, read=300.0),
                "verify": ssl_verify,
                "event_hooks": {"response": [redirect_hook]},
            }
            if headers:
                client_kwargs["headers"] = headers
            if oauth_auth is not None:
                client_kwargs["auth"] = oauth_auth
            if client_cert is not None:
                client_kwargs["cert"] = client_cert
            try:
                async with (
                    httpx_module.AsyncClient(**client_kwargs) as http_client,
                    runtime["streamable_http_client"](
                        url, http_client=http_client
                    ) as streams,
                ):
                    reason = await self._serve_http_streams(
                        streams[0],
                        streams[1],
                        connect_timeout=connect_timeout,
                        sampling_kwargs=sampling_kwargs,
                    )
                    if reason == "reconnect":
                        logger.info(
                            "MCP server '%s': reconnect requested — "
                            "tearing down HTTP session",
                            self.name,
                        )
            except BaseExceptionGroup as group:
                reason = self._reconnect_or_reraise_group(group)
            return reason

        if strict_headers:
            raise ImportError(
                f"MCP server '{self.name}' requires mcp >= 1.24.0 to enforce "
                "the portable redirect-header boundary "
                "(strict_redirect_headers). Upgrade the mcp package."
            )
        http_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": connect_timeout,
            "verify": ssl_verify,
        }
        if oauth_auth is not None:
            http_kwargs["auth"] = oauth_auth
        try:
            async with runtime["streamablehttp_client"](url, **http_kwargs) as streams:
                read_stream, write_stream, _get_session_id = streams
                reason = await self._serve_http_streams(
                    read_stream,
                    write_stream,
                    connect_timeout=connect_timeout,
                    sampling_kwargs=sampling_kwargs,
                )
                if reason == "reconnect":
                    logger.info(
                        "MCP server '%s': reconnect requested — "
                        "tearing down legacy HTTP session",
                        self.name,
                    )
        except BaseExceptionGroup as group:
            reason = self._reconnect_or_reraise_group(group)
        return reason
