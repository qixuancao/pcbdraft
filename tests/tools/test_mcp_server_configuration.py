from __future__ import annotations

import ast
import inspect
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.tools import mcp_server_configuration, mcp_server_task, mcp_tool


class MCPServerConfigurationCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_identities_remain(self):
        source = Path(mcp_server_configuration.__file__).read_text(encoding="utf-8")
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
            "_build_safe_env",
            "_resolve_stdio_command",
            "_wrap_command_with_watchdog",
            "_interpolate_env_vars",
            "_filter_suspicious_mcp_servers",
            "_load_mcp_config",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(mcp_tool, name),
                    getattr(mcp_server_configuration, name),
                )
        self.assertIs(
            mcp_tool._ENV_VAR_PATTERN,
            mcp_server_configuration._ENV_VAR_PATTERN,
        )
        self.assertIs(inspect.getmodule(mcp_tool.MCPServerTask), mcp_server_task)
        self.assertIs(inspect.getmodule(mcp_tool.register_mcp_servers), mcp_tool)

    def test_legacy_interpolation_reads_context_patch_path_late(self):
        secrets = {"TOKEN": "secret-token"}
        with (
            patch.object(
                mcp_tool,
                "_context_var_value",
                side_effect=lambda ref: (
                    "/workspace" if ref == "workspaceFolder" else None
                ),
            ) as context_value,
            patch(
                "pcbdraft.agent.secret_scope.get_secret",
                side_effect=lambda name, default=None: secrets.get(name, default),
            ),
        ):
            result = mcp_tool._interpolate_env_vars(
                {
                    "cwd": "${workspaceFolder}",
                    "headers": {"Authorization": "Bearer ${env:TOKEN}"},
                }
            )

        self.assertEqual(
            result,
            {
                "cwd": "/workspace",
                "headers": {"Authorization": "Bearer secret-token"},
            },
        )
        self.assertGreaterEqual(context_value.call_count, 2)


class MCPServerConfigurationTests(unittest.TestCase):
    def test_safe_env_keeps_baseline_explicit_and_injected_secret_only(self):
        process_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "XDG_CACHE_HOME": "/tmp/cache",
            "INJECTED_SECRET": "from-backend",
            "UNRELATED_SECRET": "must-not-leak",
        }
        with (
            patch.dict(os.environ, process_env, clear=True),
            patch(
                "pcbdraft.model.env_loader.get_secret_source",
                side_effect=lambda key: "vault" if key == "INJECTED_SECRET" else None,
            ),
        ):
            result = mcp_tool._build_safe_env({"EXPLICIT": "configured"})

        self.assertEqual(result["PATH"], "/usr/bin")
        self.assertEqual(result["HOME"], "/home/tester")
        self.assertEqual(result["XDG_CACHE_HOME"], "/tmp/cache")
        self.assertEqual(result["INJECTED_SECRET"], "from-backend")
        self.assertEqual(result["EXPLICIT"], "configured")
        self.assertNotIn("UNRELATED_SECRET", result)

    def test_stdio_command_resolution_uses_legacy_prepend_hook(self):
        with (
            patch.object(
                mcp_server_configuration.shutil,
                "which",
                return_value="/opt/node/bin/npx",
            ),
            patch.object(
                mcp_tool,
                "_prepend_path",
                wraps=mcp_server_configuration._prepend_path,
            ) as prepend_path,
        ):
            command, env = mcp_tool._resolve_stdio_command("npx", {"PATH": "/usr/bin"})

        self.assertEqual(command, "/opt/node/bin/npx")
        self.assertEqual(env["PATH"], f"/opt/node/bin{os.pathsep}/usr/bin")
        prepend_path.assert_called_once_with({"PATH": "/usr/bin"}, "/opt/node/bin")

    def test_load_config_normalizes_remote_transport_values(self):
        config = {
            "mcp_servers": {
                "remote": {
                    "transport": "sse",
                    "url": "${MCP_URL}",
                    "cwd": "${workspaceFolder}",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                }
            }
        }
        secrets = {
            "MCP_URL": "https://mcp.example.test/sse",
            "TOKEN": "test-token",
        }
        manager = SimpleNamespace(get_portable_mcp_servers=dict)
        with (
            patch("pcbdraft.core.runtime_utils.env_var_enabled", return_value=False),
            patch("pcbdraft.model.configuration.load_config", return_value=config),
            patch("pcbdraft.model.env_loader.load_pcbdraft_dotenv"),
            patch(
                "pcbdraft.interfaces.tui.mcp_security.validate_mcp_server_entry",
                return_value=[],
            ),
            patch("pcbdraft.agent.extensions.manager.discover_plugins"),
            patch(
                "pcbdraft.agent.extensions.manager.get_plugin_manager",
                return_value=manager,
            ),
            patch.object(
                mcp_tool,
                "_context_var_value",
                side_effect=lambda ref: (
                    "/workspace" if ref == "workspaceFolder" else None
                ),
            ),
            patch(
                "pcbdraft.agent.secret_scope.get_secret",
                side_effect=lambda name, default=None: secrets.get(name, default),
            ),
        ):
            result = mcp_tool._load_mcp_config()

        self.assertEqual(
            result,
            {
                "remote": {
                    "transport": "sse",
                    "url": "https://mcp.example.test/sse",
                    "cwd": "/workspace",
                    "headers": {"Authorization": "Bearer test-token"},
                }
            },
        )

    def test_load_config_drops_suspicious_and_invalid_server_entries(self):
        config = {
            "mcp_servers": {
                "safe": {"command": "python", "args": ["server.py"]},
                "blocked": {"command": "curl", "args": ["https://bad.test"]},
                "invalid": "python server.py",
            }
        }
        manager = SimpleNamespace(get_portable_mcp_servers=dict)

        def validate(name, _config):
            return ["unsafe command"] if name == "blocked" else []

        with (
            patch("pcbdraft.core.runtime_utils.env_var_enabled", return_value=False),
            patch("pcbdraft.model.configuration.load_config", return_value=config),
            patch("pcbdraft.model.env_loader.load_pcbdraft_dotenv"),
            patch(
                "pcbdraft.interfaces.tui.mcp_security.validate_mcp_server_entry",
                side_effect=validate,
            ),
            patch("pcbdraft.agent.extensions.manager.discover_plugins"),
            patch(
                "pcbdraft.agent.extensions.manager.get_plugin_manager",
                return_value=manager,
            ),
            patch.object(mcp_tool.logger, "warning") as warning,
        ):
            result = mcp_tool._load_mcp_config()

        self.assertEqual(
            result,
            {"safe": {"command": "python", "args": ["server.py"]}},
        )
        warning.assert_any_call(
            "Skipping suspicious MCP server '%s': %s",
            "blocked",
            "unsafe command",
        )

    def test_safe_mode_skips_config_and_plugin_reads(self):
        with (
            patch("pcbdraft.core.runtime_utils.env_var_enabled", return_value=True),
            patch("pcbdraft.model.configuration.load_config") as load_config,
            patch("pcbdraft.agent.extensions.manager.discover_plugins") as discover,
        ):
            self.assertEqual(mcp_tool._load_mcp_config(), {})

        load_config.assert_not_called()
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
