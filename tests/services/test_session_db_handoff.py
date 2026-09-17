"""Focused coverage for the SessionDB handoff state-machine boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_handoff


class SessionHandoffMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_handoff_api_without_reverse_import(self) -> None:
        mixin = session_db_handoff.SessionHandoffMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "request_handoff",
            "get_handoff_state",
            "list_pending_handoffs",
            "claim_handoff",
            "complete_handoff",
            "fail_handoff",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(Path(session_db_handoff.__file__).read_text(encoding="utf-8"))
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

    def test_request_claim_complete_and_rerequest_transitions(self) -> None:
        self.db.create_session("session-1", "cli")
        self.assertEqual(
            self.db.get_handoff_state("session-1"),
            {"state": None, "platform": None, "error": None},
        )
        self.assertFalse(self.db.request_handoff("missing", "telegram"))

        self.assertTrue(self.db.request_handoff("session-1", "telegram"))
        self.assertEqual(
            self.db.get_handoff_state("session-1"),
            {"state": "pending", "platform": "telegram", "error": None},
        )
        self.assertFalse(self.db.request_handoff("session-1", "discord"))
        self.assertTrue(self.db.claim_handoff("session-1"))
        self.assertFalse(self.db.claim_handoff("session-1"))
        self.assertFalse(self.db.request_handoff("session-1", "discord"))

        self.db.complete_handoff("session-1")
        self.assertEqual(
            self.db.get_handoff_state("session-1"),
            {"state": "completed", "platform": "telegram", "error": None},
        )
        self.assertTrue(self.db.request_handoff("session-1", "discord"))
        self.assertEqual(
            self.db.get_handoff_state("session-1"),
            {"state": "pending", "platform": "discord", "error": None},
        )

    def test_fail_records_bounded_error_and_allows_retry(self) -> None:
        self.db.create_session("session-1", "cli")
        self.assertTrue(self.db.request_handoff("session-1", "telegram"))
        self.assertTrue(self.db.claim_handoff("session-1"))

        error = "x" * 550
        self.db.fail_handoff("session-1", error)
        self.assertEqual(
            self.db.get_handoff_state("session-1"),
            {"state": "failed", "platform": "telegram", "error": "x" * 500},
        )

        self.assertTrue(self.db.request_handoff("session-1", "discord"))
        self.assertEqual(
            self.db.get_handoff_state("session-1"),
            {"state": "pending", "platform": "discord", "error": None},
        )

    def test_pending_list_is_oldest_first_and_shapes_system_prompt(self) -> None:
        self.db.create_session("newer", "cli", system_prompt="new prompt")
        self.db.create_session("older", "cli", system_prompt="old prompt")
        self.db.create_session("running", "cli", system_prompt="running prompt")
        self.db._execute_write(
            lambda conn: conn.executemany(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                ((20, "newer"), (10, "older"), (5, "running")),
            )
        )
        self.assertTrue(self.db.request_handoff("newer", "telegram"))
        self.assertTrue(self.db.request_handoff("older", "discord"))
        self.assertTrue(self.db.request_handoff("running", "slack"))
        self.assertTrue(self.db.claim_handoff("running"))

        rows = self.db.list_pending_handoffs()

        self.assertEqual([row["id"] for row in rows], ["older", "newer"])
        self.assertEqual(
            [row["system_prompt"] for row in rows], ["old prompt", "new prompt"]
        )

    def test_missing_terminal_updates_are_noops(self) -> None:
        self.db.complete_handoff("missing")
        self.db.fail_handoff("missing", "error")
        self.assertIsNone(self.db.get_handoff_state("missing"))
        self.assertFalse(self.db.claim_handoff("missing"))

    def test_read_failures_use_legacy_logger_path_and_fail_closed(self) -> None:
        real_connection = self.db._conn
        broken_connection = MagicMock()
        broken_connection.execute.side_effect = RuntimeError("read failed")
        self.db._conn = broken_connection
        try:
            with patch.object(session_db.logger, "debug") as log_debug:
                self.assertIsNone(self.db.get_handoff_state("session-1"))
                self.assertEqual(self.db.list_pending_handoffs(), [])
        finally:
            self.db._conn = real_connection

        self.assertEqual(
            log_debug.call_args_list,
            [
                call("Session handoff lookup failed", exc_info=True),
                call("Pending handoff listing failed", exc_info=True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
