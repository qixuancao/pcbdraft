"""Focused ownership and behavior tests for application PCB tool dispatch."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.services import application, application_tool_dispatch
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_tool_dispatch import (
    ApplicationPCBToolDispatchMixin,
)


class ApplicationPCBToolDispatchTests(unittest.TestCase):
    @staticmethod
    def _service() -> ApplicationService:
        return object.__new__(ApplicationService)

    def test_mixin_has_no_reverse_import_and_owns_only_dispatch(self) -> None:
        source = Path(application_tool_dispatch.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("pcbdraft.services.application", imports)
        self.assertIs(
            ApplicationService.execute_pcb_tool,
            ApplicationPCBToolDispatchMixin.execute_pcb_tool,
        )
        for retained in (
            "register_kicad_part",
            "run_pcb_check",
            "render_pcb_output",
            "export_pcb_output",
            "apply_pcb_operation",
            "_inspect_pcb_tool",
        ):
            self.assertNotIn(retained, ApplicationPCBToolDispatchMixin.__dict__)

    def test_inspect_project_composes_the_public_view_and_fact_result(self) -> None:
        service = self._service()
        view = {"project": {"id": "board"}}
        expected = {"tool_result": {"inspection": "project"}}
        with (
            patch.object(service, "open_project", return_value=view) as open_project,
            patch.object(service, "_with_tool_result", return_value=expected) as attach,
        ):
            result = service.execute_pcb_tool(
                "board",
                "inspect_project",
                {},
                timeout=4.0,
                expected_revision=7,
            )

        self.assertIs(result, expected)
        open_project.assert_called_once_with("board")
        attach.assert_called_once_with(view, {"inspection": "project"})

    def test_dispatch_families_preserve_arguments_timeout_and_revision(self) -> None:
        cases = (
            (
                "inspect_design",
                "_inspect_pcb_tool",
                ("board", "inspect_design", {"component": "U1"}),
                {},
            ),
            (
                "search_symbols",
                "_inspect_library_tool",
                ("board", "search_symbols", {"query": "sensor"}),
                {},
            ),
            (
                "describe_part",
                "_inspect_part_tool",
                ("board", "describe_part", {"part_id": "part-1"}),
                {},
            ),
            (
                "register_kicad_part",
                "register_kicad_part",
                ("board", {"symbol": "Device:R"}),
                {"timeout": 4.0, "expected_revision": 7},
            ),
            (
                "run_drc",
                "run_pcb_check",
                ("board", "run_drc"),
                {"timeout": 4.0, "expected_revision": 7},
            ),
            (
                "render_board",
                "render_pcb_output",
                ("board", "render_board"),
                {"timeout": 4.0, "expected_revision": 7},
            ),
            (
                "export_gerbers",
                "export_pcb_output",
                ("board", "export_gerbers"),
                {"timeout": 4.0, "expected_revision": 7},
            ),
            (
                "move_footprint",
                "apply_pcb_operation",
                ("board", "move_footprint", {"component_id": "U1"}),
                {"timeout": 4.0, "expected_revision": 7},
            ),
        )
        for tool_name, target, expected_args, expected_kwargs in cases:
            with self.subTest(tool_name=tool_name):
                service = self._service()
                arguments = (
                    {"value": {"symbol": "Device:R"}}
                    if tool_name == "register_kicad_part"
                    else expected_args[-1]
                    if target.startswith("_inspect") or target == "apply_pcb_operation"
                    else {}
                )
                marker = {"dispatched": tool_name}
                with patch.object(service, target, return_value=marker) as dispatched:
                    result = service.execute_pcb_tool(
                        "board",
                        tool_name,
                        arguments,
                        timeout=4.0,
                        expected_revision=7,
                    )

                self.assertIs(result, marker)
                dispatched.assert_called_once_with(*expected_args, **expected_kwargs)

    def test_board_region_observation_requires_the_exact_revision(self) -> None:
        service = self._service()
        view = {"state": {"revision": 7}}
        expected = {"tool_result": {"operation": "observe_board_region"}}
        with (
            patch.object(service, "open_project", return_value=view),
            patch.object(service, "_with_tool_result", return_value=expected) as attach,
        ):
            result = service.execute_pcb_tool(
                "board",
                "observe_board_region",
                {"x_mm": 4.0, "y_mm": 5.0},
                timeout=4.0,
                expected_revision=7,
            )

        self.assertIs(result, expected)
        attach.assert_called_once_with(
            view,
            {"operation": "observe_board_region", "x_mm": 4.0, "y_mm": 5.0},
        )

        class PatchedValidationError(Exception):
            pass

        with (
            patch.object(service, "open_project", return_value=view),
            patch.object(application, "ValidationError", PatchedValidationError),
            self.assertRaisesRegex(
                PatchedValidationError,
                "project changed before the board region could be observed",
            ),
        ):
            service.execute_pcb_tool(
                "board",
                "observe_board_region",
                {},
                timeout=4.0,
                expected_revision=6,
            )


if __name__ == "__main__":
    unittest.main()
