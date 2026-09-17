"""Focused coverage for the SessionDB Telegram topic persistence boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_telegram_topics


class SessionTelegramTopicsMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def _table_names(self) -> set[str]:
        rows = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _create_telegram_session(
        self, session_id: str, *, user_id: str = "user-1", timestamp: float = 1
    ) -> None:
        self.db.create_session(
            session_id,
            "telegram",
            user_id=user_id,
            chat_id="chat-1",
            session_key=f"telegram:{user_id}:{session_id}",
        )
        self.db.append_message(
            session_id, "user", f"message {session_id}", timestamp=timestamp
        )

    def test_session_db_inherits_topic_api_without_reverse_import(self) -> None:
        mixin = session_db_telegram_topics.SessionTelegramTopicsMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "apply_telegram_topic_migration",
            "enable_telegram_topic_mode",
            "disable_telegram_topic_mode",
            "is_telegram_topic_mode_enabled",
            "get_telegram_topic_binding",
            "list_telegram_topic_bindings_for_chat",
            "get_telegram_topic_binding_by_session",
            "delete_telegram_topic_binding",
            "bind_telegram_topic",
            "is_telegram_session_linked_to_topic",
            "list_unlinked_telegram_sessions_for_user",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_telegram_topics.__file__).read_text(encoding="utf-8")
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

    def test_reads_and_disable_are_noops_before_explicit_migration(self) -> None:
        self._create_telegram_session("unlinked")
        before = self._table_names()

        self.assertFalse(
            self.db.is_telegram_topic_mode_enabled(chat_id="chat-1", user_id="user-1")
        )
        self.assertIsNone(
            self.db.get_telegram_topic_binding(chat_id="chat-1", thread_id="11")
        )
        self.assertIsNone(
            self.db.get_telegram_topic_binding_by_session(session_id="unlinked")
        )
        self.assertEqual(
            self.db.list_telegram_topic_bindings_for_chat(chat_id="chat-1"), []
        )
        self.assertFalse(
            self.db.is_telegram_session_linked_to_topic(session_id="unlinked")
        )
        self.assertEqual(
            self.db.delete_telegram_topic_binding(chat_id="chat-1", thread_id="11"),
            0,
        )
        self.db.disable_telegram_topic_mode(chat_id="chat-1")

        self.assertEqual(self._table_names(), before)

    def test_enable_and_bind_preserve_idempotency_and_unique_session_rule(self) -> None:
        self._create_telegram_session("session-1")
        with patch.object(session_db.time, "time", return_value=123.5):
            self.db.enable_telegram_topic_mode(
                chat_id="chat-1",
                user_id="user-1",
                has_topics_enabled=True,
                allows_users_to_create_topics=False,
            )
            self.db.bind_telegram_topic(
                chat_id="chat-1",
                thread_id="11",
                user_id="user-1",
                session_key="telegram:user-1:session-1",
                session_id="session-1",
            )

        self.assertTrue(
            self.db.is_telegram_topic_mode_enabled(chat_id="chat-1", user_id="user-1")
        )
        mode = self.db._conn.execute(
            "SELECT * FROM telegram_dm_topic_mode WHERE chat_id = 'chat-1'"
        ).fetchone()
        self.assertEqual(mode["activated_at"], 123.5)
        self.assertEqual(mode["has_topics_enabled"], 1)
        self.assertEqual(mode["allows_users_to_create_topics"], 0)

        binding = self.db.get_telegram_topic_binding(chat_id="chat-1", thread_id="11")
        self.assertEqual(binding["session_id"], "session-1")
        self.assertEqual(
            self.db.get_telegram_topic_binding_by_session(session_id="session-1"),
            binding,
        )
        self.assertEqual(
            self.db.list_telegram_topic_bindings_for_chat(chat_id="chat-1"),
            [binding],
        )
        self.assertTrue(
            self.db.is_telegram_session_linked_to_topic(session_id="session-1")
        )

        self.db.bind_telegram_topic(
            chat_id="chat-1",
            thread_id="11",
            user_id="user-1",
            session_key="updated-key",
            session_id="session-1",
            managed_mode="manual",
        )
        updated = self.db.get_telegram_topic_binding(chat_id="chat-1", thread_id="11")
        self.assertEqual(updated["session_key"], "updated-key")
        self.assertEqual(updated["managed_mode"], "manual")

        with self.assertRaisesRegex(ValueError, "already linked"):
            self.db.bind_telegram_topic(
                chat_id="chat-1",
                thread_id="12",
                user_id="user-1",
                session_key="updated-key",
                session_id="session-1",
            )

    def test_delete_last_binding_disables_mode_and_preserves_other_lanes(self) -> None:
        for session_id in ("session-1", "session-2"):
            self._create_telegram_session(session_id)
        self.db.enable_telegram_topic_mode(chat_id="chat-1", user_id="user-1")
        for thread_id, session_id in (("11", "session-1"), ("12", "session-2")):
            self.db.bind_telegram_topic(
                chat_id="chat-1",
                thread_id=thread_id,
                user_id="user-1",
                session_key=f"key:{session_id}",
                session_id=session_id,
            )

        self.assertEqual(
            self.db.delete_telegram_topic_binding(chat_id="chat-1", thread_id="11"),
            1,
        )
        self.assertTrue(
            self.db.is_telegram_topic_mode_enabled(chat_id="chat-1", user_id="user-1")
        )
        self.assertEqual(
            self.db.delete_telegram_topic_binding(chat_id="chat-1", thread_id="12"),
            1,
        )
        self.assertFalse(
            self.db.is_telegram_topic_mode_enabled(chat_id="chat-1", user_id="user-1")
        )

    def test_unlinked_listing_uses_legacy_projection_patch_paths(self) -> None:
        self._create_telegram_session("older", timestamp=10)
        self._create_telegram_session("newer", timestamp=20)
        self._create_telegram_session("other-user", user_id="user-2", timestamp=30)

        last_active = session_db._sql_session_last_active
        with (
            patch.object(
                session_db,
                "_PREVIEW_RAW_SELECT",
                "'legacy projection'",
            ),
            patch.object(
                session_db,
                "_shape_preview",
                side_effect=lambda value: f"legacy:{value}",
            ) as shape_preview,
            patch.object(
                session_db,
                "_sql_session_last_active",
                wraps=last_active,
            ) as active_sql,
        ):
            rows = self.db.list_unlinked_telegram_sessions_for_user(
                chat_id="chat-1", user_id="user-1"
            )

        self.assertEqual([row["id"] for row in rows], ["newer", "older"])
        self.assertEqual(rows[0]["preview"], "legacy:legacy projection")
        self.assertEqual(shape_preview.call_count, 2)
        self.assertEqual(active_sql.call_args_list, [(("s",),), (("s",),)])

        self.db.bind_telegram_topic(
            chat_id="chat-1",
            thread_id="11",
            user_id="user-1",
            session_key="key:newer",
            session_id="newer",
        )
        rows = self.db.list_unlinked_telegram_sessions_for_user(
            chat_id="chat-1", user_id="user-1"
        )
        self.assertEqual([row["id"] for row in rows], ["older"])

    def test_v1_binding_table_is_rebuilt_with_cascade(self) -> None:
        self._create_telegram_session("legacy")

        def create_v1(conn):
            conn.executescript(
                """
                CREATE TABLE telegram_dm_topic_bindings (
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    managed_mode TEXT NOT NULL DEFAULT 'auto',
                    linked_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                );
                INSERT INTO telegram_dm_topic_bindings VALUES (
                    'chat-1', '11', 'user-1', 'legacy-key', 'legacy',
                    'auto', 1, 1
                );
                INSERT INTO state_meta (key, value) VALUES (
                    'telegram_dm_topic_schema_version', '1'
                );
                """
            )

        self.db._execute_write(create_v1)
        self.db.apply_telegram_topic_migration()

        fk_rows = self.db._conn.execute(
            "PRAGMA foreign_key_list('telegram_dm_topic_bindings')"
        ).fetchall()
        self.assertTrue(
            any(row[2] == "sessions" and row[6] == "CASCADE" for row in fk_rows)
        )
        self.assertEqual(
            self.db.get_telegram_topic_binding_by_session(session_id="legacy")[
                "thread_id"
            ],
            "11",
        )
        self.assertTrue(self.db.delete_session("legacy"))
        self.assertIsNone(
            self.db.get_telegram_topic_binding_by_session(session_id="legacy")
        )


if __name__ == "__main__":
    unittest.main()
