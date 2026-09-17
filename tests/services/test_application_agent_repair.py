"""Focused proposal/event extraction and compatibility coverage."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.services import application, application_agent_repair
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_agent_repair import ApplicationAgentRepairMixin


class ApplicationAgentRepairTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_read_only_helpers(self) -> None:
        source = Path(application_agent_repair.__file__).read_text(encoding="utf-8")
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
            "events",
            "_prepare_proposal",
            "_attach_plan",
            "_proposal_message",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationAgentRepairMixin, name),
                )

        for retained in (
            "prepare_agent_repair",
            "_record_failure",
            "apply_modification",
            "confirm_project",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, ApplicationService.__dict__)
                self.assertNotIn(retained, ApplicationAgentRepairMixin.__dict__)

    def test_events_uses_legacy_application_reader_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "events" / "00000001.json"
            event_path.parent.mkdir()
            atomic_write_json(
                event_path,
                {"sequence": 1, "kind": "repair.plan_ready", "summary": "Ready"},
            )
            service = ApplicationService(root / "workspace", provider_name="auto")

            with (
                patch.object(
                    service,
                    "_open",
                    return_value=SimpleNamespace(root=root),
                ),
                patch.object(
                    application,
                    "load_json_limited",
                    wraps=load_json_limited,
                ) as loader,
            ):
                events = service.events("board", after=0)

        self.assertEqual(events[0]["kind"], "repair.plan_ready")
        loader.assert_called_once_with(event_path, 64 * 1024)

    def test_prepare_proposal_resolves_legacy_policy_helpers_late(self) -> None:
        service = SimpleNamespace()
        value = {
            "design_name": "Sensor Controller",
            "request_summary": "Create a small sensor controller",
            "layers": None,
            "board": {"width_mm": None, "height_mm": None},
            "requested_parts": ["SHT31"],
            "functions": ["sensor acquisition"],
            "assumptions": [],
            "power": {},
        }

        with (
            patch.object(
                application, "_initial_stackup_layers", return_value=4
            ) as stackup,
            patch.object(application, "_slug", return_value="patched-design") as slug,
        ):
            proposal, request = ApplicationService._prepare_proposal(
                service,
                "board-12345678",
                "2026-09-17T12:00:00Z",
                value,
                {},
                "Create a small sensor controller",
            )

        stackup.assert_called_once_with("Create a small sensor controller")
        slug.assert_called_once_with("Sensor Controller")
        self.assertEqual(proposal["decisions"]["layers"], 4)
        self.assertEqual(proposal["scope"]["decision"], "attempted")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.design_id, "patched-design-12345678")

    def test_proposal_message_projects_read_only_repair_view(self) -> None:
        proposal = {
            "scope": {"decision": "attempted"},
            "clarifications": [],
            "planning": {"state": "ready", "message": None},
            "brief": {"plan_review": {"summary": {"attention_required": 2}}},
        }

        message = ApplicationService._proposal_message(proposal)

        self.assertIn("2 deterministic preflight finding(s)", message)
        self.assertIn("generation remains available", message)


if __name__ == "__main__":
    unittest.main()
