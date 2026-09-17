"""Focused coverage for the SessionDB gateway-query mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_gateway_queries


class SessionGatewayQueryMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def _create_gateway_session(
        self,
        session_id: str,
        *,
        session_key: str,
        user_id: str,
        started_at: float,
        source: str = "telegram",
        chat_id: str = "chat-1",
        chat_type: str = "group",
        thread_id: str | None = None,
    ) -> None:
        self.db.create_session(session_id, source)
        self.db.record_gateway_session_peer(
            session_id,
            source=source,
            user_id=user_id,
            session_key=session_key,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
        )
        self.db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
                (started_at, started_at, session_id),
            )
        )

    def test_session_db_inherits_query_api_without_reverse_import(self) -> None:
        method_names = (
            "list_gateway_sessions",
            "find_session_by_origin",
            "find_latest_gateway_session_for_peer",
            "find_orphaned_gateway_sessions",
        )
        mixin = session_db_gateway_queries.SessionGatewayQueryMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )
        self.assertEqual(session_db.SessionDB._ORPHAN_ADOPTION_MAX_GAP_S, 900.0)

        for host_method in (
            "record_gateway_session_peer",
            "save_gateway_routing_entry",
            "replace_gateway_routing_entries",
            "load_gateway_routing_entries",
            "delete_gateway_routing_entries",
            "list_never_active_keyed_sessions",
            "prune_never_active_keyed_sessions",
            "adopt_orphaned_gateway_session",
        ):
            self.assertNotIn(host_method, mixin.__dict__)
            self.assertIn(host_method, session_db.SessionDB.__dict__)

        tree = ast.parse(
            Path(session_db_gateway_queries.__file__).read_text(encoding="utf-8")
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

    def test_list_and_origin_lookup_use_durable_peer_rows(self) -> None:
        self._create_gateway_session(
            "alpha-old", session_key="key-alpha", user_id="user-a", started_at=10
        )
        self._create_gateway_session(
            "alpha-new", session_key="key-alpha", user_id="user-a", started_at=20
        )
        self._create_gateway_session(
            "beta", session_key="key-beta", user_id="user-b", started_at=30
        )

        last_active_sql = session_db._sql_session_last_active
        with (
            patch.object(
                session_db,
                "_sql_session_last_active",
                wraps=last_active_sql,
            ) as last_active,
            patch.object(
                self.db,
                "flush_token_counts",
                wraps=self.db.flush_token_counts,
            ) as flush_token_counts,
        ):
            rows = self.db.list_gateway_sessions(platform="TELEGRAM")

        flush_token_counts.assert_called_once_with()
        last_active.assert_called_once_with("sessions")
        self.assertEqual([row["id"] for row in rows], ["beta", "alpha-new"])
        self.assertEqual(
            self.db.find_session_by_origin(
                platform="telegram", chat_id="chat-1", user_id="user-a"
            ),
            "alpha-new",
        )
        self.assertIsNone(
            self.db.find_session_by_origin(platform="telegram", chat_id="chat-1")
        )
        self.assertIsNone(
            self.db.find_session_by_origin(
                platform="telegram", chat_id="chat-1", user_id="unknown"
            )
        )

    def test_latest_peer_lookup_honors_legacy_reset_sql_patch(self) -> None:
        self._create_gateway_session(
            "candidate", session_key="peer-key", user_id="user-a", started_at=50
        )
        self.db.append_message("candidate", "user", "recover me", timestamp=100)
        self.db.end_session("candidate", "agent_close")
        self.db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET last_activity_at = 100, ended_at = 110 "
                "WHERE id = 'candidate'"
            )
        )

        self._create_gateway_session(
            "boundary", session_key="peer-key", user_id="user-a", started_at=150
        )
        self.db.end_session("boundary", "patched_boundary")
        self.db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET last_activity_at = 150, ended_at = 200 "
                "WHERE id = 'boundary'"
            )
        )

        row = self.db.find_latest_gateway_session_for_peer(
            source="telegram", session_key="peer-key"
        )
        self.assertEqual(row["id"], "candidate")

        with patch.object(
            session_db,
            "_RESET_END_REASONS_SQL",
            "'patched_boundary'",
        ):
            self.assertIsNone(
                self.db.find_latest_gateway_session_for_peer(
                    source="telegram", session_key="peer-key"
                )
            )

    def test_orphan_query_projects_candidate_before_host_adoption(self) -> None:
        self._create_gateway_session(
            "donor", session_key="peer-key", user_id="user-a", started_at=100
        )
        self.db.append_message("donor", "user", "before gap", timestamp=110)
        self.db.create_session(
            "orphan",
            "telegram",
            parent_session_id="donor",
            user_id="user-a",
        )
        self.db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
                (115, 115, "orphan"),
            )
        )
        self.db.append_message("orphan", "user", "after gap", timestamp=120)

        last_active_sql = session_db._sql_session_last_active
        with patch.object(
            session_db,
            "_sql_session_last_active",
            wraps=last_active_sql,
        ) as last_active:
            records = self.db.find_orphaned_gateway_sessions()

        self.assertEqual(last_active.call_args_list, [call("o"), call("d")])
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0],
            {
                "orphan_id": "orphan",
                "source": "telegram",
                "message_count": 1,
                "started_at": 115.0,
                "last_active": 120.0,
                "donor_id": "donor",
                "session_key": "peer-key",
                "evidence": "lineage",
                "adoptable": True,
                "reason": "",
            },
        )
        self.assertIsNone(self.db.get_session("orphan")["session_key"])

        self.assertTrue(self.db.adopt_orphaned_gateway_session("orphan", "donor"))
        self.assertEqual(self.db.get_session("orphan")["session_key"], "peer-key")
        self.assertEqual(self.db.find_orphaned_gateway_sessions(), [])


if __name__ == "__main__":
    unittest.main()
