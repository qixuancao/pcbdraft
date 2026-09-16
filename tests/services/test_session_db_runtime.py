import errno
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.services import session_db, session_db_runtime


class SessionDBRuntimeCompatibilityTests(unittest.TestCase):
    def test_legacy_module_reexports_runtime_helpers(self):
        names = (
            "apply_wal_with_fallback",
            "apply_database_pragmas",
            "classify_persistence_error",
            "is_disk_full_error",
            "CompressionSessionBusyError",
            "CompressionSessionClosedError",
            "SessionTurnLeaseLostError",
            "_on_disk_journal_mode",
            "_is_sqlite_wal_reset_vulnerable",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(session_db, name),
                    getattr(session_db_runtime, name),
                )

    def test_persistence_error_classification_keeps_typed_causes(self):
        cases = (
            (session_db_runtime.CompressionSessionBusyError(), "compression"),
            (
                session_db_runtime.CompressionSessionClosedError("session-1"),
                "compression_closed",
            ),
            (session_db_runtime.SessionTurnLeaseLostError(), "turn_lease"),
            (sqlite3.DatabaseError("database disk image is malformed"), "corrupt"),
            (sqlite3.OperationalError("database is locked"), "locked"),
            (OSError(errno.ENOSPC, "No space left on device"), "disk"),
            (RuntimeError("unexpected"), "unknown"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(
                    session_db_runtime.classify_persistence_error(error), expected
                )

    def test_wal_policy_operates_without_session_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "runtime.db"
            conn = sqlite3.connect(db_path)
            self.addCleanup(conn.close)
            with (
                patch.object(
                    session_db_runtime,
                    "is_sqlite_wal_reset_vulnerable",
                    return_value=False,
                ),
                patch.object(
                    session_db_runtime, "resolve_journal_mode", return_value="wal"
                ),
            ):
                self.assertEqual(
                    session_db_runtime.apply_wal_with_fallback(conn), "wal"
                )
            self.assertEqual(
                session_db_runtime._on_disk_journal_mode(conn),
                "wal",
            )


if __name__ == "__main__":
    unittest.main()
