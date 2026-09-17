"""Focused boundary and legacy-hook tests for PCB operation transactions."""

from __future__ import annotations

import ast
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.kicad.consistency import NativeDeltaCheck, NativeOperationDeltaReport
from pcbdraft.services import application, application_pcb_operations
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_pcb_operations import (
    ApplicationPCBOperationsMixin,
)
from tests.services.test_flat_pcb_tools import (
    _managed,
    _materialize_project,
    _passing_consistency,
    _seed_managed_project,
)


class ApplicationPCBOperationsTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_the_transaction_boundary(
        self,
    ) -> None:
        source = Path(application_pcb_operations.__file__).read_text(encoding="utf-8")
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

        for name in ("apply_pcb_operation", "_retain_generated_route"):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationPCBOperationsMixin, name),
                )

        for retained in (
            "_native_progress_sources",
            "_retained_check_progress",
            "_route_convergence_decision",
            "_flat_semantic_operations",
            "_event",
            "_write_records",
        ):
            self.assertNotIn(retained, ApplicationPCBOperationsMixin.__dict__)

    def test_managed_project_open_resolves_the_legacy_application_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design_root = Path(temporary) / "design"
            design_root.mkdir()
            project = SimpleNamespace(
                design_root=design_root,
                state={"revision": 3},
            )
            synchronized = Mock(side_effect=RuntimeError("stop after patched open"))
            managed = SimpleNamespace(assert_synchronized=synchronized)
            service = ApplicationService(temporary, provider_name="auto")

            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=3),
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                self.assertRaisesRegex(RuntimeError, "stop after patched open"),
            ):
                service.apply_pcb_operation(
                    "board",
                    "add_component",
                    {},
                    timeout=2.0,
                    expected_revision=3,
                )

            opener.assert_called_once_with(design_root)
            synchronized.assert_called_once_with()

    def test_transaction_commits_through_late_bound_native_and_lock_hooks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, _before = _seed_managed_project(Path(temporary))
            delta = NativeOperationDeltaReport(
                "set_board_outline",
                (NativeDeltaCheck("outline_changed", True, "changed", "changed"),),
                "outline",
            )
            with (
                patch.object(
                    application,
                    "open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch.object(
                    application,
                    "load_generation_request",
                    return_value=object(),
                ),
                patch.object(
                    application,
                    "materialize_managed_design",
                    side_effect=_materialize_project,
                ) as materialize,
                patch.object(
                    application,
                    "inspect_native_consistency",
                    return_value=_passing_consistency(),
                ) as inspect,
                patch.object(
                    application,
                    "compare_native_operation_delta",
                    return_value=delta,
                ) as compare_delta,
                patch.object(
                    application, "_native_board_projection", return_value=None
                ),
                patch.object(
                    application,
                    "_native_schematic_projection",
                    return_value=None,
                ),
                patch.object(
                    application, "ResourceLock", return_value=nullcontext()
                ) as lock,
                patch.object(
                    service,
                    "_native_progress_sources",
                    return_value=(None, None),
                ),
            ):
                result = service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            committed = service._open(project_id)
            self.assertEqual(committed.state["revision"], 1)
            self.assertEqual(committed.state["design_revision"], 1)
            self.assertEqual(result["tool_result"]["operation"], "set_board_outline")
            materialize.assert_called_once()
            inspect.assert_called_once()
            compare_delta.assert_called_once()
            lock.assert_called_once_with(committed.root, service.locks_root)

    def test_publication_failure_restores_design_and_project_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, before = _seed_managed_project(Path(temporary))
            delta = NativeOperationDeltaReport(
                "set_board_outline",
                (NativeDeltaCheck("outline_changed", True, "changed", "changed"),),
                "outline",
            )
            with (
                patch.object(
                    application,
                    "open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch.object(
                    application,
                    "load_generation_request",
                    return_value=object(),
                ),
                patch.object(
                    application,
                    "materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch.object(
                    application,
                    "inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
                patch.object(
                    application,
                    "compare_native_operation_delta",
                    return_value=delta,
                ),
                patch.object(
                    application, "_native_board_projection", return_value=None
                ),
                patch.object(
                    application,
                    "_native_schematic_projection",
                    return_value=None,
                ),
                patch.object(application, "ResourceLock", return_value=nullcontext()),
                patch.object(
                    service,
                    "_native_progress_sources",
                    return_value=(None, None),
                ),
                patch.object(
                    service,
                    "_write_records",
                    side_effect=PCBDraftError("injected publication failure"),
                ),
                self.assertRaisesRegex(PCBDraftError, "publication failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state["revision"], 0)
            self.assertEqual(restored.state["design_revision"], 0)
            self.assertEqual(_managed(restored.design_root).design, before)


if __name__ == "__main__":
    unittest.main()
