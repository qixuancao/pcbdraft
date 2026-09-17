"""Focused coverage for the SessionDB compression-lease mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_compression_lease


class SessionCompressionLeaseMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session("session-1", "cli")

    def test_session_db_inherits_lease_api_without_reverse_import(self) -> None:
        method_names = (
            "_compression_lease_matches_on_conn",
            "_reclaim_expired_compression_lease_on_conn",
            "refresh_compression_lock",
            "try_acquire_compression_lock",
            "release_compression_lock",
            "get_compression_lock_holder",
        )
        mixin = session_db_compression_lease.SessionCompressionLeaseMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        for turn_lease_name in (
            "_session_turn_lease_key_on_conn",
            "try_acquire_session_turn_lease",
            "acquire_session_turn_lease",
            "refresh_session_turn_lease",
            "release_session_turn_lease",
        ):
            self.assertNotIn(turn_lease_name, mixin.__dict__)
            self.assertIn(turn_lease_name, session_db.SessionDB.__dict__)

        tree = ast.parse(
            Path(session_db_compression_lease.__file__).read_text(encoding="utf-8")
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
        self.assertIs(session_db_compression_lease.logger, session_db.logger)
        self.assertIs(session_db_compression_lease.time, session_db.time)

    def test_real_owner_isolation_refresh_and_release(self) -> None:
        other_db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        with patch.object(session_db.time, "time", return_value=100.0):
            self.assertTrue(
                self.db.try_acquire_compression_lock(
                    "session-1", "owner-a", ttl_seconds=1.0
                )
            )
            self.assertFalse(
                other_db.try_acquire_compression_lock(
                    "session-1", "owner-b", ttl_seconds=10.0
                )
            )

        with patch.object(session_db.time, "time", return_value=200.0):
            self.assertTrue(
                self.db.refresh_compression_lock(
                    "session-1", "owner-a", ttl_seconds=10.0
                )
            )
        with patch.object(session_db.time, "time", return_value=205.0):
            self.assertEqual(
                self.db.get_compression_lock_holder("session-1"), "owner-a"
            )
            self.assertFalse(
                other_db.try_acquire_compression_lock(
                    "session-1", "owner-b", ttl_seconds=10.0
                )
            )

        other_db.release_compression_lock("session-1", "owner-b")
        with patch.object(session_db.time, "time", return_value=205.0):
            self.assertEqual(
                self.db.get_compression_lock_holder("session-1"), "owner-a"
            )
        self.db.release_compression_lock("session-1", "owner-a")
        self.assertIsNone(self.db.get_compression_lock_holder("session-1"))

    def test_legacy_process_probe_patch_reclaims_live_lease(self) -> None:
        self.assertTrue(
            self.db.try_acquire_compression_lock(
                "session-1", "owner-a", ttl_seconds=600.0
            )
        )

        with patch.object(
            session_db,
            "_compression_lock_holder_process_is_dead",
            return_value=True,
        ) as process_is_dead:
            self.assertTrue(
                self.db.try_acquire_compression_lock(
                    "session-1", "owner-b", ttl_seconds=600.0
                )
            )

        process_is_dead.assert_called_once_with("owner-a")
        self.assertEqual(self.db.get_compression_lock_holder("session-1"), "owner-b")

    def test_archive_and_compact_rejects_stale_holder_before_commit(self) -> None:
        self.db.append_message("session-1", "user", "original request")
        self.assertTrue(
            self.db.try_acquire_compression_lock(
                "session-1", "owner-a", ttl_seconds=60.0
            )
        )

        with self.assertRaises(session_db.SessionCompressionInProgressError):
            self.db.archive_and_compact(
                "session-1",
                [{"role": "assistant", "content": "stale summary"}],
                lock_holder="owner-b",
            )
        self.assertEqual(
            [message["content"] for message in self.db.get_messages("session-1")],
            ["original request"],
        )

        self.assertEqual(
            self.db.archive_and_compact(
                "session-1",
                [{"role": "assistant", "content": "current summary"}],
                lock_holder="owner-a",
            ),
            1,
        )
        self.assertEqual(
            [message["content"] for message in self.db.get_messages("session-1")],
            ["current summary"],
        )

    def test_orphan_reopen_reclaims_expired_lease(self) -> None:
        self.db.end_session("session-1", "compression")
        with patch.object(session_db.time, "time", return_value=100.0):
            self.assertTrue(
                self.db.try_acquire_compression_lock(
                    "session-1", "owner-a", ttl_seconds=1.0
                )
            )

        with patch.object(session_db.time, "time", return_value=200.0):
            self.assertTrue(self.db.reopen_orphaned_compression_session("session-1"))
            self.assertIsNone(self.db.get_compression_lock_holder("session-1"))

        row = self.db.get_session("session-1")
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["end_reason"])


if __name__ == "__main__":
    unittest.main()
