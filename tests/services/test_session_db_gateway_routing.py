"""Focused coverage for the SessionDB gateway-routing mixin boundary."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_gateway_routing


class SessionGatewayRoutingMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_routing_api_without_reverse_import(self) -> None:
        method_names = (
            "record_gateway_session_peer",
            "set_expiry_finalized",
            "save_gateway_routing_entry",
            "replace_gateway_routing_entries",
            "load_gateway_routing_entries",
            "delete_gateway_routing_entries",
            "list_never_active_keyed_sessions",
            "_delete_routing_entries_for_sessions",
            "prune_never_active_keyed_sessions",
        )
        mixin = session_db_gateway_routing.SessionGatewayRoutingMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        for host_boundary in (
            "_execute_write",
            "delete_session",
            "adopt_orphaned_gateway_session",
        ):
            self.assertNotIn(host_boundary, mixin.__dict__)
        self.assertIn("adopt_orphaned_gateway_session", session_db.SessionDB.__dict__)

        for query_method in (
            "list_gateway_sessions",
            "find_session_by_origin",
            "find_latest_gateway_session_for_peer",
            "find_orphaned_gateway_sessions",
        ):
            self.assertNotIn(query_method, mixin.__dict__)

        tree = ast.parse(
            Path(session_db_gateway_routing.__file__).read_text(encoding="utf-8")
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

    def test_peer_record_self_heals_and_updates_compression_lineage(self) -> None:
        with patch.object(session_db.time, "time", return_value=123.0):
            self.db.record_gateway_session_peer(
                "missing",
                source="telegram",
                user_id="user-a",
                session_key="peer-a",
                chat_id="chat-a",
                display_name="First",
            )
        row = self.db.get_session("missing")
        self.assertEqual(row["started_at"], 123.0)
        self.assertEqual(row["session_key"], "peer-a")
        self.assertEqual(row["chat_id"], "chat-a")

        self.db.set_expiry_finalized("missing")
        self.assertEqual(self.db.get_session("missing")["expiry_finalized"], 1)

        self.db.create_session("parent", "telegram")
        self.db.end_session("parent", "compression")
        self.db.create_session(
            "child",
            "telegram",
            parent_session_id="parent",
        )
        self.db.record_gateway_session_peer(
            "child",
            source="telegram",
            session_key="lineage-key",
            chat_id="chat-b",
            include_compression_ancestors=True,
        )
        self.assertEqual(self.db.get_session("parent")["session_key"], "lineage-key")
        self.assertEqual(self.db.get_session("child")["session_key"], "lineage-key")

    def test_scoped_routing_index_uses_host_transaction_and_legacy_time(self) -> None:
        with (
            patch.object(session_db.time, "time", return_value=456.0),
            patch.object(
                self.db,
                "_execute_write",
                wraps=self.db._execute_write,
            ) as execute_write,
        ):
            self.db.save_gateway_routing_entry("shared", '{"v": 1}', scope="one")
            self.db.save_gateway_routing_entry("shared", '{"v": 2}', scope="two")
            self.db.replace_gateway_routing_entries(
                {"replacement": '{"v": 3}', "": '{"ignored": true}'},
                scope="one",
            )

        self.assertEqual(execute_write.call_count, 3)
        self.assertEqual(
            self.db.load_gateway_routing_entries(scope="one"),
            {"replacement": '{"v": 3}'},
        )
        self.assertEqual(
            self.db.load_gateway_routing_entries(scope="two"),
            {"shared": '{"v": 2}'},
        )
        with self.db._lock:
            updated_at = self.db._conn.execute(
                "SELECT updated_at FROM gateway_routing WHERE scope = 'one'"
            ).fetchone()[0]
        self.assertEqual(updated_at, 456.0)

        self.db.delete_gateway_routing_entries(["replacement"], scope="one")
        self.assertEqual(self.db.load_gateway_routing_entries(scope="one"), {})
        self.assertEqual(
            self.db.load_gateway_routing_entries(scope="two"),
            {"shared": '{"v": 2}'},
        )

    def test_never_active_prune_preserves_legacy_decode_and_log_patches(self) -> None:
        with patch.object(session_db.time, "time", return_value=100.0):
            self.db.create_session("stale", "telegram")
            self.db.record_gateway_session_peer(
                "stale",
                source="telegram",
                session_key="stale-key",
            )
            self.db.create_session("active", "telegram")
            self.db.record_gateway_session_peer(
                "active",
                source="telegram",
                session_key="active-key",
            )
            self.db.append_message("active", "user", "keep me", timestamp=101.0)
            self.db.save_gateway_routing_entry(
                "stale-key",
                '{"session_id": "stale"}',
                scope="scope-a",
            )
            self.db.save_gateway_routing_entry(
                "malformed",
                "not-json",
                scope="scope-a",
            )

        legacy_loads = json.loads
        with (
            patch.object(session_db.time, "time", return_value=864100.0),
            patch.object(session_db.json, "loads", wraps=legacy_loads) as loads,
            patch.object(
                session_db,
                "_exception_info_without_values",
                return_value=False,
            ) as exception_info,
            patch.object(session_db.logger, "debug") as log_debug,
        ):
            self.assertEqual(
                [
                    row["id"]
                    for row in self.db.list_never_active_keyed_sessions(
                        older_than_days=5.0
                    )
                ],
                ["stale"],
            )
            self.assertEqual(
                self.db.prune_never_active_keyed_sessions(older_than_days=5.0),
                (1, 1),
            )

        self.assertGreaterEqual(loads.call_count, 2)
        exception_info.assert_called_once_with()
        log_debug.assert_called_once_with(
            "Gateway routing entry decode failed",
            exc_info=False,
        )
        self.assertIsNone(self.db.get_session("stale"))
        self.assertIsNotNone(self.db.get_session("active"))
        self.assertEqual(
            self.db.load_gateway_routing_entries(scope="scope-a"),
            {"malformed": "not-json"},
        )


if __name__ == "__main__":
    unittest.main()
