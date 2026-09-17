from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pcbdraft.tools import mcp_tool, mcp_tool_call, mcp_tool_discovery


class _Registry:
    def __init__(self):
        self.owners: dict[str, str] = {}
        self.registered: list[dict] = []
        self.deregistered: list[str] = []
        self.aliases: list[tuple[str, str]] = []

    def get_toolset_for_tool(self, name):
        return self.owners.get(name)

    def register(self, **kwargs):
        self.registered.append(kwargs)
        self.owners[kwargs["name"]] = kwargs["toolset"]

    def deregister(self, name):
        self.deregistered.append(name)
        self.owners.pop(name, None)

    def register_toolset_alias(self, alias, toolset):
        self.aliases.append((alias, toolset))


class MCPToolDiscoveryCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_identities_remain(self):
        source = Path(mcp_tool_discovery.__file__).read_text(encoding="utf-8")
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        )

        self.assertNotIn("pcbdraft.tools.mcp_tool", imports)
        self.assertIs(
            mcp_tool.MCPServerTask._discover_tools,
            mcp_tool_discovery._discover_tools,
        )
        self.assertIs(
            mcp_tool.MCPServerTask._refresh_tools,
            mcp_tool_discovery._refresh_tools,
        )
        self.assertIs(
            mcp_tool._register_server_tools,
            mcp_tool_discovery._register_server_tools,
        )
        self.assertIs(
            mcp_tool._register_from_cache_sync,
            mcp_tool_discovery._register_from_cache_sync,
        )
        self.assertIs(mcp_tool._CachedMCPTool, mcp_tool_discovery._CachedMCPTool)
        self.assertIs(inspect.getmodule(mcp_tool.MCPServerTask.run), mcp_tool)
        self.assertIs(
            mcp_tool._make_tool_handler,
            mcp_tool_call._make_tool_handler,
        )

    def test_live_registration_reads_legacy_handler_and_schema_patch_paths(self):
        registry = _Registry()
        tool = SimpleNamespace(
            name="search",
            description="Search docs",
            inputSchema={"type": "object"},
        )
        server = SimpleNamespace(
            _tools=[tool],
            _list_cache_meta={},
            tool_timeout=9,
        )
        schema = {
            "name": "mcp__docs__search",
            "description": "Search docs",
            "parameters": {"type": "object"},
        }
        handler = object()
        with (
            patch("pcbdraft.tools.registry.registry", registry),
            patch.object(mcp_tool, "_normalize_name_filter", return_value=set()),
            patch.object(mcp_tool, "_make_check_fn", return_value=lambda: True),
            patch.object(mcp_tool, "_record_tool_trust_metadata") as trust_metadata,
            patch.object(mcp_tool, "_scan_mcp_description") as scan_description,
            patch.object(
                mcp_tool, "_convert_mcp_schema", return_value=schema
            ) as convert,
            patch.object(
                mcp_tool, "_make_tool_handler", return_value=handler
            ) as make_handler,
            patch.object(mcp_tool, "_select_utility_schemas", return_value=[]),
            patch.object(mcp_tool, "_track_mcp_tool_server") as track,
            patch("pcbdraft.tools.mcp_schema_cache.write_cache_entry"),
        ):
            names = mcp_tool._register_server_tools("docs", server, {})

        self.assertEqual(names, ["mcp__docs__search"])
        self.assertEqual(registry.aliases, [("docs", "mcp-docs")])
        self.assertIs(registry.registered[0]["handler"], handler)
        trust_metadata.assert_called_once_with("docs", {}, [tool])
        scan_description.assert_called_once_with("docs", "search", "Search docs")
        convert.assert_called_once_with("docs", tool)
        make_handler.assert_called_once_with("docs", "search", 9)
        track.assert_called_once_with("mcp__docs__search", "docs")

    def test_cached_registration_updates_legacy_lazy_state(self):
        registry = _Registry()
        raw_tool = {
            "name": "search",
            "description": "Search docs",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        }
        schema = {
            "name": "mcp__docs__search",
            "description": "Search docs",
            "parameters": {"type": "object"},
        }
        lazy_configs: dict = {}
        lazy_fingerprints: dict = {}
        lazy_names: dict = {}
        with (
            patch("pcbdraft.tools.registry.registry", registry),
            patch(
                "pcbdraft.tools.mcp_schema_cache.config_fingerprint",
                return_value="fingerprint",
            ),
            patch(
                "pcbdraft.tools.mcp_schema_cache.tools_from_cache_entry",
                return_value=[raw_tool],
            ),
            patch(
                "pcbdraft.tools.mcp_schema_cache.utility_tools_from_cache_entry",
                return_value=[],
            ),
            patch.object(mcp_tool, "_normalize_name_filter", return_value=set()),
            patch.object(mcp_tool, "_make_check_fn", return_value=lambda: True),
            patch.object(mcp_tool, "_record_tool_trust_metadata"),
            patch.object(mcp_tool, "_scan_mcp_description"),
            patch.object(mcp_tool, "_convert_mcp_schema", return_value=schema),
            patch.object(mcp_tool, "_make_tool_handler", return_value=object()),
            patch.object(mcp_tool, "_track_mcp_tool_server"),
            patch.object(mcp_tool, "_lazy_server_configs", lazy_configs),
            patch.object(mcp_tool, "_lazy_server_fingerprints", lazy_fingerprints),
            patch.object(mcp_tool, "_lazy_server_tool_names", lazy_names),
        ):
            names = mcp_tool._register_from_cache_sync("docs", {"timeout": 8}, {})

        self.assertEqual(names, ["mcp__docs__search"])
        self.assertEqual(lazy_configs, {"docs": {"timeout": 8}})
        self.assertEqual(lazy_fingerprints, {"docs": "fingerprint"})
        self.assertEqual(lazy_names, {"docs": ["mcp__docs__search"]})


class MCPToolDiscoveryAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_uses_legacy_pagination_and_registration_hooks(self):
        task = mcp_tool.MCPServerTask("docs")
        task.session = SimpleNamespace(list_tools=AsyncMock())
        task._config = {"tools": {"include": ["search"]}}
        task._ready.set()
        tools = [SimpleNamespace(name="search")]

        with (
            patch.object(
                mcp_tool,
                "_paginate_full_list",
                new=AsyncMock(return_value=tools),
            ) as paginate,
            patch.object(
                mcp_tool,
                "_register_server_tools",
                return_value=["mcp__docs__search"],
            ) as register,
        ):
            await task._discover_tools()

        self.assertEqual(task._tools, tools)
        self.assertEqual(task._registered_tool_names, ["mcp__docs__search"])
        paginate.assert_awaited_once_with(
            task.session.list_tools,
            "tools",
            "docs",
            cache_meta_out=task._list_cache_meta,
        )
        register.assert_called_once_with("docs", task, task._config)

    async def test_dynamic_refresh_deregisters_stale_tool_through_legacy_hooks(self):
        registry = _Registry()
        registry.owners["mcp__docs__old"] = "mcp-docs"
        task = mcp_tool.MCPServerTask("docs")
        task.session = SimpleNamespace(list_tools=AsyncMock())
        task._config = {}
        task._registered_tool_names = ["mcp__docs__old"]
        fresh_tools = [SimpleNamespace(name="new")]

        with (
            patch("pcbdraft.tools.registry.registry", registry),
            patch.object(
                mcp_tool,
                "_paginate_full_list",
                new=AsyncMock(return_value=fresh_tools),
            ),
            patch.object(
                mcp_tool,
                "mcp_prefixed_tool_name",
                side_effect=lambda server, tool: f"mcp__{server}__{tool}",
            ) as name_builder,
            patch.object(
                mcp_tool,
                "_register_server_tools",
                return_value=["mcp__docs__new"],
            ) as register,
            patch.object(mcp_tool, "_forget_mcp_tool_server") as forget,
            patch.object(mcp_tool.logger, "warning"),
        ):
            await task._refresh_tools()

        self.assertEqual(registry.deregistered, ["mcp__docs__old"])
        self.assertEqual(task._registered_tool_names, ["mcp__docs__new"])
        name_builder.assert_called_once_with("docs", "new")
        register.assert_called_once_with("docs", task, {})
        forget.assert_called_once_with("mcp__docs__old")


if __name__ == "__main__":
    unittest.main()
