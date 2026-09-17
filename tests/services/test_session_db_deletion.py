"""Focused coverage for the SessionDB deletion mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_deletion


class SessionDeletionMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.sessions_dir = self.home / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)

    def _write_session_files(self, session_id: str) -> list[Path]:
        files = [
            self.sessions_dir / f"{session_id}.json",
            self.sessions_dir / f"{session_id}.jsonl",
            self.sessions_dir / f"request_dump_{session_id}_1.json",
        ]
        for path in files:
            path.write_text(session_id, encoding="utf-8")
        return files

    def test_session_db_inherits_deletion_api_without_reverse_import(self) -> None:
        mixin = session_db_deletion.SessionDeletionMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "clear_messages",
            "_remove_session_files",
            "get_session_delete_targets",
            "delete_session",
            "delete_session_if_empty",
            "delete_sessions",
            "count_empty_sessions",
            "delete_empty_sessions",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(Path(session_db_deletion.__file__).read_text(encoding="utf-8"))
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

    def test_clear_messages_resets_transcript_counters(self) -> None:
        self.db.create_session("session", "cli")
        self.db.append_message("session", "user", "question")
        self.db.append_message(
            "session",
            "assistant",
            tool_calls=[{"id": "call-1", "type": "function"}],
        )
        self.assertEqual(self.db.get_session("session")["message_count"], 2)
        self.assertEqual(self.db.get_session("session")["tool_call_count"], 1)

        self.db.clear_messages("session")

        self.assertEqual(self.db.message_count("session"), 0)
        row = self.db.get_session("session")
        self.assertEqual(row["message_count"], 0)
        self.assertEqual(row["tool_call_count"], 0)

    def test_delete_session_cascades_delegates_and_orphans_branch(self) -> None:
        self.db.create_session("parent", "cli")
        self.db.create_session(
            "delegate",
            "cli",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        self.db.create_session(
            "nested",
            "cli",
            parent_session_id="delegate",
            model_config={"_delegate_from": "delegate"},
        )
        self.db.create_session(
            "branch",
            "cli",
            parent_session_id="parent",
            model_config={"_branched_from": "parent"},
        )
        for session_id in ("parent", "delegate", "nested", "branch"):
            self.db.append_message(session_id, "user", session_id)
            self._write_session_files(session_id)

        collect = session_db._collect_delegate_child_ids
        delete_delegates = session_db._delete_delegate_children
        with (
            patch.object(
                session_db, "_collect_delegate_child_ids", wraps=collect
            ) as collect_children,
            patch.object(
                session_db, "_delete_delegate_children", wraps=delete_delegates
            ) as delete_children,
        ):
            targets = self.db.get_session_delete_targets("parent")
            self.assertEqual(targets, ["parent", "delegate", "nested"])
            self.assertTrue(
                self.db.delete_session(
                    "parent",
                    sessions_dir=self.sessions_dir,
                    expected_delete_ids=targets,
                )
            )

        self.assertGreaterEqual(collect_children.call_count, 2)
        delete_children.assert_called_once()
        for session_id in ("parent", "delegate", "nested"):
            self.assertIsNone(self.db.get_session(session_id))
            self.assertFalse(
                any(path.exists() for path in self._write_paths(session_id))
            )
        branch = self.db.get_session("branch")
        self.assertIsNone(branch["parent_session_id"])
        self.assertTrue(all(path.exists() for path in self._write_paths("branch")))

    def _write_paths(self, session_id: str) -> list[Path]:
        return [
            self.sessions_dir / f"{session_id}.json",
            self.sessions_dir / f"{session_id}.jsonl",
            self.sessions_dir / f"request_dump_{session_id}_1.json",
        ]

    def test_expected_delete_ids_fail_closed_when_delegate_set_changes(self) -> None:
        self.db.create_session("parent", "cli")
        expected = self.db.get_session_delete_targets("parent")
        self.db.create_session(
            "late-delegate",
            "cli",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )

        self.assertFalse(self.db.delete_session("parent", expected_delete_ids=expected))
        self.assertIsNotNone(self.db.get_session("parent"))
        self.assertIsNotNone(self.db.get_session("late-delegate"))

    def test_delete_session_if_empty_preserves_titled_message_and_child_rows(
        self,
    ) -> None:
        self.db.create_session("empty", "cli")
        empty_files = self._write_session_files("empty")
        self.db.create_session("titled", "cli")
        self.db.set_session_title("titled", "Keep")
        self.db.create_session("with-message", "cli")
        self.db.append_message("with-message", "user", "keep")
        self.db.create_session("with-child", "cli")
        self.db.create_session("child", "cli", parent_session_id="with-child")

        self.assertTrue(
            self.db.delete_session_if_empty("empty", sessions_dir=self.sessions_dir)
        )
        self.assertTrue(all(not path.exists() for path in empty_files))
        for session_id in ("titled", "with-message", "with-child"):
            with self.subTest(session_id=session_id):
                self.assertFalse(self.db.delete_session_if_empty(session_id))
                self.assertIsNotNone(self.db.get_session(session_id))

    def test_bulk_and_empty_deletion_preserve_non_targets(self) -> None:
        self.db.create_session("bulk-parent", "cli")
        self.db.create_session(
            "bulk-delegate",
            "cli",
            parent_session_id="bulk-parent",
            model_config={"_delegate_from": "bulk-parent"},
        )
        self.db.create_session(
            "bulk-branch",
            "cli",
            parent_session_id="bulk-parent",
            model_config={"_branched_from": "bulk-parent"},
        )
        self.db.create_session("bulk-other", "cli")
        for session_id in ("bulk-parent", "bulk-delegate", "bulk-other"):
            self._write_session_files(session_id)

        self.assertEqual(
            self.db.delete_sessions(
                ["bulk-parent", "bulk-other", "bulk-other", "missing"],
                sessions_dir=self.sessions_dir,
            ),
            2,
        )
        self.assertIsNone(self.db.get_session("bulk-parent"))
        self.assertIsNone(self.db.get_session("bulk-delegate"))
        self.assertIsNone(self.db.get_session("bulk-other"))
        self.assertIsNone(self.db.get_session("bulk-branch")["parent_session_id"])

        self.db.create_session("ended-empty", "cli")
        empty_files = self._write_session_files("ended-empty")
        self.db.end_session("ended-empty", "complete")
        self.db.create_session("live-empty", "cli")
        self.db.create_session("archived-empty", "cli")
        self.db.end_session("archived-empty", "complete")
        self.db.set_session_archived("archived-empty", True)
        self.db.create_session("ended-nonempty", "cli")
        self.db.append_message("ended-nonempty", "user", "keep")
        self.db.end_session("ended-nonempty", "complete")

        self.assertEqual(self.db.count_empty_sessions(), 1)
        self.assertEqual(
            self.db.delete_empty_sessions(sessions_dir=self.sessions_dir), 1
        )
        self.assertTrue(all(not path.exists() for path in empty_files))
        self.assertIsNotNone(self.db.get_session("live-empty"))
        self.assertIsNotNone(self.db.get_session("archived-empty"))
        self.assertIsNotNone(self.db.get_session("ended-nonempty"))


if __name__ == "__main__":
    unittest.main()
