"""Focused message-input helper extraction and compatibility coverage."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.services import application, application_message_inputs
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_message_inputs import ApplicationMessageInputMixin


class ApplicationMessageInputTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_pure_helpers(self) -> None:
        source = Path(application_message_inputs.__file__).read_text(encoding="utf-8")
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
            "_normalize_message_text",
            "_reply_delivery_binding",
            "_reply_already_delivered",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationMessageInputMixin, name),
                )

        for retained in (
            "record_progress",
            "reply_message",
            "send_message",
            "confirm_project",
            "_record_failure",
            "apply_modification",
            "verify_release",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, ApplicationService.__dict__)
                self.assertNotIn(retained, ApplicationMessageInputMixin.__dict__)

    def test_text_normalization_resolves_legacy_application_patch_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            with (
                patch.object(application, "MAX_USER_MESSAGE_BYTES", 37),
                patch.object(application, "_safe_text", return_value="bounded") as safe,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    return_value="sanitized",
                ) as sanitize,
            ):
                result = service._normalize_message_text("raw", "reply text")

        self.assertEqual(result, "sanitized")
        safe.assert_called_once_with("raw", "reply text", limit=37)
        sanitize.assert_called_once_with("bounded")

    def test_reply_binding_validation_and_projection_are_pure(self) -> None:
        class PatchedValidationError(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            with (
                patch.object(application, "ValidationError", PatchedValidationError),
                self.assertRaisesRegex(
                    PatchedValidationError,
                    "reply delivery binding is invalid",
                ),
            ):
                service._reply_delivery_binding("turn-one", None)

            binding = service._reply_delivery_binding("turn-one", 2)

        self.assertEqual(binding, {"turn_id": "turn-one", "index": 2})
        assert binding is not None
        conversation = {
            "messages": [
                {"data": None},
                {"data": {"turn_id": "turn-one", "index": 2}},
            ]
        }
        self.assertTrue(service._reply_already_delivered(conversation, binding))
        self.assertEqual(
            conversation,
            {
                "messages": [
                    {"data": None},
                    {"data": {"turn_id": "turn-one", "index": 2}},
                ]
            },
        )

    def test_reply_message_keeps_exactly_once_delivery_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            project_id = service.create_draft("Reply binding")["project"]["id"]

            first = service.reply_message(
                project_id,
                "Reply once",
                turn_id="turn-one",
                index=0,
            )
            conversation_path = service.project_root(project_id) / "conversation.json"
            before_retry = conversation_path.read_bytes()
            second = service.reply_message(
                project_id,
                "Different retry text",
                turn_id="turn-one",
                index=0,
            )
            after_retry = conversation_path.read_bytes()

        self.assertEqual(before_retry, after_retry)
        self.assertEqual(first["conversation"], second["conversation"])
        replies = first["conversation"]["messages"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["text"], "Reply once")
        self.assertEqual(
            replies[0]["data"],
            {"turn_id": "turn-one", "index": 0},
        )


if __name__ == "__main__":
    unittest.main()
