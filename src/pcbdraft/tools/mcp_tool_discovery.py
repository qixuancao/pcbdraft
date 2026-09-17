# Registration keeps broad best-effort schema-cache handling from the original.
# ruff: noqa: BLE001
"""MCP tool discovery and schema registration dispatch.

``mcp_tool`` remains the compatibility surface and injects its live namespace
so established monkeypatch paths remain effective. This module never imports
``mcp_tool`` and owns no transport, configuration, auth/recovery, or tool-call
execution behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

_UTILITY_CAPABILITY_METHODS = {
    "list_resources": "list_resources",
    "read_resource": "read_resource",
    "list_prompts": "list_prompts",
    "get_prompt": "get_prompt",
}
_UTILITY_CAPABILITY_ATTRS = {
    "list_resources": "resources",
    "read_resource": "resources",
    "list_prompts": "prompts",
    "get_prompt": "prompts",
}

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_tool_discovery_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP tool discovery runtime is not configured")
    return _runtime_namespace()


async def _refresh_tools(self):
    """Re-fetch tools from the server and update the registry.

    Called when the server sends ``notifications/tools/list_changed``.
    The lock prevents overlapping refreshes from rapid-fire notifications.
    After the initial ``await`` (list_tools), all mutations are synchronous
    — atomic from the event loop's perspective.
    """
    from pcbdraft.tools.registry import registry

    if not self._advertises_tools():
        # A server that doesn't implement tools/* should never send
        # tools/list_changed, but guard anyway — calling tools/list
        # would raise MCPError(-32601).
        return

    async with self._refresh_lock:
        # Capture old tool names for change diff
        old_tool_names = set(self._registered_tool_names)

        # 1. Fetch current tool list from server (follow nextCursor)
        async with self._rpc_lock:
            new_mcp_tools = await _runtime()["_paginate_full_list"](
                self.session.list_tools, "tools", self.name
            )

        # 2. Re-register with fresh tool list. Avoid nuke-and-repave for
        # all names: live agent turns may already have tool-call IDs
        # pointing at existing handler functions. Replacing entries
        # in-place is enough for unchanged names and avoids transient
        # "tool not connected" / stale-handler races during startup
        # notifications. Tools absent from the fresh list are no longer
        # callable, so remove only those stale registry entries first.
        toolset_name = f"mcp-{self.name}"
        stale_tool_names = old_tool_names - {
            _runtime()["mcp_prefixed_tool_name"](self.name, tool.name)
            for tool in new_mcp_tools
        }
        for tool_name in stale_tool_names:
            # Never let one server's refresh remove a colliding name that
            # is currently owned by another server.
            if registry.get_toolset_for_tool(tool_name) != toolset_name:
                continue
            registry.deregister(tool_name)
            _runtime()["_forget_mcp_tool_server"](tool_name)

        # 3. Re-register with the fresh list. The helper may skip names that
        # are ambiguous after normalization.
        self._tools = new_mcp_tools
        registered_names = _runtime()["_register_server_tools"](
            self.name, self, self._config
        )

        # A previously unique raw name can become ambiguous without changing
        # its normalized registry name. In that case the pre-pass above does
        # not consider it stale, so remove any old entry that the final,
        # collision-checked registration set no longer owns.
        registered_name_set = set(registered_names)
        for tool_name in old_tool_names - registered_name_set:
            if registry.get_toolset_for_tool(tool_name) != toolset_name:
                continue
            registry.deregister(tool_name)
            _runtime()["_forget_mcp_tool_server"](tool_name)
        self._registered_tool_names = registered_names

        # 4. Log what changed (user-visible notification)
        new_tool_names = set(self._registered_tool_names)
        added = new_tool_names - old_tool_names
        removed = old_tool_names - new_tool_names
        changes = []
        if added:
            changes.append(f"added: {', '.join(sorted(added))}")
        if removed:
            changes.append(f"removed: {', '.join(sorted(removed))}")
        if changes:
            _runtime()["logger"].warning(
                "MCP server '%s': tools changed dynamically — %s. "
                "Verify these changes are expected.",
                self.name,
                "; ".join(changes),
            )
        else:
            _runtime()["logger"].info(
                "MCP server '%s': dynamically refreshed %d tool(s) (no changes)",
                self.name,
                len(self._registered_tool_names),
            )


async def _discover_tools(self):
    """Discover tools from the connected session.

    Capability-gated: prompt-only / resource-only MCP servers don't
    implement ``tools/list``, and calling it raises ``MCPError(-32601)``,
    which previously aborted the connection — those servers could never
    stay connected for their prompts/resources. Skip the call when the
    server doesn't advertise the ``tools`` capability.
    (Ported from anomalyco/opencode#31271.)
    """
    # Fresh transport connection → re-probe with the cheap ``ping`` path.
    # Clears any latch from a prior connection in case the server gained
    # ping support across the reconnect.
    self._ping_unsupported = False
    if self.session is None:
        return
    if not self._advertises_tools():
        _runtime()["logger"].info(
            "MCP server '%s': does not advertise 'tools' capability — "
            "skipping tools/list (prompts/resources remain available)",
            self.name,
        )
        self._tools = []
        self._register_discovered_tools_if_needed()
        return
    async with self._rpc_lock:
        self._list_cache_meta = {}
        self._tools = await _runtime()["_paginate_full_list"](
            self.session.list_tools,
            "tools",
            self.name,
            cache_meta_out=self._list_cache_meta,
        )
    self._register_discovered_tools_if_needed()


def _register_discovered_tools_if_needed(self) -> None:
    """Re-register tools after an owned server reconnects if needed.

    Initial registration is performed by ``_discover_and_register_server``
    after ``start()`` completes. During a later reconnect, outage handling
    may clear ``_ready`` before discovery and may deregister stale tools.
    A managed server can still be identified by its entry in ``_servers``;
    publish its freshly discovered tools before transport readiness is
    restored so a successful revival cannot come back with zero tools.
    A server retained after a recoverable initial failure is likewise
    registry-owned before its first successful session, so ownership also
    authorizes its first publication.
    """
    if self._registered_tool_names:
        return
    if not self._ready.is_set():
        with _runtime()["_lock"]:
            if _runtime()["_servers"].get(self.name) is not self:
                return
    self._registered_tool_names = _runtime()["_register_server_tools"](
        self.name, self, self._config
    )
    # A retained initial-failure server that just published tools has
    # recovered: drop its stale connect error so status surfaces stop
    # reporting it as failed.
    with _runtime()["_lock"]:
        if _runtime()["_servers"].get(self.name) is self:
            _runtime()["_server_connect_errors"].pop(self.name, None)


def _select_utility_schemas(server_name: str, server: Any, config: dict) -> list[dict]:
    """Select utility schemas based on config and server capabilities."""
    tools_filter = config.get("tools") or {}
    resources_enabled = _runtime()["_parse_boolish"](
        tools_filter.get("resources"), default=True
    )
    prompts_enabled = _runtime()["_parse_boolish"](
        tools_filter.get("prompts"), default=True
    )

    # ``initialize_result.capabilities`` is the source of truth: its sub-objects
    # (``resources``, ``prompts``) are non-None iff the server advertises that
    # request family. ``hasattr(server.session, ...)`` was the old gate but
    # ClientSession always has the four method attributes defined on the class,
    # so it never filtered anything.
    advertised_caps = None
    init_result = getattr(server, "initialize_result", None)
    if init_result is not None:
        advertised_caps = getattr(init_result, "capabilities", None)

    selected: list[dict] = []
    for entry in _runtime()["_build_utility_schemas"](server_name):
        handler_key = entry["handler_key"]
        if handler_key in {"list_resources", "read_resource"} and not resources_enabled:
            _runtime()["logger"].debug(
                "MCP server '%s': skipping utility '%s' (resources disabled)",
                server_name,
                handler_key,
            )
            continue
        if handler_key in {"list_prompts", "get_prompt"} and not prompts_enabled:
            _runtime()["logger"].debug(
                "MCP server '%s': skipping utility '%s' (prompts disabled)",
                server_name,
                handler_key,
            )
            continue

        # Preferred gate: check the server's advertised capabilities. Skip
        # if the capability is explicitly not advertised.
        if advertised_caps is not None:
            cap_attr = _runtime()["_UTILITY_CAPABILITY_ATTRS"][handler_key]
            if getattr(advertised_caps, cap_attr, None) is None:
                _runtime()["logger"].debug(
                    "MCP server '%s': skipping utility '%s' "
                    "(server does not advertise '%s' capability)",
                    server_name,
                    handler_key,
                    cap_attr,
                )
                continue
        else:
            # Legacy fallback for test fixtures or older code paths where
            # initialize_result wasn't captured. Preserves the old behavior
            # of registering every stub in that case rather than regressing
            # any server that was working before this fix.
            required_method = _runtime()["_UTILITY_CAPABILITY_METHODS"][handler_key]
            if not hasattr(server.session, required_method):
                _runtime()["logger"].debug(
                    "MCP server '%s': skipping utility '%s' (session lacks %s)",
                    server_name,
                    handler_key,
                    required_method,
                )
                continue
        selected.append(entry)
    return selected


def _register_server_tools(name: str, server: Any, config: dict) -> list[str]:
    """Register tools from an already-connected server into the registry.

    Handles include/exclude filtering and utility tools. Toolset resolution
    for ``mcp-{server}`` and raw server-name aliases is derived from the live
    registry, rather than mutating ``toolsets.TOOLSETS`` at runtime.

    Lossy provider-safe name normalization can map distinct raw names to the
    same registry name (for example ``read-file`` and ``read_file``). Such
    collisions fail closed: every ambiguous entry is skipped rather than
    selecting an arbitrary handler.

    Used by both initial discovery and dynamic refresh (list_changed).

    Returns:
        List of registered prefixed tool names.
    """
    from pcbdraft.tools.registry import registry

    registered_names: list[str] = []
    toolset_name = f"mcp-{name}"

    # Selective tool loading: honour include/exclude lists from config.
    # Rules (matching issue #690 spec, extended with glob support):
    #   tools.include — whitelist: only matching tool names are registered
    #   tools.exclude — blacklist: all tools EXCEPT matching ones are registered
    #   entries may be exact names or fnmatch globs (e.g. "*_radar_*")
    #   include takes precedence over exclude
    #   Neither set → register all tools (backward-compatible default)
    tools_filter = config.get("tools") or {}
    include_set = _runtime()["_normalize_name_filter"](
        tools_filter.get("include"), f"mcp_servers.{name}.tools.include"
    )
    exclude_set = _runtime()["_normalize_name_filter"](
        tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude"
    )

    def _should_register(tool_name: str) -> bool:
        if include_set:
            return _runtime()["matches_name_filter"](tool_name, include_set)
        if exclude_set:
            return not _runtime()["matches_name_filter"](tool_name, exclude_set)
        return True

    check_fn = _runtime()["_make_check_fn"](name)
    candidates: list[dict] = []

    # Trust-tier metadata (security boundary): capture the server's
    # configured trust tier and each tool's readOnlyHint annotation NOW,
    # at discovery, so the call-time gate in _make_tool_handler classifies
    # from data we control rather than re-reading server-supplied state.
    _runtime()["_record_tool_trust_metadata"](name, config, server._tools)

    for mcp_tool in server._tools:
        if not _should_register(mcp_tool.name):
            _runtime()["logger"].debug(
                "MCP server '%s': skipping tool '%s' (filtered by config)",
                name,
                mcp_tool.name,
            )
            continue

        _runtime()["_scan_mcp_description"](
            name, mcp_tool.name, mcp_tool.description or ""
        )
        schema = _runtime()["_convert_mcp_schema"](name, mcp_tool)
        candidates.append(
            {
                "registry_name": schema["name"],
                "origin": f"tool {mcp_tool.name!r}",
                "schema": schema,
                "handler": _runtime()["_make_tool_handler"](
                    name, mcp_tool.name, server.tool_timeout
                ),
                "check_fn": check_fn,
            }
        )

    # Generated resource/prompt utility tools share the same namespace as raw
    # MCP tools, so they must participate in the same collision preflight.
    handler_factories = {
        "list_resources": _runtime()["_make_list_resources_handler"],
        "read_resource": _runtime()["_make_read_resource_handler"],
        "list_prompts": _runtime()["_make_list_prompts_handler"],
        "get_prompt": _runtime()["_make_get_prompt_handler"],
    }
    for entry in _runtime()["_select_utility_schemas"](name, server, config):
        schema = entry["schema"]
        handler_key = entry["handler_key"]
        candidates.append(
            {
                "registry_name": schema["name"],
                "origin": f"generated utility {handler_key!r}",
                "schema": schema,
                "handler": handler_factories[handler_key](name, server.tool_timeout),
                "check_fn": check_fn,
            }
        )

    # Exact duplicate rows from a server are harmless but should not inflate
    # counts. Distinct origins that collapse to one normalized name are unsafe.
    unique_candidates: list[dict] = []
    seen_candidates: set[tuple[str, str]] = set()
    origins_by_name: dict[str, set[str]] = {}
    for candidate in candidates:
        key = (candidate["registry_name"], candidate["origin"])
        if key in seen_candidates:
            _runtime()["logger"].debug(
                "MCP server '%s': duplicate registration candidate %s for '%s'; "
                "keeping one",
                name,
                candidate["origin"],
                candidate["registry_name"],
            )
            continue
        seen_candidates.add(key)
        unique_candidates.append(candidate)
        origins_by_name.setdefault(candidate["registry_name"], set()).add(
            candidate["origin"]
        )

    # A generated resource/prompt utility that normalizes onto a server-native
    # tool's name must not knock that native tool out of the registry: the
    # native tool is the capability the user connected the server for, while the
    # generated utility (read_resource/list_resources/list_prompts/get_prompt)
    # is optional sugar that only matters when the server exposes no such tool
    # of its own (#87112). Resolve that specific collision in favour of the
    # native tool — keep it, drop the shadowed utility — and fall back to the
    # conservative skip-everything only for genuinely ambiguous collisions (two
    # or more native tools normalizing to one name, which we cannot
    # disambiguate). The four utility keys are distinct, so a colliding set
    # holds at most one utility origin.
    ambiguous_names: dict[str, list[str]] = {}
    shadowed_utilities: set[tuple[str, str]] = set()
    for registry_name, origins in origins_by_name.items():
        if len(origins) <= 1:
            continue
        utility_origins = sorted(
            o for o in origins if o.startswith("generated utility ")
        )
        native_origins = sorted(origins - set(utility_origins))
        if len(native_origins) == 1 and utility_origins:
            for util_origin in utility_origins:
                shadowed_utilities.add((registry_name, util_origin))
            _runtime()["logger"].info(
                "MCP server '%s': generated utility %s normalizes onto "
                "server-native %s — keeping the native tool and dropping the "
                "utility (the utility only applies when the server has no such "
                "tool of its own)",
                name,
                ", ".join(utility_origins),
                native_origins[0],
            )
            continue
        ambiguous_names[registry_name] = sorted(origins)

    for registry_name, origins in sorted(ambiguous_names.items()):
        _runtime()["logger"].error(
            "MCP server '%s': name normalization collision for '%s' from %s; "
            "skipping every colliding entry instead of choosing an arbitrary "
            "handler",
            name,
            registry_name,
            ", ".join(origins),
        )

    for candidate in unique_candidates:
        registry_name = candidate["registry_name"]
        if registry_name in ambiguous_names:
            continue
        if (registry_name, candidate["origin"]) in shadowed_utilities:
            continue

        existing_toolset = registry.get_toolset_for_tool(registry_name)
        if existing_toolset and existing_toolset != toolset_name:
            if existing_toolset.startswith("mcp-"):
                _runtime()["logger"].error(
                    "MCP server '%s': %s normalizes to '%s', already owned by "
                    "MCP toolset '%s' — skipping to preserve the existing owner",
                    name,
                    candidate["origin"],
                    registry_name,
                    existing_toolset,
                )
            else:
                _runtime()["logger"].warning(
                    "MCP server '%s': %s (→ '%s') collides with built-in tool "
                    "in toolset '%s' — skipping to preserve built-in",
                    name,
                    candidate["origin"],
                    registry_name,
                    existing_toolset,
                )
            continue

        registry.register(
            name=registry_name,
            toolset=toolset_name,
            schema=candidate["schema"],
            handler=candidate["handler"],
            check_fn=candidate["check_fn"],
            is_async=False,
            description=candidate["schema"]["description"],
        )

        # The pre-check above is advisory only. Multiple servers connect in
        # parallel, so ToolRegistry.register() is the atomic ownership gate.
        if registry.get_toolset_for_tool(registry_name) != toolset_name:
            _runtime()["logger"].error(
                "MCP server '%s': registration of %s as '%s' was rejected by "
                "the registry; skipping provenance/count updates",
                name,
                candidate["origin"],
                registry_name,
            )
            continue

        _runtime()["_track_mcp_tool_server"](registry_name, name)
        registered_names.append(registry_name)

    if registered_names:
        registry.register_toolset_alias(name, toolset_name)
        # Write-through (#56832): refresh the on-disk schema cache after a
        # live connect so the next startup can lazily register this server
        # without spawning it. Cache failures never break registration.
        try:
            from pcbdraft.tools.mcp_schema_cache import (
                config_fingerprint,
                write_cache_entry,
            )

            tools_payload: list[dict] = []
            for mcp_tool in server._tools:
                if not _should_register(mcp_tool.name):
                    continue
                schema_obj = getattr(mcp_tool, "inputSchema", None)
                tools_payload.append(
                    {
                        "name": mcp_tool.name,
                        "description": mcp_tool.description or "",
                        "inputSchema": schema_obj
                        if isinstance(schema_obj, dict)
                        else {},
                        # Persist the trust-relevant annotation so the lazy
                        # (cache-registered) path gates identically on next
                        # startup without spawning the server.
                        "annotations": {
                            "readOnlyHint": _runtime()["_annotation_read_only_hint"](
                                mcp_tool
                            ),
                        },
                    }
                )
            utility_payload = [
                {"schema": entry["schema"], "handler_key": entry["handler_key"]}
                for entry in _runtime()["_select_utility_schemas"](name, server, config)
            ]
            write_cache_entry(
                name,
                config_fingerprint(config),
                tools=tools_payload,
                utility_tools=utility_payload,
                ttl_ms=(getattr(server, "_list_cache_meta", None) or {}).get("ttl_ms"),
                cache_scope=(getattr(server, "_list_cache_meta", None) or {}).get(
                    "cache_scope"
                ),
            )
        except Exception as exc:
            _runtime()["logger"].debug(
                "MCP schema cache write failed for '%s': %s", name, exc
            )

    return registered_names


class _CachedMCPTool:
    """Minimal stand-in for MCP Tool objects loaded from the schema cache."""

    __slots__ = ("description", "inputSchema", "name")

    def __init__(self, name: str, description: str, inputSchema: dict):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema or {}


def _register_from_cache_sync(name: str, config: dict, entry: dict) -> list[str]:
    """Register a server's tools from a cached manifest, no child process.

    Lazy startup (#56832, design by Vansh5632): tools appear in the registry
    immediately; the first real call routes through
    ``_get_connected_server_for_call`` → ``_ensure_lazy_server_connected``.
    """
    from pcbdraft.tools.mcp_schema_cache import (
        config_fingerprint,
        tools_from_cache_entry,
        utility_tools_from_cache_entry,
    )
    from pcbdraft.tools.registry import registry

    registered_names: list[str] = []
    toolset_name = f"mcp-{name}"
    fingerprint = config_fingerprint(config)
    tool_timeout = config.get("timeout", _runtime()["_DEFAULT_TOOL_TIMEOUT"])
    tools_filter = config.get("tools") or {}
    include_set = _runtime()["_normalize_name_filter"](
        tools_filter.get("include"), f"mcp_servers.{name}.tools.include"
    )
    exclude_set = _runtime()["_normalize_name_filter"](
        tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude"
    )

    def _should_register(tool_name: str) -> bool:
        if include_set:
            return _runtime()["matches_name_filter"](tool_name, include_set)
        if exclude_set:
            return not _runtime()["matches_name_filter"](tool_name, exclude_set)
        return True

    check_fn = _runtime()["_make_check_fn"](name)
    # Trust-tier metadata for the lazy path: the cached manifest carries
    # each tool's readOnlyHint (written by the live discovery path), and
    # trust comes from operator config. Recording it before registration
    # keeps the call-time gate identical whether the server was spawned
    # live or registered from cache. Missing "annotations" in older cache
    # files fails closed to write-capable.
    cached_tool_objs = [
        SimpleNamespace(
            name=raw.get("name"),
            annotations=raw.get("annotations")
            if isinstance(raw.get("annotations"), dict)
            else None,
        )
        for raw in tools_from_cache_entry(entry)
        if isinstance(raw, dict) and raw.get("name")
    ]
    _runtime()["_record_tool_trust_metadata"](name, config, cached_tool_objs)
    for raw in tools_from_cache_entry(entry):
        if not isinstance(raw, dict):
            continue
        raw_name = raw.get("name")
        if not raw_name or not _should_register(raw_name):
            continue
        raw_schema = raw.get("inputSchema")
        mcp_tool = _runtime()["_CachedMCPTool"](
            raw_name,
            raw.get("description") or "",
            raw_schema if isinstance(raw_schema, dict) else {},
        )
        # Defense-in-depth: the cache file is user-writable JSON, so run the
        # same injection scan the eager discovery path applies.
        _runtime()["_scan_mcp_description"](
            name, mcp_tool.name, mcp_tool.description or ""
        )
        schema = _runtime()["_convert_mcp_schema"](name, mcp_tool)
        registry_name = schema["name"]
        existing_toolset = registry.get_toolset_for_tool(registry_name)
        if existing_toolset and existing_toolset != toolset_name:
            _runtime()["logger"].warning(
                "MCP server '%s' (lazy): cached tool '%s' collides with "
                "toolset '%s' — skipping",
                name,
                registry_name,
                existing_toolset,
            )
            continue
        registry.register(
            name=registry_name,
            toolset=toolset_name,
            schema=schema,
            handler=_runtime()["_make_tool_handler"](name, raw_name, tool_timeout),
            check_fn=check_fn,
            is_async=False,
            description=schema["description"],
        )
        if registry.get_toolset_for_tool(registry_name) != toolset_name:
            continue
        _runtime()["_track_mcp_tool_server"](registry_name, name)
        registered_names.append(registry_name)

    handler_factories = {
        "list_resources": _runtime()["_make_list_resources_handler"],
        "read_resource": _runtime()["_make_read_resource_handler"],
        "list_prompts": _runtime()["_make_list_prompts_handler"],
        "get_prompt": _runtime()["_make_get_prompt_handler"],
    }
    for raw in utility_tools_from_cache_entry(entry):
        if not isinstance(raw, dict):
            continue
        schema = raw.get("schema")
        handler_key = raw.get("handler_key")
        if not isinstance(schema, dict) or handler_key not in handler_factories:
            continue
        util_name = schema.get("name") or ""
        if not util_name:
            continue
        existing_toolset = registry.get_toolset_for_tool(util_name)
        if existing_toolset and existing_toolset != toolset_name:
            continue
        registry.register(
            name=util_name,
            toolset=toolset_name,
            schema=schema,
            handler=handler_factories[handler_key](name, tool_timeout),
            check_fn=check_fn,
            is_async=False,
            description=schema.get("description") or "",
        )
        if registry.get_toolset_for_tool(util_name) != toolset_name:
            continue
        _runtime()["_track_mcp_tool_server"](util_name, name)
        registered_names.append(util_name)

    if registered_names:
        registry.register_toolset_alias(name, toolset_name)
        with _runtime()["_lock"]:
            _runtime()["_lazy_server_configs"][name] = dict(config)
            _runtime()["_lazy_server_fingerprints"][name] = fingerprint
            _runtime()["_lazy_server_tool_names"][name] = list(registered_names)
        _runtime()["logger"].info(
            "MCP server '%s' (lazy): registered %d tool(s) from schema cache",
            name,
            len(registered_names),
        )
    return registered_names
