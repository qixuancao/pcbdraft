# ruff: noqa: BLE001, RUF023
"""Per-server MCP connection and retry coordination.

The compatibility module injects its live namespace so historical patch paths
for SDK symbols, logging, feature flags, authentication policy, and discovery
helpers remain effective. Transport setup and teardown stay implemented by
``MCPTaskLifecycleMixin``; this module does not duplicate that lifecycle and
never imports ``mcp_tool``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pcbdraft.tools import mcp_tool_discovery as _mcp_tool_discovery
from pcbdraft.tools.mcp_task_lifecycle import MCPTaskLifecycleMixin

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_server_task_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP server task runtime has not been configured")
    return _runtime_namespace()


class MCPServerTask(MCPTaskLifecycleMixin):
    """Manages a single MCP server connection in a dedicated asyncio Task.

    The entire connection lifecycle (connect, discover, serve, disconnect)
    runs inside one asyncio Task so that anyio cancel-scopes created by
    the transport client are entered and exited in the same Task context.

    Supports both stdio and HTTP/StreamableHTTP transports.
    """

    __slots__ = (
        "name",
        "session",
        "tool_timeout",
        "_task",
        "_ready",
        "_shutdown_event",
        "_reconnect_event",
        "_tools",
        "_error",
        "_config",
        "_sampling",
        "_elicitation",
        "_registered_tool_names",
        "_auth_type",
        "_refresh_lock",
        "_rpc_lock",
        "_pending_refresh_tasks",
        "_pending_call_context",
        "_lifecycle_started_at",
        "_last_tool_call_at",
        "_idle_timeout_seconds",
        "_max_lifetime_seconds",
        "_recycled_reason",
        "initialize_result",
        "_ping_unsupported",
        "_list_cache_meta",
        "_reconnect_retries",
        "_session_proven",
        "_was_parked",
    )

    @staticmethod
    def _mcp_task_lifecycle_runtime() -> dict[str, Any]:
        """Expose the live compatibility namespace to lifecycle code."""

        return _runtime()

    def _advertises_tools(self) -> bool:
        """Whether the server advertises the ``tools`` capability.

        Per the MCP spec, ``InitializeResult.capabilities.tools`` is non-None
        iff the server implements the ``tools/*`` request family. Prompt-only
        or resource-only servers omit it, and calling ``tools/list`` against
        them raises ``MCPError(-32601 Method not found)`` — which previously
        killed the connection during discovery and made every keepalive fail.
        (Ported from anomalyco/opencode#31271.)

        Returns True when no capability info was captured (legacy fallback:
        preserve the old always-call-list_tools behavior rather than regress
        any server that was working before this gate).
        """
        init_result = self.initialize_result
        caps = (
            getattr(init_result, "capabilities", None)
            if init_result is not None
            else None
        )
        if caps is None:
            return True
        return getattr(caps, "tools", None) is not None

    # ----- Dynamic tool discovery (notifications/tools/list_changed) -----

    async def _refresh_tools_task(self):
        """Run a dynamic tool refresh and log failures from background tasks."""
        runtime = _runtime()
        asyncio = runtime["asyncio"]
        logger = runtime["logger"]

        try:
            await self._refresh_tools()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP server '%s': dynamic tool refresh failed", self.name)

    def _schedule_tools_refresh(self) -> asyncio.Task:
        """Schedule a background tool refresh and keep it strongly referenced."""
        runtime = _runtime()
        asyncio = runtime["asyncio"]

        task = asyncio.create_task(self._refresh_tools_task())
        self._pending_refresh_tasks.add(task)
        task.add_done_callback(self._pending_refresh_tasks.discard)
        return task

    def _make_logging_callback(self):
        """Build a ``logging_callback`` for ``ClientSession``.

        Routes MCP ``notifications/message`` log notifications from the
        server into Hermes' logging (agent.log via hermes_logging), tagged
        with the server name.  Without this, the SDK's default callback
        silently discards them, so server-side warnings/errors during a
        tool call were invisible.  Port of anomalyco/opencode#34529.
        """

        async def _on_log(params):
            runtime = _runtime()
            _MCP_LOG_LEVEL_MAP = runtime["_MCP_LOG_LEVEL_MAP"]
            logging = runtime["logging"]
            json = runtime["json"]
            logger = runtime["logger"]

            try:
                level = _MCP_LOG_LEVEL_MAP.get(
                    str(getattr(params, "level", "info")).lower(),
                    logging.INFO,
                )
                data = getattr(params, "data", None)
                if not isinstance(data, str):
                    try:
                        data = json.dumps(data, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        data = str(data)
                # Cap pathological payloads so a chatty/broken server can't
                # flood agent.log with megabyte lines.
                if len(data) > 2000:
                    data = data[:2000] + "... [truncated]"
                logger_name = getattr(params, "logger", None)
                origin = f"{self.name}/{logger_name}" if logger_name else self.name
                logger.log(level, "MCP server log [%s]: %s", origin, data)
            except Exception:
                logger.debug(
                    "Failed to handle MCP log notification from '%s'",
                    self.name,
                    exc_info=True,
                )

        return _on_log

    def _make_message_handler(self):
        """Build a ``message_handler`` callback for ``ClientSession``.

        Dispatches on notification type.  Only ``ToolListChangedNotification``
        triggers a refresh; prompt and resource change notifications are
        logged as stubs for future work.
        """

        async def _handler(message):
            runtime = _runtime()
            asyncio = runtime["asyncio"]
            logger = runtime["logger"]
            _MCP_NOTIFICATION_TYPES = runtime["_MCP_NOTIFICATION_TYPES"]
            ServerNotification = runtime["ServerNotification"]
            ToolListChangedNotification = runtime["ToolListChangedNotification"]
            PromptListChangedNotification = runtime["PromptListChangedNotification"]
            ResourceListChangedNotification = runtime["ResourceListChangedNotification"]

            try:
                if isinstance(message, Exception):
                    logger.debug(
                        "MCP message handler (%s): exception: %s", self.name, message
                    )
                    return
                if _MCP_NOTIFICATION_TYPES and isinstance(message, ServerNotification):
                    # mcp 2.0 turned ServerNotification from a RootModel into
                    # a plain union of the concrete notification types, so the
                    # payload IS the message instead of living under ``.root``.
                    # ``isinstance`` accepts a union, so the guard above still
                    # holds on both generations; only the unwrap changes.
                    # Without this, ``message.root`` raises AttributeError into
                    # the catch-all below and tools/list_changed refreshes stop
                    # firing silently.
                    match getattr(message, "root", message):
                        case ToolListChangedNotification():
                            logger.info(
                                "MCP server '%s': received tools/list_changed notification",
                                self.name,
                            )
                            # Some servers (notably mongodb-mcp-server) emit
                            # tools/list_changed immediately after initialize,
                            # while the client may already be executing another
                            # request. Refreshing synchronously inside the SDK
                            # notification handler can race with that request
                            # and wedge the stdio JSON-RPC stream, making all
                            # subsequent tool calls time out. Do the refresh in
                            # a separate task and let the handler return
                            # promptly.
                            self._schedule_tools_refresh()
                            # Yield one loop tick so tests and short-lived
                            # notification contexts can observe the scheduled
                            # refresh without awaiting the full server RPC.
                            await asyncio.sleep(0)
                        case PromptListChangedNotification():
                            logger.debug(
                                "MCP server '%s': prompts/list_changed (ignored)",
                                self.name,
                            )
                        case ResourceListChangedNotification():
                            logger.debug(
                                "MCP server '%s': resources/list_changed (ignored)",
                                self.name,
                            )
                        case _:
                            pass
            except Exception:
                logger.exception("Error in MCP message handler for '%s'", self.name)

        return _handler

    _refresh_tools = _mcp_tool_discovery._refresh_tools
    _discover_tools = _mcp_tool_discovery._discover_tools
    _register_discovered_tools_if_needed = (
        _mcp_tool_discovery._register_discovered_tools_if_needed
    )

    async def run(self, config: dict):
        """Long-lived coroutine: connect, discover tools, wait, disconnect.

        Includes automatic reconnection with exponential backoff if the
        connection drops unexpectedly (unless shutdown was requested).
        """
        runtime = _runtime()
        asyncio = runtime["asyncio"]
        logger = runtime["logger"]
        _DEFAULT_TOOL_TIMEOUT = runtime["_DEFAULT_TOOL_TIMEOUT"]
        _get_lifecycle_seconds = runtime["_get_lifecycle_seconds"]
        _ensure_mcp_sdk = runtime["_ensure_mcp_sdk"]
        SamplingHandler = runtime["SamplingHandler"]
        ElicitationHandler = runtime["ElicitationHandler"]
        _validate_remote_mcp_url = runtime["_validate_remote_mcp_url"]
        InvalidMcpUrlError = runtime["InvalidMcpUrlError"]
        _resolve_client_cert = runtime["_resolve_client_cert"]
        NonMcpEndpointError = runtime["NonMcpEndpointError"]
        _MAX_RECONNECT_RETRIES = runtime["_MAX_RECONNECT_RETRIES"]
        _PARKED_RETRY_INTERVAL = runtime["_PARKED_RETRY_INTERVAL"]
        _unwrap_exception_group = runtime["_unwrap_exception_group"]
        _classify_mcp_failure = runtime["_classify_mcp_failure"]
        _is_auth_error = runtime["_is_auth_error"]
        _MAX_INITIAL_CONNECT_RETRIES = runtime["_MAX_INITIAL_CONNECT_RETRIES"]
        _jittered = runtime["_jittered"]
        _MAX_BACKOFF_SECONDS = runtime["_MAX_BACKOFF_SECONDS"]

        self._config = config
        self.tool_timeout = config.get("timeout", _DEFAULT_TOOL_TIMEOUT)
        self._auth_type = (config.get("auth") or "").lower().strip()
        self._idle_timeout_seconds = _get_lifecycle_seconds(
            config, "idle_timeout_seconds"
        )
        self._max_lifetime_seconds = _get_lifecycle_seconds(
            config, "max_lifetime_seconds"
        )

        # Bind the lazily-imported SDK before reading feature flags below
        # (_MCP_SAMPLING_TYPES / _MCP_ELICITATION_TYPES are False until the
        # SDK import actually runs).
        _ensure_mcp_sdk()

        # Set up sampling handler if enabled and SDK types are available
        sampling_config = config.get("sampling", {})
        if sampling_config.get("enabled", True) and runtime["_MCP_SAMPLING_TYPES"]:
            self._sampling = SamplingHandler(self.name, sampling_config)
        else:
            self._sampling = None

        # Set up elicitation handler if enabled and SDK types are available.
        # Servers use elicitation/create to ask the client for structured
        # input mid-tool-call (e.g. payment authorization). The handler
        # routes those requests through Hermes' approval system.
        elicitation_config = config.get("elicitation", {})
        if (
            elicitation_config.get("enabled", True)
            and runtime["_MCP_ELICITATION_TYPES"]
        ):
            self._elicitation = ElicitationHandler(
                self.name, elicitation_config, owner=self
            )
        else:
            self._elicitation = None

        # Validate: warn if both url and command are present
        if "url" in config and "command" in config:
            logger.warning(
                "MCP server '%s' has both 'url' and 'command' in config. "
                "Using HTTP transport ('url'). Remove 'command' to silence "
                "this warning.",
                self.name,
            )

        # Validate remote URL once, up front.  Raising here (rather than
        # letting it blow up inside the SDK's httpx layer on every retry)
        # means a typo in config.yaml fails fast with a clear error — and
        # critically, no reconnect-backoff burn.  (Ported from
        # anomalyco/opencode#25019.)
        if self._is_http():
            try:
                _validate_remote_mcp_url(self.name, config.get("url"))
            except InvalidMcpUrlError as exc:
                logger.warning("%s", exc)
                self._error = exc
                self._ready.set()
                return

            # Pre-flight content-type probe (Streamable HTTP only; SSE is
            # exercised by its own client and legitimately serves
            # text/event-stream). A URL pointed at a web-app root returns
            # HTML, which makes the SDK hang for the full connect_timeout
            # before surfacing an opaque CancelledError. Probing here — once,
            # outside the SDK task group — fails fast and non-retryably with
            # an actionable message, mirroring the URL-validation path above.
            # Skip the probe when _ready is already set (reconnect after a
            # prior successful connect) — the endpoint was validated once,
            # re-probing is a redundant round-trip. Also skip for OAuth servers:
            # without a cached token the endpoint returns HTML or 401, which
            # would incorrectly block the OAuth flow before it can run.
            if (
                config.get("transport") != "sse"
                and not config.get("skip_preflight")
                and not self._ready.is_set()
                and self._auth_type != "oauth"
            ):
                try:
                    _probe_headers = dict(config.get("headers") or {})
                    await self._preflight_content_type(
                        config["url"],
                        headers=_probe_headers,
                        ssl_verify=config.get("ssl_verify", True),
                        client_cert=_resolve_client_cert(self.name, config),
                    )
                except NonMcpEndpointError as exc:
                    logger.warning("%s", exc)
                    self._error = exc
                    self._ready.set()
                    return

        self._reconnect_retries = 0
        initial_retries = 0
        backoff = 1.0

        while True:
            try:
                if self._is_http():
                    lifecycle_reason = await self._run_http(config)
                else:
                    lifecycle_reason = await self._run_stdio(config)
                # Transport returned cleanly. Two cases:
                #  - _shutdown_event was set: exit the run loop entirely.
                #  - _reconnect_event was set (auth recovery): loop back and
                #    rebuild the MCP session with fresh credentials. Do NOT
                #    touch the retry counters — this is not a failure.
                if self._shutdown_event.is_set():
                    break
                if lifecycle_reason == "recycle":
                    logger.info(
                        "MCP server '%s': stdio session recycled after %s; "
                        "waiting for lazy reconnect",
                        self.name,
                        self._recycled_reason,
                    )
                    self.session = None
                    await self._wait_for_lazy_reconnect()
                    if self._shutdown_event.is_set():
                        break
                    self._reconnect_event.clear()
                    continue
                # Per-cycle reconnect chatter — DEBUG. In the flapping case
                # this fires on every rebuild; the WARNINGs live on the
                # state transitions.
                logger.debug(
                    "MCP server '%s': reconnecting (OAuth recovery or manual refresh)",
                    self.name,
                )
                # A clean transport return means a session was established and
                # then asked to rebuild (auth recovery / manual refresh /
                # keepalive failure / transport TaskGroup drop). That alone is
                # NOT proof of health: a flapping transport handshakes fine and
                # drops moments later, and resetting the budget here let such
                # servers respawn forever (#62212 — 6212 spawns in 63h).
                # Only clear the consecutive-failure budget once the session
                # PROVED healthy — survived >=1 full keepalive interval or
                # served >=1 successful tool call (_mark_session_proven).
                if self._session_proven:
                    self._reconnect_retries = 0
                    backoff = 1.0
                else:
                    # Unproven session: charge the rapid-drop budget so a
                    # flapping transport still reaches the park.
                    self._reconnect_retries += 1
                    if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s': %d consecutive reconnects "
                            "without a healthy session (rapid-drop budget "
                            "exhausted), parking; will self-probe every %ds "
                            "until it recovers (state: degraded → parked)",
                            self.name,
                            _MAX_RECONNECT_RETRIES,
                            _PARKED_RETRY_INTERVAL,
                        )
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_PARKED_RETRY_INTERVAL
                        )
                        if parked == "shutdown":
                            break
                        logger.debug(
                            "MCP server '%s': attempting revival from parked "
                            "state (self-probe or explicit reconnect request); "
                            "rebuilding transport.",
                            self.name,
                        )
                        # One probe attempt per wake — see the exception-path
                        # park below.
                        self._reconnect_retries = _MAX_RECONNECT_RETRIES
                        backoff = 1.0
                # Reset the session reference and readiness; _run_http/_run_stdio
                # will repopulate both on successful re-entry.  Leaving
                # _ready set here lets handler-side recovery mistake the stale
                # pre-reconnect session for a fresh one and retry too early.
                self._ready.clear()
                self.session = None
                continue
            except asyncio.CancelledError:
                # Task was cancelled (shutdown, gateway restart, explicit
                # task.cancel()). Don't treat this as a connection failure —
                # CancelledError inherits from BaseException (not Exception)
                # in Python 3.11+, so the broad ``except Exception`` below
                # would NOT catch it; we'd silently exit the reconnect loop
                # and the MCP server would stay dead until Hermes is fully
                # restarted. Re-raise so the task's cancellation propagates
                # correctly to asyncio's task machinery and ``shutdown()``'s
                # ``await self._task`` completes. See #9930.
                self.session = None
                raise
            except Exception as exc:
                self.session = None
                # Unwrap anyio TaskGroup wrappers first: str(exc) on a
                # BaseExceptionGroup is "unhandled errors in a TaskGroup
                # (N sub-exceptions)" — useless in logs, and it hides the
                # root cause from the auth/permanence classification below.
                # Empty dead-pipe errors still get a name this way
                # (e.g. "BrokenPipeError: ").
                root = _unwrap_exception_group(exc)
                failure_class = _classify_mcp_failure(root)
                if self._is_recycled_stdio():
                    logger.warning(
                        "MCP server '%s': lazy reconnect after stdio recycle "
                        "failed, marking unavailable while retrying: %s: %s",
                        self.name,
                        type(root).__name__,
                        root,
                    )
                    self._recycled_reason = None

                # If this is the first connection attempt, retry with backoff
                # before giving up. A transient DNS/network blip at startup
                # should not permanently kill the server.
                # (Ported from Kilo Code's MCP resilience fix.)
                if not self._ready.is_set():
                    if failure_class == "permanent":
                        # Deterministic failure (bad command, non-MCP URL,
                        # 401/403): every retry hits the same wall. Park
                        # immediately instead of burning the retry ladder
                        # and spamming N identical warnings (#65673).
                        #
                        # Auth failures park here too rather than returning.
                        # Returning ends the run task, and with it the only
                        # listener on ``_reconnect_event`` — so a 401 on the
                        # very first connect left the server unrevivable for
                        # the life of the process, even after the user
                        # re-authenticated with ``hermes mcp login``. Parking
                        # keeps the task alive so the 300s self-probe (and an
                        # explicit /mcp refresh) can pick up fresh tokens.
                        if _is_auth_error(root):
                            logger.warning(
                                "MCP server '%s' failed initial authentication, "
                                "parking until credentials change; re-authenticate "
                                "through the configured MCP integration for %s "
                                "(state: connecting → parked): %s: %s",
                                self.name,
                                self.name,
                                type(root).__name__,
                                root,
                            )
                        else:
                            logger.warning(
                                "MCP server '%s' failed initial connection with a "
                                "permanent error, parking without retries "
                                "(state: connecting → parked): %s: %s",
                                self.name,
                                type(root).__name__,
                                root,
                            )
                        self._error = exc
                        self._ready.set()
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_PARKED_RETRY_INTERVAL
                        )
                        if parked == "shutdown":
                            return
                        logger.debug(
                            "MCP server '%s': attempting revival after "
                            "permanent initial failure (self-probe or explicit "
                            "reconnect request); rebuilding transport.",
                            self.name,
                        )
                        initial_retries = 0
                        self._reconnect_retries = 0
                        backoff = 1.0
                        self._error = None
                        self._ready.clear()
                        continue

                    initial_retries += 1
                    if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s' failed initial connection after "
                            "%d attempts, parking until a reconnect is "
                            "requested (state: connecting → parked): %s: %s",
                            self.name,
                            _MAX_INITIAL_CONNECT_RETRIES,
                            type(root).__name__,
                            root,
                        )
                        self._error = exc
                        self._ready.set()
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_PARKED_RETRY_INTERVAL
                        )
                        if parked == "shutdown":
                            return
                        logger.debug(
                            "MCP server '%s': attempting revival after initial "
                            "connection failures (self-probe or explicit "
                            "reconnect request); rebuilding transport.",
                            self.name,
                        )
                        initial_retries = 0
                        self._reconnect_retries = 0
                        backoff = 1.0
                        self._error = None
                        self._ready.clear()
                        continue

                    logger.debug(
                        "MCP server '%s' initial connection failed "
                        "(attempt %d/%d), retrying in %.0fs: %s: %s",
                        self.name,
                        initial_retries,
                        _MAX_INITIAL_CONNECT_RETRIES,
                        backoff,
                        type(root).__name__,
                        root,
                    )
                    await asyncio.sleep(_jittered(backoff))
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                    # Check if shutdown was requested during the sleep
                    if self._shutdown_event.is_set():
                        self._error = exc
                        self._ready.set()
                        return
                    continue

                # If shutdown was requested, don't reconnect
                if self._shutdown_event.is_set():
                    logger.debug(
                        "MCP server '%s' disconnected during shutdown: %s: %s",
                        self.name,
                        type(root).__name__,
                        root,
                    )
                    return

                if failure_class == "permanent":
                    # A previously-working server now fails deterministically
                    # (revoked credentials, URL now serving a web page, stdio
                    # binary uninstalled). Retrying can't help — park
                    # immediately without burning the retry ladder.
                    logger.warning(
                        "MCP server '%s' hit a permanent error, parking "
                        "without retries; will self-probe every %ds "
                        "(state: connected → parked): %s: %s",
                        self.name,
                        _PARKED_RETRY_INTERVAL,
                        type(root).__name__,
                        root,
                    )
                    self._was_parked = True
                    self._deregister_tools()
                    self._reconnect_event.clear()
                    parked = await self._wait_for_reconnect_or_shutdown(
                        timeout=_PARKED_RETRY_INTERVAL
                    )
                    if parked == "shutdown":
                        return
                    logger.debug(
                        "MCP server '%s': attempting revival from parked state "
                        "(permanent error; self-probe or explicit reconnect "
                        "request); rebuilding transport.",
                        self.name,
                    )
                    self._reconnect_retries = _MAX_RECONNECT_RETRIES
                    backoff = 1.0
                    continue

                self._reconnect_retries += 1
                if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                    logger.warning(
                        "MCP server '%s' failed after %d reconnection attempts, "
                        "parking; will self-probe every %ds until it recovers "
                        "(state: degraded → parked): %s: %s",
                        self.name,
                        _MAX_RECONNECT_RETRIES,
                        _PARKED_RETRY_INTERVAL,
                        type(root).__name__,
                        root,
                    )
                    # Do NOT return — exiting the task orphans the server:
                    # nothing would ever listen for _reconnect_event again
                    # and the server would be permanently wedged for the
                    # life of the process (#16788). Instead, drop the phantom
                    # tools from the registry and park. Because parking
                    # deregisters the tools, no tool call can reach the
                    # circuit-breaker half-open probe or _signal_reconnect —
                    # so the park is a TIMED wait: every _PARKED_RETRY_INTERVAL
                    # we wake and attempt one reconnect ourselves (#57129).
                    # An explicit _reconnect_event.set() (OAuth recovery,
                    # manual /mcp refresh) still wakes us immediately.
                    self._was_parked = True
                    self._deregister_tools()
                    self._reconnect_event.clear()
                    parked = await self._wait_for_reconnect_or_shutdown(
                        timeout=_PARKED_RETRY_INTERVAL
                    )
                    if parked == "shutdown":
                        return
                    logger.debug(
                        "MCP server '%s': attempting revival from parked state "
                        "(self-probe or explicit reconnect request); "
                        "rebuilding transport.",
                        self.name,
                    )
                    # One probe attempt per wake: budget of 1 so a still-dead
                    # server parks again for another interval instead of
                    # burning 5 rapid retries each cycle.
                    self._reconnect_retries = _MAX_RECONNECT_RETRIES
                    backoff = 1.0
                    continue

                # Per-attempt retry chatter stays at DEBUG; state transitions
                # (connected->degraded, degraded->parked, parked->revived)
                # carry the WARNINGs — one line per transition, not per try.
                logger.debug(
                    "MCP server '%s' connection lost (attempt %d/%d), "
                    "reconnecting in %.0fs: %s: %s",
                    self.name,
                    self._reconnect_retries,
                    _MAX_RECONNECT_RETRIES,
                    backoff,
                    type(root).__name__,
                    root,
                )
                await asyncio.sleep(_jittered(backoff))
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                # Check again after sleeping
                if self._shutdown_event.is_set():
                    return
            finally:
                self.session = None
