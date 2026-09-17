from __future__ import annotations

import ast
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.services import application, application_native_outputs
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_native_outputs import (
    ApplicationNativeOutputsMixin,
)


class ApplicationNativeOutputsTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_native_outputs(self):
        source = Path(application_native_outputs.__file__).read_text(encoding="utf-8")
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

        for name in (
            "run_pcb_check",
            "render_pcb_output",
            "export_pcb_output",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationNativeOutputsMixin, name),
                )

        for retained in (
            "_native_progress_sources",
            "_retained_check_progress",
            "_bind_aggregate_validation_revision",
            "_aggregate_check_progress",
        ):
            self.assertIn(retained, ApplicationService.__dict__)
            self.assertNotIn(retained, ApplicationNativeOutputsMixin.__dict__)
        self.assertNotIn("_with_tool_result", ApplicationNativeOutputsMixin.__dict__)

    def test_check_rejects_incomplete_receipt_through_legacy_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            root = Path(temporary) / "project"
            design_root = root / "design"
            design_root.mkdir(parents=True)
            project = SimpleNamespace(
                root=root,
                design_root=design_root,
                state={"revision": 4, "design_revision": 3},
            )
            managed = SimpleNamespace(assert_synchronized=Mock())
            check_result = SimpleNamespace(report_path=root / "unused.json")

            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=4),
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                patch.object(application, "new_run_id", return_value="check-id"),
                patch.object(
                    application,
                    "run_individual_check",
                    return_value=check_result,
                ) as checker,
                patch.object(application, "load_json_limited", return_value={}) as load,
                self.assertRaisesRegex(
                    ValidationError,
                    "individual PCB check receipt is incomplete",
                ),
            ):
                service.run_pcb_check(
                    "board",
                    "run_drc",
                    timeout=12.0,
                    expected_revision=4,
                )

            opener.assert_called_once_with(design_root)
            managed.assert_synchronized.assert_called_once_with()
            checker.assert_called_once_with(
                managed,
                "run_drc",
                output=root / "validation" / "check-id",
                timeout=12.0,
            )
            load.assert_called_once_with(
                root / "validation" / "check-id" / "receipt.json",
                application.APP_FILE_LIMIT,
            )

    def test_render_records_mocked_native_output_through_legacy_patch_points(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            root = Path(temporary) / "project"
            design_root = root / "design"
            design_root.mkdir(parents=True)
            state = {"revision": 5, "design_revision": 3}
            project = SimpleNamespace(
                root=root,
                design_root=design_root,
                state=state,
                conversation={"messages": []},
            )
            design = SimpleNamespace(content_hash=Mock(return_value="design-hash"))
            managed = SimpleNamespace(design=design, assert_synchronized=Mock())
            output_root = root / "previews" / "render-id"
            bundle = SimpleNamespace(
                root=output_root,
                receipt_path=output_root / "receipt.json",
                design_content_hash="design-hash",
                files={"board": output_root / "board.svg"},
            )
            public = {"project": {"id": "board"}, "state": state}

            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=5),
                patch.object(service, "_event") as event,
                patch.object(service, "_write_records") as write_records,
                patch.object(service, "open_project", return_value=public),
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                patch.object(application, "new_run_id", return_value="render-id"),
                patch.object(
                    application,
                    "generate_preview",
                    return_value=bundle,
                ) as render,
                patch.object(
                    application,
                    "ResourceLock",
                    return_value=nullcontext(),
                ) as lock,
                patch.object(application, "utc_timestamp", return_value="timestamp"),
            ):
                result = service.render_pcb_output(
                    "board",
                    "render_board",
                    timeout=9.0,
                    expected_revision=5,
                )

            self.assertEqual(opener.call_count, 2)
            render.assert_called_once_with(
                managed,
                output_root,
                "render_board",
                timeout=9.0,
            )
            lock.assert_called_once_with(root, service.locks_root)
            self.assertEqual(state["revision"], 6)
            self.assertEqual(state["updated_at"], "timestamp")
            self.assertEqual(
                state["last_preview"]["files"],
                {"board": "previews/render-id/board.svg"},
            )
            event.assert_called_once_with(
                state,
                root,
                "pcb.render_complete",
                "Completed individual PCB render render_board",
            )
            write_records.assert_called_once_with(root, state, project.conversation)
            self.assertEqual(result["tool_result"]["revision"], 6)
            self.assertEqual(
                result["tool_result"]["design_content_hash"], "design-hash"
            )


if __name__ == "__main__":
    unittest.main()
