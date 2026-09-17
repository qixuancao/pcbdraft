"""Focused coverage for the SessionDB read-only lineage boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_lineage


class SessionLineageMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_lineage_api_without_reverse_import(self) -> None:
        method_names = (
            "_is_explicit_fork_child_row",
            "_is_compression_child_row",
            "get_compression_lineage",
        )
        mixin = session_db_lineage.SessionLineageMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(Path(session_db_lineage.__file__).read_text(encoding="utf-8"))
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

    def test_real_compression_lineage_skips_explicit_branch(self) -> None:
        self.db.create_session("root", "cli")
        self.db.end_session("root", "compression")
        self.db.create_session(
            "branch",
            "cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )
        self.db.create_session("middle", "cli", parent_session_id="root")
        self.db.end_session("middle", "compression")
        self.db.create_session("tip", "cli", parent_session_id="middle")

        expected = ["root", "middle", "tip"]
        self.assertEqual(self.db.get_compression_lineage("root"), expected)
        self.assertEqual(self.db.get_compression_lineage("middle"), expected)
        self.assertEqual(self.db.get_compression_lineage("tip"), expected)
        self.assertEqual(self.db.get_compression_lineage("branch"), ["branch"])
        self.assertEqual(self.db.get_compression_lineage("missing"), [])

    def test_delegate_marker_inherited_by_continuation_is_not_a_fork(self) -> None:
        self.db.create_session("outer", "cli")
        self.db.create_session(
            "delegate",
            "cli",
            parent_session_id="outer",
            model_config={"_delegate_from": "outer"},
        )
        self.db.end_session("delegate", "compression")
        self.db.create_session(
            "continuation",
            "cli",
            parent_session_id="delegate",
            model_config={"_delegate_from": "outer"},
        )

        continuation = self.db.get_session("continuation")
        self.assertFalse(self.db._is_explicit_fork_child_row(continuation))
        self.assertTrue(self.db._is_compression_child_row(continuation))
        self.assertEqual(
            self.db.get_compression_lineage("continuation"),
            ["delegate", "continuation"],
        )

    def test_legacy_json_loads_patch_path_remains_live(self) -> None:
        legacy_loads = session_db.json.loads
        row = {
            "source": "cli",
            "parent_session_id": "root",
            "model_config": '{"_branched_from": "root"}',
        }
        with patch.object(session_db.json, "loads", wraps=legacy_loads) as loads:
            self.assertTrue(self.db._is_explicit_fork_child_row(row))

        loads.assert_called_once_with(row["model_config"])


if __name__ == "__main__":
    unittest.main()
