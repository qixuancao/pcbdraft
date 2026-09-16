"""Focused coverage for the SessionDB presentation-state mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_presentation


class SessionPresentationStateMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_presentation_api_without_reverse_import(self) -> None:
        method_names = (
            "_title_rank",
            "sanitize_title",
            "_is_compression_ancestor",
            "_set_session_title",
            "set_session_title",
            "set_auto_title",
            "set_auto_title_if_empty",
            "get_session_title",
            "get_session_title_source",
            "set_session_title_source",
            "set_session_archived",
            "set_session_pinned",
            "set_session_hidden",
            "set_session_read",
            "session_unread",
            "get_session_by_title",
            "resolve_session_by_title",
            "get_next_title_in_lineage",
            "get_compression_tip",
        )
        mixin = session_db_presentation.SessionPresentationStateMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_presentation.__file__).read_text(encoding="utf-8")
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
        self.assertNotIn("pcbdraft.services.session_db", imports)

    def test_title_sanitizing_precedence_and_legacy_patch_path(self) -> None:
        self.db.create_session("session-1", "cli")
        self.assertEqual(
            session_db.SessionDB.sanitize_title("  hello\t world\u200b  "),
            "hello world",
        )
        with patch.object(
            session_db, "_sanitize_surrogates", return_value="patched title"
        ) as sanitizer:
            self.assertEqual(
                session_db.SessionDB.sanitize_title("ignored"), "patched title"
            )
        sanitizer.assert_called_once_with("ignored")
        with patch.object(session_db.SessionDB, "MAX_TITLE_LENGTH", 3):
            with self.assertRaisesRegex(ValueError, "Title too long"):
                session_db.SessionDB.sanitize_title("four")

        self.assertTrue(
            self.db.set_auto_title(
                "session-1",
                "Derived",
                source=session_db.SessionDB.TITLE_SOURCE_DERIVED,
            )
        )
        self.assertTrue(
            self.db.set_auto_title(
                "session-1", "Generated", source=session_db.SessionDB.TITLE_SOURCE_LLM
            )
        )
        self.assertFalse(
            self.db.set_auto_title(
                "session-1",
                "Too late",
                source=session_db.SessionDB.TITLE_SOURCE_DERIVED,
            )
        )
        self.assertTrue(self.db.set_session_title("session-1", "User title"))
        self.assertFalse(
            self.db.set_auto_title_if_empty("session-1", "Must not overwrite")
        )
        self.assertEqual(self.db.get_session_title("session-1"), "User title")
        self.assertEqual(
            self.db.get_session_title_source("session-1"),
            session_db.SessionDB.TITLE_SOURCE_USER,
        )
        self.assertTrue(
            self.db.set_session_title_source(
                "session-1", session_db.SessionDB.TITLE_SOURCE_LLM
            )
        )
        self.assertEqual(
            self.db.get_session_title_source("session-1"),
            session_db.SessionDB.TITLE_SOURCE_LLM,
        )
        with self.assertRaisesRegex(ValueError, "invalid title source"):
            self.db.set_session_title_source("session-1", "external")

    def test_compression_title_transfer_and_title_resolution(self) -> None:
        self.db.create_session("root", "cli")
        self.assertTrue(self.db.set_session_title("root", "Board"))
        self.db.end_session("root", "compression")
        self.db.create_session("tip", "cli", parent_session_id="root")

        self.assertTrue(self.db.set_session_title("tip", "Board"))
        self.assertIsNone(self.db.get_session_title("root"))
        self.assertEqual(self.db.get_session_by_title("Board")["id"], "tip")
        self.assertEqual(self.db.get_compression_tip("root"), "tip")

        self.db.create_session("numbered", "cli")
        self.assertTrue(self.db.set_session_title("numbered", "Board #2"))
        self.assertEqual(self.db.resolve_session_by_title("Board"), "numbered")
        self.assertEqual(self.db.get_next_title_in_lineage("Board"), "Board #3")

        self.db.create_session(
            "branch",
            "cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )
        self.db.create_session("tool-child", "tool", parent_session_id="root")
        self.assertEqual(self.db.get_compression_tip("root"), "tip")
        self.assertEqual(self.db.resolve_resume_session_id("root"), "tip")

    def test_lineage_visibility_and_read_state_update_as_one_unit(self) -> None:
        self.db.create_session("root", "cli")
        self.db.end_session("root", "compression")
        self.db.create_session("tip", "cli", parent_session_id="root")

        self.assertTrue(self.db.set_session_archived("tip", True))
        self.assertTrue(self.db.set_session_pinned("root", True))
        self.assertTrue(self.db.set_session_hidden("tip", True))
        with patch.object(session_db.time, "time", return_value=123.5):
            self.assertTrue(self.db.set_session_read("root", True))

        for session_id in ("root", "tip"):
            row = self.db.get_session(session_id)
            with self.subTest(session_id=session_id):
                self.assertEqual(row["archived"], 1)
                self.assertEqual(row["pinned"], 1)
                self.assertEqual(row["hidden"], 1)
                self.assertEqual(row["last_read_at"], 123.5)

        self.assertFalse(
            session_db.SessionDB.session_unread(
                {"last_read_at": None, "last_active": 200.0}
            )
        )
        self.assertTrue(
            session_db.SessionDB.session_unread(
                {"last_read_at": 100.0, "last_active": 101.0}
            )
        )
        self.assertFalse(
            session_db.SessionDB.session_unread(
                {"last_read_at": 101.0, "last_active": 100.0}
            )
        )
        self.assertTrue(self.db.set_session_read("tip", False))
        self.assertEqual(self.db.get_session("root")["last_read_at"], 0.0)
        self.assertEqual(self.db.get_session("tip")["last_read_at"], 0.0)


if __name__ == "__main__":
    unittest.main()
