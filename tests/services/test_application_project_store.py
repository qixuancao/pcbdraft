"""Focused project-record storage and compatibility coverage."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.model.providers import IntentProvider
from pcbdraft.services import application, application_project_store


class ApplicationProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.workspace = Path(temporary)
        provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
        self.service = application.ApplicationService(
            self.workspace,
            provider=provider,
            recover_interrupted=False,
        )

    def test_application_inherits_store_api_and_keeps_compatibility_exports(
        self,
    ) -> None:
        moved_methods = (
            "_project_path",
            "_open",
            "_open_path",
            "_validate_state",
            "_validate_conversation",
            "_append_message",
            "_event",
            "_write_records",
            "_summary",
            "_attempt_records",
            "_valid_attempt_record",
            "_public_project",
        )
        self.assertTrue(
            issubclass(
                application.ApplicationService,
                application_project_store.ApplicationProjectStoreMixin,
            )
        )
        for name in moved_methods:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(application.ApplicationService, name),
                    inspect.getattr_static(
                        application_project_store.ApplicationProjectStoreMixin, name
                    ),
                )
        self.assertIs(
            application.ApplicationProject,
            application_project_store.ApplicationProject,
        )
        for name in (
            "APP_FILE_LIMIT",
            "APP_PROJECT_SCHEMA",
            "APP_PROJECT_VERSION",
            "ATTEMPT_SCHEMA",
            "ATTEMPT_VERSION",
            "CONVERSATION_SCHEMA",
            "CONVERSATION_VERSION",
            "MAX_MESSAGES",
            "_ATTEMPT_FIELDS",
            "_CONVERSATION_FIELDS",
            "_PROJECT_ID",
            "_STATE_FIELDS",
            "_public_readiness_record",
        ):
            with self.subTest(export=name):
                self.assertIs(
                    getattr(application, name), getattr(application_project_store, name)
                )

        for retained in (
            "_record_failure",
            "_recover_interrupted_projects",
            "_interrupt_running_attempts",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, application.ApplicationService.__dict__)
                self.assertNotIn(
                    retained,
                    application_project_store.ApplicationProjectStoreMixin.__dict__,
                )

        tree = ast.parse(
            Path(application_project_store.__file__).read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.services.application", imports)

    def test_project_records_round_trip_with_event_and_attempt_projection(self) -> None:
        draft = self.service.create_draft("Storage helpers")
        project_id = str(draft["project"]["id"])
        project = self.service._open(project_id)

        self.service._append_message(
            project.conversation,
            "assistant",
            "status",
            "Project records were updated.",
            data={"source": "test"},
        )
        project.state["last_validation"] = {"production_ready": True}
        self.service._event(
            project.state,
            project.root,
            "project.tested",
            "Stored one event",
        )
        self.service._write_records(project.root, project.state, project.conversation)

        attempt_id = "20260917T120000Z-1234abcd"
        attempt_root = project.root / "attempts" / attempt_id
        attempt_root.mkdir()
        atomic_write_json(
            attempt_root / "attempt.json",
            {
                "schema": application.ATTEMPT_SCHEMA,
                "version": application.ATTEMPT_VERSION,
                "id": attempt_id,
                "status": "completed",
                "phase": "completed",
                "runtime": "agent_plan_v1",
                "assurance": "provisional",
                "started_at": "2026-09-17T12:00:00Z",
                "completed_at": "2026-09-17T12:00:01Z",
                "part_ids": [],
                "requested_parts": [],
                "files": {
                    "request": None,
                    "plan": None,
                    "semantic_ir": None,
                    "part_catalog": None,
                    "retained_native": None,
                },
                "error": None,
            },
        )

        reopened = self.service._open(project_id)
        self.assertIsInstance(reopened, application.ApplicationProject)
        self.assertEqual(
            reopened.conversation["messages"][-1]["text"],
            "Project records were updated.",
        )
        self.assertEqual(self.service.events(project_id)[0]["kind"], "project.tested")

        public = self.service.open_project(project_id)
        self.assertEqual(public["attempts"][0]["id"], attempt_id)
        self.assertTrue(
            public["state"]["last_validation"]["production_evidence_complete"]
        )
        self.assertFalse(public["state"]["last_validation"]["production_ready"])

    def test_paths_fail_closed_and_managed_project_patch_path_is_preserved(
        self,
    ) -> None:
        draft = self.service.create_draft("Patch boundary")
        project_id = str(draft["project"]["id"])
        project = self.service._open(project_id)
        project.design_root.mkdir()
        managed = SimpleNamespace(
            root=project.design_root,
            design=SimpleNamespace(
                design_id=project_id,
                name="Patch boundary",
                content_hash=lambda: "content-hash",
            ),
            manifest={"files": {"board": "board.kicad_pcb"}},
            drift=tuple,
        )
        with patch.object(
            application, "open_managed_project", return_value=managed
        ) as opened:
            public = self.service.open_project(project_id)
        opened.assert_called_once_with(project.design_root)
        self.assertEqual(public["design"]["content_hash"], "content-hash")

        outside = self.workspace / "outside"
        outside.mkdir()
        (self.service.projects_root / "escape-id").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(ValidationError, "path is unsafe"):
            self.service._project_path("escape-id")

    def test_append_and_event_use_legacy_application_sanitizer_patch(self) -> None:
        draft = self.service.create_draft("Sanitizer patch")
        project_id = str(draft["project"]["id"])
        project = self.service._open(project_id)

        with patch.object(
            application,
            "_sanitize_secret_text",
            side_effect=lambda value: f"patched:{value}",
        ) as sanitize:
            self.service._append_message(
                project.conversation, "assistant", "status", "message"
            )
            self.service._event(project.state, project.root, "project.tested", "event")

        self.assertEqual(
            project.conversation["messages"][-1]["text"], "patched:message"
        )
        event = self.service.events(project_id)[0]
        self.assertEqual(event["message"], "patched:event")
        self.assertEqual(
            [call.args[0] for call in sanitize.call_args_list],
            ["message", "event"],
        )


if __name__ == "__main__":
    unittest.main()
