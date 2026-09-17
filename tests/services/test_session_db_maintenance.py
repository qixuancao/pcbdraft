"""Focused coverage for the SessionDB maintenance mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_maintenance


class SessionMaintenanceMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_maintenance_api_without_reverse_import(self) -> None:
        mixin = session_db_maintenance.SessionMaintenanceMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "logical_size_bytes",
            "vacuum",
            "maybe_auto_prune_and_vacuum",
            "maybe_auto_archive",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_maintenance.__file__).read_text(encoding="utf-8")
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

    def test_logical_size_uses_sqlite_pages_and_legacy_logger_path(self) -> None:
        page_count = self.db._conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db._conn.execute("PRAGMA page_size").fetchone()[0]
        self.assertEqual(self.db.logical_size_bytes(), page_count * page_size)

        real_connection = self.db._conn
        broken_connection = MagicMock()
        broken_connection.execute.side_effect = RuntimeError("pragma failed")
        self.db._conn = broken_connection
        try:
            with patch.object(session_db.logger, "debug") as log_debug:
                self.assertIsNone(self.db.logical_size_bytes())
            log_debug.assert_called_once_with(
                "Could not read logical DB size", exc_info=True
            )
        finally:
            self.db._conn = real_connection

    def test_vacuum_optimizes_fts_and_runs_both_checkpoints(self) -> None:
        statements: list[str] = []
        self.db._conn.set_trace_callback(statements.append)
        self.addCleanup(self.db._conn.set_trace_callback, None)

        with patch.object(self.db, "optimize_fts", return_value=2) as optimize:
            self.assertEqual(self.db.vacuum(), 2)

        optimize.assert_called_once_with()
        normalized = {statement.strip().upper() for statement in statements}
        self.assertIn("PRAGMA WAL_CHECKPOINT(PASSIVE)", normalized)
        self.assertIn("VACUUM", normalized)
        self.assertIn("PRAGMA WAL_CHECKPOINT(TRUNCATE)", normalized)

    def test_vacuum_continues_when_fts_optimize_fails(self) -> None:
        with (
            patch.object(
                self.db, "optimize_fts", side_effect=RuntimeError("fts failed")
            ),
            patch.object(session_db.logger, "warning") as log_warning,
        ):
            self.assertEqual(self.db.vacuum(), 0)

        log_warning.assert_called_once_with(
            "FTS optimize before VACUUM failed", exc_info=True
        )

    def test_auto_prune_vacuums_records_meta_and_skips_recent_run(self) -> None:
        sessions_dir = self.home / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        with (
            patch.object(session_db.time, "time", return_value=100_000.0),
            patch.object(self.db, "prune_sessions", return_value=3) as prune,
            patch.object(self.db, "vacuum", return_value=1) as vacuum,
            patch.object(session_db.logger, "info") as log_info,
        ):
            first = self.db.maybe_auto_prune_and_vacuum(
                retention_days=7,
                min_interval_hours=24,
                sessions_dir=sessions_dir,
                min_vacuum_interval_days=30,
            )
            second = self.db.maybe_auto_prune_and_vacuum(
                retention_days=7,
                min_interval_hours=24,
                sessions_dir=sessions_dir,
                min_vacuum_interval_days=30,
            )

        self.assertEqual(first, {"skipped": False, "pruned": 3, "vacuumed": True})
        self.assertEqual(second, {"skipped": True, "pruned": 0, "vacuumed": False})
        prune.assert_called_once_with(older_than_days=7, sessions_dir=sessions_dir)
        vacuum.assert_called_once_with()
        self.assertEqual(self.db.get_meta("last_vacuum"), "100000.0")
        self.assertEqual(self.db.get_meta("last_auto_prune"), "100000.0")
        log_info.assert_called_once()

    def test_auto_prune_respects_independent_vacuum_throttle(self) -> None:
        self.db.set_meta("last_vacuum", "199999.0")
        with (
            patch.object(session_db.time, "time", return_value=200_000.0),
            patch.object(self.db, "prune_sessions", return_value=1),
            patch.object(self.db, "vacuum") as vacuum,
        ):
            result = self.db.maybe_auto_prune_and_vacuum(
                min_interval_hours=0,
                min_vacuum_interval_days=30,
            )

        self.assertEqual(result, {"skipped": False, "pruned": 1, "vacuumed": False})
        vacuum.assert_not_called()
        self.assertEqual(self.db.get_meta("last_auto_prune"), "200000.0")

    def test_auto_archive_records_meta_and_skips_recent_run(self) -> None:
        with (
            patch.object(session_db.time, "time", return_value=300_000.0),
            patch.object(self.db, "archive_stale_sessions", return_value=2) as archive,
            patch.object(session_db.logger, "info") as log_info,
        ):
            first = self.db.maybe_auto_archive(
                idle_days=5,
                min_interval_hours=24,
                exclude_pinned=False,
            )
            second = self.db.maybe_auto_archive(
                idle_days=5,
                min_interval_hours=24,
                exclude_pinned=False,
            )

        self.assertEqual(first, {"skipped": False, "archived": 2})
        self.assertEqual(second, {"skipped": True, "archived": 0})
        archive.assert_called_once_with(5, exclude_pinned=False)
        self.assertEqual(self.db.get_meta("last_auto_archive"), "300000.0")
        log_info.assert_called_once()

    def test_automatic_maintenance_returns_errors_without_raising(self) -> None:
        with (
            patch.object(session_db.time, "time", return_value=400_000.0),
            patch.object(
                self.db, "prune_sessions", side_effect=RuntimeError("prune failed")
            ),
            patch.object(session_db.logger, "warning") as log_warning,
        ):
            prune_result = self.db.maybe_auto_prune_and_vacuum()
        self.assertEqual(prune_result["error"], "prune failed")
        log_warning.assert_called_once_with(
            "state.db auto-maintenance failed", exc_info=True
        )

        with (
            patch.object(session_db.time, "time", return_value=500_000.0),
            patch.object(
                self.db,
                "archive_stale_sessions",
                side_effect=RuntimeError("archive failed"),
            ),
            patch.object(session_db.logger, "warning") as log_warning,
        ):
            archive_result = self.db.maybe_auto_archive()
        self.assertEqual(archive_result["error"], "archive failed")
        log_warning.assert_called_once_with(
            "state.db auto-archive failed", exc_info=True
        )


if __name__ == "__main__":
    unittest.main()
