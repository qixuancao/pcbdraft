from __future__ import annotations

import ast
import inspect
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.tools import mcp_protocol_policy, mcp_tool


class MCPProtocolPolicyCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_identities_remain(self):
        source = Path(mcp_protocol_policy.__file__).read_text(encoding="utf-8")
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
        for name in (
            "_sanitize_error",
            "_exc_str",
            "_handshake_rejected_as_modern",
            "_is_method_not_found_error",
            "_scan_mcp_description",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(mcp_tool, name),
                    getattr(mcp_protocol_policy, name),
                )
        self.assertIs(
            mcp_tool._CREDENTIAL_PATTERN,
            mcp_protocol_policy._CREDENTIAL_PATTERN,
        )
        self.assertIs(inspect.getmodule(mcp_tool._make_tool_handler), mcp_tool)

    def test_error_shaping_reads_legacy_pattern_patch_path(self):
        with patch.object(
            mcp_tool,
            "_CREDENTIAL_PATTERN",
            re.compile(r"private-value"),
        ):
            rendered = mcp_tool._sanitize_error("failed with private-value")

        self.assertEqual(rendered, "failed with [REDACTED]")
        self.assertEqual(mcp_tool._exc_str(Exception()), "Exception()")
        self.assertEqual(mcp_tool._exc_str(RuntimeError(" failed ")), "failed")

    def test_protocol_error_detection_handles_structured_and_message_forms(self):
        structured = SimpleNamespace(error=SimpleNamespace(code=-32601))

        self.assertTrue(mcp_tool._is_method_not_found_error(structured))
        self.assertTrue(
            mcp_tool._is_method_not_found_error(Exception("Unknown method"))
        )
        self.assertTrue(
            mcp_tool._handshake_rejected_as_modern(SimpleNamespace(code=-32022))
        )
        self.assertFalse(mcp_tool._is_method_not_found_error(Exception("timeout")))

    def test_handshake_policy_reads_legacy_detector_patch_path(self):
        with patch.object(
            mcp_tool,
            "_is_method_not_found_error",
            return_value=True,
        ) as detector:
            self.assertTrue(
                mcp_tool._handshake_rejected_as_modern(Exception("custom rejection"))
            )

        detector.assert_called_once()

    def test_description_scan_reads_legacy_patterns_and_logger(self):
        with (
            patch.object(
                mcp_tool,
                "_MCP_INJECTION_PATTERNS",
                [(re.compile(r"danger", re.IGNORECASE), "test finding")],
            ),
            patch.object(mcp_tool.logger, "warning") as warning,
        ):
            findings = mcp_tool._scan_mcp_description(
                "docs",
                "search",
                "DANGER payload",
            )

        self.assertEqual(findings, ["test finding"])
        warning.assert_called_once_with(
            "MCP server '%s' tool '%s': suspicious description content — %s. "
            "Description: %.200s",
            "docs",
            "search",
            "test finding",
            "DANGER payload",
        )


if __name__ == "__main__":
    unittest.main()
