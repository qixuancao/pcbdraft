"""Focused tests for pure turn-result formatting."""

from __future__ import annotations

import ast
import copy
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop, turn_result_formatting
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.turn_result_formatting import TurnResultFormattingMixin


class TurnResultFormattingCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_module_has_no_reverse_import_and_agent_inherits_methods(self) -> None:
        source = Path(turn_result_formatting.__file__).read_text(encoding="utf-8")
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

        self.assertNotIn("pcbdraft.agent.loop", imports)
        self.assertTrue(issubclass(AIAgent, TurnResultFormattingMixin))
        for name in (
            "_neutralize_footer_paths",
            "_format_file_mutation_failure_footer",
            "_format_turn_completion_explanation",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(AIAgent, name),
                    inspect.getattr_static(TurnResultFormattingMixin, name),
                )
        self.assertIs(
            AIAgent._FOOTER_PATH_RE,
            TurnResultFormattingMixin._FOOTER_PATH_RE,
        )
        for host_method in (
            "_record_file_mutation_result",
            "_file_mutation_verifier_enabled",
            "_turn_completion_explainer_enabled",
            "run_conversation",
        ):
            with self.subTest(host_method=host_method):
                self.assertIs(inspect.getmodule(getattr(AIAgent, host_method)), loop)

    def test_legacy_class_pattern_and_formatter_patch_paths_remain_dynamic(
        self,
    ) -> None:
        fake_pattern = SimpleNamespace(sub=MagicMock(return_value="neutralized"))
        with patch.object(AIAgent, "_FOOTER_PATH_RE", fake_pattern):
            self.assertEqual(self.agent._neutralize_footer_paths("raw"), "neutralized")
        replacement, text = fake_pattern.sub.call_args.args
        self.assertTrue(callable(replacement))
        self.assertEqual(text, "raw")

        failed = {"/tmp/board.kicad_pcb": {"tool": "patch", "error_preview": "bad"}}
        with patch.object(
            AIAgent,
            "_neutralize_footer_paths",
            return_value="safe footer",
        ) as neutralize:
            self.assertEqual(
                self.agent._format_file_mutation_failure_footer(failed),
                "safe footer",
            )
        neutralize.assert_called_once()
        self.assertIn("`/tmp/board.kicad_pcb`", neutralize.call_args.args[0])


class TurnResultFormattingBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_failure_footer_is_bounded_and_neutralizes_preview_paths(self) -> None:
        failed = {
            f"/tmp/board-{index}.kicad_pcb": {
                "tool": "write_file" if index == 0 else "patch",
                "error_preview": (
                    "could not read /home/user/config.yaml" if index == 0 else ""
                ),
            }
            for index in range(12)
        }
        original = copy.deepcopy(failed)

        footer = self.agent._format_file_mutation_failure_footer(failed)

        self.assertIn("File-mutation verifier: 12 file(s)", footer)
        self.assertIn("`/tmp/board-0.kicad_pcb`", footer)
        self.assertIn("`/home/user/config.yaml`", footer)
        self.assertIn("• … and 2 more", footer)
        self.assertNotIn("board-10.kicad_pcb", footer)
        self.assertEqual(failed, original)
        self.assertEqual(self.agent._format_file_mutation_failure_footer({}), "")

    def test_completion_explanation_projects_normal_and_failure_reasons(self) -> None:
        cases = (
            ("", None, ""),
            ("text_response(finish_reason=stop)", None, ""),
            ("unknown", None, ""),
            ("max_iterations_reached(10/10)", None, "maximum tool-iteration"),
            ("pending_tool_result", None, "tool result was still pending"),
            ("session_persistence_failed", "locked", "session storage was busy"),
            (
                "session_persistence_failed",
                "corrupt",
                "structural corruption",
            ),
            ("session_persistence_failed", "disk", "full disk"),
        )

        for reason, cause, expected in cases:
            with self.subTest(reason=reason, cause=cause):
                rendered = self.agent._format_turn_completion_explanation(reason, cause)
                if expected:
                    self.assertIn(expected, rendered)
                    self.assertTrue(rendered.startswith("⚠️ No reply: "))
                else:
                    self.assertEqual(rendered, "")


if __name__ == "__main__":
    unittest.main()
