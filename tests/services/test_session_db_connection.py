"""Focused coverage for the SessionDB connection lifecycle boundary."""

from __future__ import annotations

import ast
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_connection


class SessionConnectionMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)

    def test_session_db_inherits_connection_api_without_reverse_import(self) -> None:
        mixin = session_db_connection.SessionConnectionMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "__init__",
            "_close_connection_quietly",
            "_get_read_conn",
            "_discard_partial_read_conn",
            "_close_read_conn",
            "_checkout_read_conn",
            "_read_ctx",
            "_execute_write",
            "_sleep_before_write_retry",
            "_reconnect_after_notadb",
            "_try_wal_checkpoint",
            "__enter__",
            "__exit__",
            "close",
            "__del__",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_connection.__file__).read_text(encoding="utf-8")
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
        self.assertIs(session_db.queue, session_db_connection.queue)

    def test_real_database_transaction_and_close_lifecycle(self) -> None:
        db_path = self.home / "state.db"
        db = session_db.SessionDB(db_path)
        self.addCleanup(db.close)

        db.create_session("session-1", "cli")
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?)",
                ("connection-test", "committed"),
            )
        )
        self.assertEqual(db.get_meta("connection-test"), "committed")

        def fail_after_write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?)",
                ("connection-rollback", "discarded"),
            )
            raise RuntimeError("rollback probe")

        with self.assertRaisesRegex(RuntimeError, "rollback probe"):
            db._execute_write(fail_after_write)
        self.assertIsNone(db.get_meta("connection-rollback"))
        self.assertEqual(db._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

        db.close()
        self.assertIsNone(db._conn)
        db.close()

    def test_real_read_pool_connection_is_reused_and_drained(self) -> None:
        db = session_db.SessionDB(self.home / "read-pool.db")
        self.addCleanup(db.close)
        db.create_session("session-1", "cli")
        db._wal_active = True

        with db._read_ctx() as read_conn:
            self.assertIsNot(read_conn, db._conn)
            self.assertEqual(
                read_conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", ("session-1",)
                ).fetchone()[0],
                "session-1",
            )

        self.assertEqual(db._read_pool.qsize(), 1)
        db.close()
        self.assertEqual(db._read_pool.qsize(), 0)

    def test_legacy_connection_and_pragma_patch_paths_remain_live(self) -> None:
        real_connect = session_db._connect_tracked_db
        real_pragmas = session_db.apply_database_pragmas
        with (
            patch.object(
                session_db,
                "_connect_tracked_db",
                wraps=real_connect,
            ) as connect,
            patch.object(
                session_db,
                "apply_database_pragmas",
                wraps=real_pragmas,
            ) as pragmas,
            patch.object(session_db, "_READ_POOL_MAX", 2),
            session_db.SessionDB(self.home / "patched.db") as db,
        ):
            self.assertEqual(db._read_pool.maxsize, 2)
            self.assertTrue(connect.called)
            self.assertTrue(pragmas.called)

    def test_legacy_timing_patch_controls_retry_policy(self) -> None:
        db = self.enterContext(session_db.SessionDB(self.home / "timing.db"))
        random_source = MagicMock()
        random_source.uniform.return_value = 0.025
        with (
            patch.object(session_db.time, "monotonic", return_value=1.0),
            patch.object(session_db.time, "sleep") as sleep,
            patch.object(session_db.random, "SystemRandom", return_value=random_source),
        ):
            self.assertTrue(db._sleep_before_write_retry(deadline=2.0, patience_s=2.0))

        random_source.uniform.assert_called_once_with(
            db._WRITE_RETRY_MIN_S,
            db._WRITE_RETRY_MAX_S,
        )
        sleep.assert_called_once_with(0.025)

    def test_init_failure_uses_legacy_error_reporting_path(self) -> None:
        failure = sqlite3.OperationalError("preflight refused")
        with (
            patch.object(
                session_db,
                "preflight_db_writability",
                side_effect=failure,
            ),
            patch.object(session_db, "_set_last_init_error") as set_error,
            self.assertRaisesRegex(sqlite3.OperationalError, "preflight refused"),
        ):
            session_db.SessionDB(self.home / "broken.db")

        set_error.assert_called_once_with("OperationalError: preflight refused")


if __name__ == "__main__":
    unittest.main()
