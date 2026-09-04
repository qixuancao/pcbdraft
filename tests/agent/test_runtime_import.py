from __future__ import annotations

import subprocess
import sys
import unittest


class NativeRuntimeImportTests(unittest.TestCase):
    def test_runtime_imports_without_flat_aliases_or_source_path_injection(
        self,
    ) -> None:
        script = """
import os
import sys
import tempfile

with tempfile.TemporaryDirectory() as home:
    os.environ['PCBDRAFT_RUNTIME_HOME'] = home
    os.environ['PCBDRAFT_DEBUG_TRACE'] = '0'
    paths = list(sys.path)
    from pcbdraft.interfaces.terminal import activate
    activate()
    from pcbdraft.interfaces.tui.app import TerminalApp
    from pcbdraft.agent.loop import AIAgent
    from pcbdraft.model.provider_profiles import list_providers
    from pcbdraft.agent.extensions.manager import get_plugin_manager
    assert AIAgent.__module__ == 'pcbdraft.agent.loop'
    assert TerminalApp.__module__ == 'pcbdraft.interfaces.tui.app'
    assert len(list_providers()) >= 30
    assert get_plugin_manager().has_middleware('tool_execution')
    assert sys.path == paths
    assert not set(sys.modules) & {'cli', 'run_agent', 'hermes_cli', 'agent', 'tools', 'gateway'}
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_model_loop_dispatches_and_retains_conversation_history(
        self,
    ) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "tests.agent.native_conversation_fixture"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NATIVE_ROUNDTRIP_OK", result.stdout)
