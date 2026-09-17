"""Focused coverage for the SessionDB compression-health mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_compression_health


class SessionCompressionHealthMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session("session-1", "cli")

    def test_session_db_inherits_health_api_without_reverse_import(self) -> None:
        method_names = (
            "record_compression_failure_cooldown",
            "get_compression_failure_cooldown",
            "get_compression_failure_cooldown_row",
            "restore_compression_failure_cooldown_row",
            "clear_compression_failure_cooldown",
            "get_compression_fallback_streak",
            "set_compression_fallback_streak",
            "increment_hygiene_failure_streak",
            "reset_hygiene_failure_streak",
            "get_compression_ineffective_count",
            "set_compression_ineffective_count",
        )
        mixin = session_db_compression_health.SessionCompressionHealthMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        for host_method in (
            "_execute_write",
            "try_acquire_compression_lock",
            "acquire_session_turn_lease",
            "record_gateway_session_peer",
        ):
            self.assertNotIn(host_method, mixin.__dict__)

        tree = ast.parse(
            Path(session_db_compression_health.__file__).read_text(encoding="utf-8")
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

    def test_cooldown_active_filter_snapshot_clear_and_restore(self) -> None:
        self.db.record_compression_failure_cooldown("session-1", 150.0, "busy")
        with patch.object(session_db.time, "time", return_value=100.0) as now:
            self.assertEqual(
                self.db.get_compression_failure_cooldown("session-1"),
                {
                    "cooldown_until": 150.0,
                    "remaining_seconds": 50.0,
                    "error": "busy",
                },
            )
        now.assert_called_once_with()

        snapshot = self.db.get_compression_failure_cooldown_row("session-1")
        self.assertEqual(
            snapshot,
            {
                "session_exists": True,
                "cooldown_until": 150.0,
                "error": "busy",
            },
        )
        with patch.object(session_db.time, "time", return_value=200.0):
            self.assertIsNone(self.db.get_compression_failure_cooldown("session-1"))

        self.db.clear_compression_failure_cooldown("session-1")
        self.assertEqual(
            self.db.get_compression_failure_cooldown_row("session-1"),
            {
                "session_exists": True,
                "cooldown_until": None,
                "error": None,
            },
        )
        self.db.restore_compression_failure_cooldown_row("session-1", snapshot)
        self.assertEqual(
            self.db.get_compression_failure_cooldown_row("session-1"), snapshot
        )

        absent = self.db.get_compression_failure_cooldown_row("missing")
        self.assertEqual(
            absent,
            {"session_exists": False, "cooldown_until": None, "error": None},
        )
        self.db.restore_compression_failure_cooldown_row("missing", absent)
        with self.assertRaisesRegex(RuntimeError, "rollback session missing"):
            self.db.restore_compression_failure_cooldown_row(
                "missing",
                {"session_exists": True, "cooldown_until": 250.0, "error": "lost"},
            )

    def test_fallback_hygiene_and_ineffective_counters_persist(self) -> None:
        other_db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.assertEqual(self.db.get_compression_fallback_streak("session-1"), 0)
        self.db.set_compression_fallback_streak("session-1", -3)
        self.assertEqual(other_db.get_compression_fallback_streak("session-1"), 0)
        self.db.set_compression_fallback_streak("session-1", 4)
        self.assertEqual(other_db.get_compression_fallback_streak("session-1"), 4)

        self.assertEqual(self.db.get_compression_ineffective_count("session-1"), 0)
        self.db.set_compression_ineffective_count("session-1", -2)
        self.assertEqual(other_db.get_compression_ineffective_count("session-1"), 0)
        self.db.set_compression_ineffective_count("session-1", 3)
        self.assertEqual(other_db.get_compression_ineffective_count("session-1"), 3)

        self.assertEqual(self.db.increment_hygiene_failure_streak("peer-key"), 1)
        self.assertEqual(other_db.increment_hygiene_failure_streak("peer-key"), 2)
        other_db.reset_hygiene_failure_streak("peer-key")
        self.assertEqual(self.db.increment_hygiene_failure_streak("peer-key"), 1)
        self.assertEqual(self.db.increment_hygiene_failure_streak(""), 1)

    def test_legacy_sqlite_and_logger_patch_paths_remain_live(self) -> None:
        with (
            patch.object(session_db.sqlite3, "Error", RuntimeError),
            patch.object(
                self.db,
                "_execute_write",
                side_effect=RuntimeError("write failed"),
            ),
            patch.object(session_db.logger, "warning") as log_warning,
        ):
            self.db.record_compression_failure_cooldown("session-1", 150.0, "busy")

        log_warning.assert_called_once()
        self.assertEqual(
            log_warning.call_args.args[:2],
            ("record_compression_failure_cooldown(%s) failed: %s", "session-1"),
        )
        self.assertIsInstance(log_warning.call_args.args[2], RuntimeError)

        # The moved readers resolve the legacy sqlite3.Row symbol at call time.
        # A false classification remains compatible because SQLite rows also
        # support positional access.
        with patch.object(session_db.sqlite3, "Row", tuple):
            self.assertEqual(
                self.db.get_compression_failure_cooldown_row("session-1"),
                {
                    "session_exists": True,
                    "cooldown_until": None,
                    "error": None,
                },
            )


if __name__ == "__main__":
    unittest.main()
