from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.tools import mcp_tool, mcp_tool_schema


class MCPToolSchemaCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_symbols_remain(self):
        source = Path(mcp_tool_schema.__file__).read_text(encoding="utf-8")
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

        names = (
            "sanitize_mcp_name_component",
            "mcp_prefixed_tool_name",
            "_convert_mcp_schema",
            "_build_utility_schemas",
            "_normalize_name_filter",
            "matches_name_filter",
            "_parse_boolish",
            "_get_lifecycle_seconds",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(mcp_tool, name)))
        self.assertEqual(
            mcp_tool.MCP_TOOL_NAME_PREFIX,
            mcp_tool_schema.MCP_TOOL_NAME_PREFIX,
        )
        self.assertIs(
            mcp_tool._normalize_mcp_input_schema,
            mcp_tool_schema._normalize_mcp_input_schema,
        )

    def test_legacy_name_wrappers_preserve_sanitizer_patch_path(self):
        self.assertEqual(
            mcp_tool_schema.mcp_prefixed_tool_name("docs-server", "find/docs"),
            "mcp__docs_server__find_docs",
        )

        with (
            patch.object(
                mcp_tool,
                "sanitize_mcp_name_component",
                side_effect=lambda value: f"safe_{value}",
            ) as sanitizer,
            patch.object(mcp_tool, "MCP_TOOL_NAME_PREFIX", "custom::"),
        ):
            self.assertEqual(
                mcp_tool.mcp_prefixed_tool_name("docs", "find"),
                "custom::safe_docs__safe_find",
            )
        self.assertEqual(sanitizer.call_count, 2)

    def test_legacy_conversion_wrapper_resolves_patched_dependencies(self):
        tool = SimpleNamespace(
            name="find-docs",
            description="<tag>Find docs</tag>",
            inputSchema={"type": "object"},
        )
        with (
            patch.object(
                mcp_tool,
                "_normalize_mcp_input_schema",
                return_value={"normalized": True},
            ) as normalizer,
            patch.object(
                mcp_tool,
                "mcp_field",
                return_value={"raw": True},
            ) as field_reader,
            patch.object(
                mcp_tool,
                "strip_unicode_tags",
                side_effect=lambda value: f"clean:{value}",
            ) as sanitizer,
        ):
            schema = mcp_tool._convert_mcp_schema("docs-server", tool)

        self.assertEqual(
            schema,
            {
                "name": "mcp__docs_server__find_docs",
                "description": "clean:<tag>Find docs</tag>",
                "parameters": {"normalized": True},
            },
        )
        field_reader.assert_called_once_with(tool, "input_schema", "inputSchema")
        normalizer.assert_called_once_with({"raw": True})
        sanitizer.assert_called_once_with("<tag>Find docs</tag>")

    def test_utility_wrapper_resolves_patched_name_builder(self):
        with patch.object(
            mcp_tool,
            "mcp_prefixed_tool_name",
            side_effect=lambda server, tool: f"{server}:{tool}",
        ) as name_builder:
            utilities = mcp_tool._build_utility_schemas("docs")

        self.assertEqual(
            [entry["handler_key"] for entry in utilities],
            ["list_resources", "read_resource", "list_prompts", "get_prompt"],
        )
        self.assertEqual(utilities[1]["schema"]["name"], "docs:read_resource")
        self.assertEqual(name_builder.call_count, 4)


class MCPToolSchemaTests(unittest.TestCase):
    def test_input_schema_normalization_is_recursive_and_non_mutating(self):
        schema = {
            "type": "object",
            "definitions": {
                "Thing": {
                    "type": "object",
                    "required": ["known", "missing"],
                    "properties": {"known": {"type": "string"}},
                }
            },
            "properties": {
                "definitions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "item": {"$ref": "#/definitions/Thing"},
                "mode": {
                    "anyOf": [
                        {"const": "a"},
                        {"const": "b"},
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "nested": {
                    "required": ["kept", "missing"],
                    "properties": {"kept": {"type": "string"}},
                },
            },
            "required": ["item", "missing"],
        }
        original = copy.deepcopy(schema)

        normalized = mcp_tool._normalize_mcp_input_schema(schema)

        self.assertEqual(schema, original)
        self.assertNotIn("definitions", normalized)
        self.assertEqual(
            normalized["properties"]["definitions"],
            {"type": "array", "items": {"type": "string"}},
        )
        self.assertEqual(normalized["properties"]["item"]["$ref"], "#/$defs/Thing")
        self.assertEqual(normalized["required"], ["item"])
        self.assertEqual(normalized["$defs"]["Thing"]["required"], ["known"])
        self.assertEqual(
            normalized["properties"]["mode"],
            {
                "type": "string",
                "enum": ["a", "b"],
                "nullable": True,
                "default": None,
            },
        )
        self.assertEqual(normalized["properties"]["nested"]["type"], "object")
        self.assertEqual(normalized["properties"]["nested"]["required"], ["kept"])

    def test_input_schema_normalization_repairs_empty_top_level(self):
        expected = {"type": "object", "properties": {}}

        self.assertEqual(mcp_tool_schema._normalize_mcp_input_schema(None), expected)
        self.assertEqual(
            mcp_tool_schema._normalize_mcp_input_schema({"type": "object"}),
            expected,
        )

    def test_filters_boolish_and_lifecycle_parsing(self):
        patterns = mcp_tool_schema._normalize_name_filter(
            ["read_exact", "list_*"],
            "tools",
        )
        self.assertTrue(mcp_tool_schema.matches_name_filter("read_exact", patterns))
        self.assertTrue(mcp_tool_schema.matches_name_filter("list_docs", patterns))
        self.assertFalse(mcp_tool_schema.matches_name_filter("LIST_docs", patterns))
        self.assertFalse(mcp_tool_schema.matches_name_filter("write_docs", patterns))

        self.assertTrue(mcp_tool_schema._parse_boolish(" yes ", default=False))
        self.assertFalse(mcp_tool_schema._parse_boolish("off"))
        self.assertEqual(
            mcp_tool_schema._get_lifecycle_seconds(
                {"idle": "12.5", "lifecycle": {"idle": 99}},
                "idle",
            ),
            12.5,
        )
        self.assertEqual(
            mcp_tool_schema._get_lifecycle_seconds(
                {"lifecycle": {"idle": 7}},
                "idle",
            ),
            7.0,
        )
        self.assertIsNone(mcp_tool_schema._get_lifecycle_seconds({"idle": 0}, "idle"))

    def test_legacy_config_wrappers_preserve_logger_patch_path(self):
        with patch.object(mcp_tool.logger, "warning") as warning:
            self.assertEqual(mcp_tool._normalize_name_filter(42, "include"), set())
            self.assertFalse(mcp_tool._parse_boolish("maybe", default=False))
            self.assertIsNone(
                mcp_tool._get_lifecycle_seconds({"idle": "never"}, "idle")
            )
        self.assertEqual(warning.call_count, 3)


if __name__ == "__main__":
    unittest.main()
