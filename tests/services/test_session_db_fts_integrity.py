"""Focused coverage for the SessionDB FTS integrity mixin boundary."""

from __future__ import annotations

import ast
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_fts_integrity


class SessionFTSIntegrityMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_integrity_api_without_reverse_import(self) -> None:
        mixin = session_db_fts_integrity.SessionFTSIntegrityMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "_is_fts5_unavailable_error",
            "_is_trigram_unavailable_error",
            "_db_has_legacy_inline_fts",
            "_warn_trigram_unavailable",
            "_warn_fts5_unavailable",
            "_ensure_fts_cjk_schema",
            "_drop_fts_triggers",
            "_ensure_fts_schema",
            "_is_fts_write_corruption_error",
            "_try_runtime_fts_rebuild",
            "_enter_fts_fail_open",
            "_has_fts_trash",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_fts_integrity.__file__).read_text(encoding="utf-8")
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

    def test_real_database_probe_and_missing_trigger_repair(self) -> None:
        if not self.db._fts_enabled:
            self.skipTest("SQLite build does not provide FTS5")

        cursor = self.db._conn.cursor()
        self.assertTrue(self.db._fts_table_probe(cursor, "messages_fts"))
        self.assertFalse(self.db._db_has_legacy_inline_fts(cursor))
        cursor.execute("DROP TRIGGER messages_fts_insert")
        self.assertIsNone(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                ("messages_fts_insert",),
            ).fetchone()
        )

        self.assertTrue(
            self.db._ensure_fts_schema(cursor, "messages_fts", session_db.FTS_SQL)
        )
        self.assertIsNotNone(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                ("messages_fts_insert",),
            ).fetchone()
        )

    def test_legacy_trigger_allowlist_patch_controls_drop(self) -> None:
        cursor = self.db._conn.cursor()
        cursor.execute(
            "CREATE TRIGGER legacy_patch_probe AFTER INSERT ON messages "
            "BEGIN SELECT 1; END"
        )

        with patch.object(session_db, "_FTS_TRIGGERS", ("legacy_patch_probe",)):
            self.db._drop_fts_triggers(cursor)

        self.assertIsNone(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                ("legacy_patch_probe",),
            ).fetchone()
        )

    def test_legacy_malformed_classifier_patch_remains_live(self) -> None:
        error = sqlite3.DatabaseError("provider-specific corruption wording")
        with patch.object(
            session_db,
            "is_malformed_db_error",
            return_value=True,
        ) as classify:
            self.assertTrue(self.db._is_fts_write_corruption_error(error))

        classify.assert_called_once_with(error)


if __name__ == "__main__":
    unittest.main()
