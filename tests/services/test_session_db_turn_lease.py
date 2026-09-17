"""Focused coverage for the SessionDB session-turn lease mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_turn_lease


class SessionTurnLeaseMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session("root", "cli")

    def _create_compression_child(self) -> None:
        self.db.end_session("root", "compression")
        self.db.create_session("child", "cli", parent_session_id="root")

    def test_session_db_inherits_turn_lease_api_without_reverse_import(self) -> None:
        method_names = (
            "_session_turn_lease_key_on_conn",
            "_session_turn_lease_key",
            "try_acquire_session_turn_lease",
            "acquire_session_turn_lease",
            "refresh_session_turn_lease",
            "release_session_turn_lease",
        )
        mixin = session_db_turn_lease.SessionTurnLeaseMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        for host_method in (
            "_execute_write",
            "close",
            "try_acquire_compression_lock",
            "create_session",
        ):
            self.assertNotIn(host_method, mixin.__dict__)

        tree = ast.parse(
            Path(session_db_turn_lease.__file__).read_text(encoding="utf-8")
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

    def test_lease_key_follows_compression_but_stops_at_explicit_fork(self) -> None:
        self._create_compression_child()
        self.db.create_session(
            "branch",
            "cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )

        self.assertEqual(self.db._session_turn_lease_key("root"), "root")
        self.assertEqual(self.db._session_turn_lease_key("child"), "root")
        self.assertEqual(self.db._session_turn_lease_key("branch"), "branch")

    def test_real_cross_connection_acquire_refresh_and_release(self) -> None:
        self._create_compression_child()
        other_db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

        with patch.object(session_db.time, "time", return_value=100.0):
            self.assertTrue(
                self.db.try_acquire_session_turn_lease(
                    "child", "owner-a", ttl_seconds=10.0, patience_s=0.1
                )
            )
            self.assertFalse(
                other_db.try_acquire_session_turn_lease(
                    "root", "owner-b", ttl_seconds=10.0, patience_s=0.1
                )
            )
        row = self.db._conn.execute(
            "SELECT conversation_id, holder, expires_at FROM session_turn_leases"
        ).fetchone()
        self.assertEqual(
            (row["conversation_id"], row["holder"], row["expires_at"]),
            ("root", "owner-a", 110.0),
        )

        with patch.object(session_db.time, "time", return_value=105.0):
            self.assertTrue(
                other_db.refresh_session_turn_lease(
                    "child", "owner-a", ttl_seconds=10.0
                )
            )
        self.assertEqual(
            self.db._conn.execute(
                "SELECT expires_at FROM session_turn_leases "
                "WHERE conversation_id = 'root'"
            ).fetchone()["expires_at"],
            115.0,
        )
        other_db.release_session_turn_lease("root", "owner-b")
        self.assertEqual(
            self.db._conn.execute(
                "SELECT holder FROM session_turn_leases WHERE conversation_id = 'root'"
            ).fetchone()["holder"],
            "owner-a",
        )
        self.db.release_session_turn_lease("child", "owner-a")
        self.assertIsNone(
            self.db._conn.execute(
                "SELECT holder FROM session_turn_leases WHERE conversation_id = 'root'"
            ).fetchone()
        )

    def test_legacy_process_probe_patch_reclaims_live_lease(self) -> None:
        self.assertTrue(
            self.db.try_acquire_session_turn_lease("root", "owner-a", ttl_seconds=600.0)
        )
        with patch.object(
            session_db,
            "_compression_lock_holder_process_is_dead",
            return_value=True,
        ) as process_is_dead:
            self.assertTrue(
                self.db.try_acquire_session_turn_lease(
                    "root", "owner-b", ttl_seconds=600.0
                )
            )

        process_is_dead.assert_called_once_with("owner-a")
        row = self.db._conn.execute(
            "SELECT holder FROM session_turn_leases WHERE conversation_id = 'root'"
        ).fetchone()
        self.assertEqual(row["holder"], "owner-b")

    def test_wait_uses_legacy_clock_sleep_and_logger_patch_paths(self) -> None:
        on_wait = Mock()
        should_abort = Mock(side_effect=[RuntimeError("probe failed"), False])
        with (
            patch.object(session_db.time, "monotonic", side_effect=[0.0, 0.0]),
            patch.object(session_db.time, "sleep") as sleep,
            patch.object(session_db.logger, "debug") as log_debug,
            patch.object(
                self.db,
                "try_acquire_session_turn_lease",
                side_effect=[False, True],
            ),
        ):
            acquired = self.db.acquire_session_turn_lease(
                "root",
                "owner-a",
                wait_seconds=5.0,
                poll_interval_seconds=0.2,
                on_wait=on_wait,
                should_abort=should_abort,
            )

        self.assertTrue(acquired)
        on_wait.assert_called_once_with(0.0)
        sleep.assert_called_once_with(0.2)
        log_debug.assert_called_once_with(
            "session turn lease should_abort callback failed", exc_info=True
        )

    def test_wait_uses_legacy_sqlite_and_error_classifier_patch_paths(self) -> None:
        with (
            patch.object(session_db.sqlite3, "Error", RuntimeError),
            patch.object(
                session_db,
                "classify_persistence_error",
                return_value="locked",
            ) as classify,
            patch.object(session_db.time, "monotonic", side_effect=[10.0, 10.0]),
            patch.object(
                self.db,
                "try_acquire_session_turn_lease",
                side_effect=RuntimeError("database is locked"),
            ),
        ):
            self.assertFalse(
                self.db.acquire_session_turn_lease("root", "owner-a", wait_seconds=0.0)
            )

        classify.assert_called_once()
        self.assertIsInstance(classify.call_args.args[0], RuntimeError)


if __name__ == "__main__":
    unittest.main()
