"""Focused application status-projection and compatibility coverage."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.model import tool_calls
from pcbdraft.services import application, application_status_projection
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_status_projection import (
    ApplicationStatusProjectionMixin,
)


class ApplicationStatusProjectionTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_read_views(self) -> None:
        source = Path(application_status_projection.__file__).read_text(
            encoding="utf-8"
        )
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

        for name in ("diagnostics", "inspect_engineering_stage"):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationStatusProjectionMixin, name),
                )

        for retained in (
            "set_repository",
            "_use_repository",
            "_bind_expected_revision",
            "create_project",
            "_current_progress_and_stage",
            "_managed_progress_and_stage",
            "record_progress",
            "send_message",
            "prepare_agent_repair",
            "apply_modification",
            "verify_release",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, ApplicationService.__dict__)
                self.assertNotIn(retained, ApplicationStatusProjectionMixin.__dict__)

    def test_diagnostics_projects_doctor_and_provider_through_legacy_paths(
        self,
    ) -> None:
        provider = SimpleNamespace(
            diagnostic=Mock(
                return_value={
                    "id": "patched-provider",
                    "available": True,
                    "planning": "available",
                }
            )
        )
        doctor = {
            "ok": True,
            "tools": {"kicad-cli": {"available": True}},
            "library_tables": {
                "sym-lib-table": {"configured": True},
                "fp-lib-table": {"configured": True},
            },
            "library_data": {
                "symbols": {"available": True},
                "footprints": {"available": False},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider=provider,
                recover_interrupted=False,
            )
            with (
                patch.object(
                    application, "doctor_report", return_value=doctor
                ) as probe,
                patch.object(
                    tool_calls,
                    "provider_agent_protocol",
                    return_value={"mode": "patched"},
                ) as protocol,
            ):
                result = service.diagnostics()

        probe.assert_called_once_with()
        protocol.assert_called_once_with(provider)
        provider.diagnostic.assert_called_once_with()
        self.assertEqual(result["provider"]["id"], "patched-provider")
        self.assertEqual(
            result["agent_orchestration"]["router"],
            {"mode": "patched"},
        )
        self.assertFalse(result["ready_for_generation"])
        self.assertEqual(result["repository"]["source"], "explicit")

    def test_engineering_stage_projects_revisions_and_legacy_run_id_matcher(
        self,
    ) -> None:
        run_id = "20260917T120000Z-1234abcd"
        project = SimpleNamespace(
            state={
                "revision": 9,
                "design_revision": 4,
                "last_validation": {"run_id": run_id},
            }
        )
        stage = SimpleNamespace(
            to_dict=Mock(
                return_value={
                    "stage": "validated",
                    "evidence_complete": True,
                }
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            with (
                patch.object(service, "_open", return_value=project) as opener,
                patch.object(
                    service,
                    "_current_progress_and_stage",
                    return_value=(SimpleNamespace(), stage),
                ) as projector,
                patch.object(application.re, "fullmatch", return_value=True) as match,
            ):
                result = service.inspect_engineering_stage("board-one")

        opener.assert_called_once_with("board-one")
        projector.assert_called_once_with(project)
        match.assert_called_once_with(
            r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}",
            run_id,
        )
        stage.to_dict.assert_called_once_with()
        self.assertEqual(result["project_id"], "board-one")
        self.assertEqual(result["live_revision"], 9)
        self.assertEqual(result["design_revision"], 4)
        self.assertEqual(result["evidence_source"], f"validation-run:{run_id}")
        self.assertEqual(result["stage"], "validated")

    def test_draft_stage_is_read_only_and_reports_missing_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            project_id = service.create_draft("Stage projection")["project"]["id"]
            root = service.project_root(project_id)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*.json")
            }

            result = service.inspect_engineering_stage(project_id)

            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*.json")
            }

        self.assertEqual(after, before)
        self.assertEqual(result["stage"], "not_started")
        self.assertFalse(result["release_gate_passed"])
        self.assertEqual(result["blockers"], ["requirements_not_frozen"])
        self.assertEqual(result["evidence_source"], "validation-run:none")


if __name__ == "__main__":
    unittest.main()
