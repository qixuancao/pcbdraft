"""Focused coverage for the SessionDB pruning mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_pruning


class SessionPruningMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.sessions_dir = self.home / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)

    def _set_started_at(self, session_id: str, timestamp: float) -> None:
        self.db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET started_at = ?, last_activity_at = NULL "
                "WHERE id = ?",
                (timestamp, session_id),
            )
        )

    def _write_session_files(self, session_id: str) -> list[Path]:
        files = [
            self.sessions_dir / f"{session_id}.json",
            self.sessions_dir / f"{session_id}.jsonl",
            self.sessions_dir / f"request_dump_{session_id}_1.json",
        ]
        for path in files:
            path.write_text(session_id, encoding="utf-8")
        return files

    def _create_filter_match(
        self, session_id: str, *, timestamp: float, ended: bool
    ) -> None:
        self.db.create_session(
            session_id,
            "cli",
            model="gpt-5-test",
            cwd="/repo/worktree",
            user_id="user-1",
            chat_id="chat-1",
            chat_type="dm",
        )
        self._set_started_at(session_id, timestamp)
        self.db.update_session_cwd(
            session_id,
            "/repo/worktree",
            git_branch="feature/alpha",
            git_repo_root="/repo",
        )
        self.db.set_session_title(session_id, f"Alpha_100% {session_id}")
        self.db.append_message(
            session_id,
            "assistant",
            "answer",
            tool_calls=[{"id": f"call-{session_id}", "type": "function"}],
            timestamp=timestamp,
        )
        self.db.update_token_counts(
            session_id,
            input_tokens=3,
            output_tokens=2,
            actual_cost_usd=0.4,
            billing_provider="Provider-X",
        )
        if ended:
            self.db.end_session(session_id, "complete")

    def test_session_db_inherits_pruning_api_without_reverse_import(self) -> None:
        mixin = session_db_pruning.SessionPruningMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "_prune_filter_where",
            "_apply_prune_age_filter",
            "list_prune_candidates",
            "count_open_prune_matches",
            "archive_sessions",
            "archive_stale_sessions",
            "prune_sessions",
            "purge_stale_tool_call_markers",
            "prune_empty_ghost_sessions",
            "finalize_orphaned_compression_sessions",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(Path(session_db_pruning.__file__).read_text(encoding="utf-8"))
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

    def test_candidate_filters_open_count_and_archive_use_legacy_helpers(self) -> None:
        self._create_filter_match("old-ended", timestamp=10, ended=True)
        self._create_filter_match("old-open", timestamp=10, ended=False)
        self._create_filter_match("recent-ended", timestamp=90, ended=True)
        filters = {
            "source": "cli",
            "title_like": "alpha_100%",
            "cwd_prefix": "/repo",
            "min_messages": 1,
            "max_messages": 1,
            "model_like": "GPT-5",
            "provider": "PROVIDER-X",
            "user_id": "user-1",
            "chat_id": "chat-1",
            "chat_type": "dm",
            "branch_like": "FEATURE/ALPHA",
            "min_tokens": 5,
            "max_tokens": 5,
            "min_cost": 0.4,
            "max_cost": 0.4,
            "min_tool_calls": 1,
            "max_tool_calls": 1,
        }

        escape_like = session_db._escape_like
        cwd_clause = session_db._cwd_prefix_clause
        with (
            patch.object(session_db.time, "time", return_value=100.0),
            patch.object(session_db, "_escape_like", wraps=escape_like) as escape,
            patch.object(session_db, "_cwd_prefix_clause", wraps=cwd_clause) as cwd_sql,
        ):
            rows = self.db.list_prune_candidates(older_than_days=50 / 86400, **filters)
            open_count = self.db.count_open_prune_matches(
                older_than_days=50 / 86400, **filters
            )

        self.assertEqual([row["id"] for row in rows], ["old-ended"])
        self.assertEqual(open_count, 1)
        self.assertGreaterEqual(escape.call_count, 6)
        self.assertEqual(cwd_sql.call_count, 2)

        self.assertEqual(
            self.db.archive_sessions(
                older_than_days=None,
                last_active_before=50,
                source="cli",
                title_like="alpha_100%",
            ),
            1,
        )
        self.assertEqual(self.db.get_session("old-ended")["archived"], 1)
        self.assertEqual(self.db.get_session("old-open")["archived"], 0)
        self.assertEqual(
            self.db.archive_sessions(
                older_than_days=None,
                last_active_before=50,
                source="cli",
                title_like="alpha_100%",
            ),
            0,
        )

    def test_archive_stale_sessions_respects_pins_and_time_sql_patch_paths(
        self,
    ) -> None:
        for session_id, timestamp in (
            ("stale", 10),
            ("pinned", 10),
            ("recent", 90),
        ):
            self.db.create_session(session_id, "cli")
            self._set_started_at(session_id, timestamp)
            self.db.append_message(session_id, "user", session_id, timestamp=timestamp)
        self.db.set_session_pinned("pinned", True)

        last_active = session_db._sql_session_last_active
        with (
            patch.object(session_db.time, "time", return_value=100.0),
            patch.object(
                session_db, "_sql_session_last_active", wraps=last_active
            ) as active_sql,
        ):
            self.assertEqual(
                self.db.archive_stale_sessions(50 / 86400, exclude_pinned=True), 1
            )

        active_sql.assert_called_once_with("s")
        self.assertEqual(self.db.get_session("stale")["archived"], 1)
        self.assertEqual(self.db.get_session("pinned")["archived"], 0)
        self.assertEqual(self.db.get_session("recent")["archived"], 0)

    def test_prune_deletes_ended_match_orphans_child_and_removes_files(self) -> None:
        self.db.create_session("old-parent", "cli")
        self._set_started_at("old-parent", 10)
        self.db.append_message("old-parent", "user", "old", timestamp=10)
        self.db.end_session("old-parent", "complete")
        old_files = self._write_session_files("old-parent")
        self.db.create_session("child", "cli", parent_session_id="old-parent")
        self.db.create_session("recent-ended", "cli")
        self._set_started_at("recent-ended", 90)
        self.db.append_message("recent-ended", "user", "recent", timestamp=90)
        self.db.end_session("recent-ended", "complete")
        self.db.create_session("old-open", "cli")
        self._set_started_at("old-open", 10)
        self.db.append_message("old-open", "user", "open", timestamp=10)

        with patch.object(session_db.time, "time", return_value=100.0):
            count = self.db.prune_sessions(
                older_than_days=50 / 86400,
                source="cli",
                sessions_dir=self.sessions_dir,
            )

        self.assertEqual(count, 1)
        self.assertIsNone(self.db.get_session("old-parent"))
        self.assertTrue(all(not path.exists() for path in old_files))
        self.assertIsNone(self.db.get_session("child")["parent_session_id"])
        self.assertIsNotNone(self.db.get_session("recent-ended"))
        self.assertIsNotNone(self.db.get_session("old-open"))

    def test_purge_stale_markers_supports_dry_run_backup_and_legacy_paths(self) -> None:
        self.db.create_session("session", "cli")
        marker_id = self.db.append_message(
            "session",
            "assistant",
            "[memory]",
            tool_calls=[{"id": "call-marker", "type": "function"}],
        )
        normal_id = self.db.append_message(
            "session",
            "assistant",
            "normal text",
            tool_calls=[{"id": "call-normal", "type": "function"}],
        )

        marker_pattern = session_db._STALE_TOOL_CALL_MARKER_RE
        with patch.object(
            session_db, "_STALE_TOOL_CALL_MARKER_RE", wraps=marker_pattern
        ) as pattern:
            dry_run = self.db.purge_stale_tool_call_markers(dry_run=True)
        self.assertEqual(dry_run["row_ids"], [marker_id])
        self.assertEqual(dry_run["rows_affected"], 1)
        self.assertGreaterEqual(pattern.fullmatch.call_count, 2)

        with patch.object(session_db.logger, "info") as log_info:
            result = self.db.purge_stale_tool_call_markers(backup=True)
        self.assertEqual(result["row_ids"], [marker_id])
        self.assertEqual(result["rows_affected"], 1)
        self.assertIsNotNone(result["backup_path"])
        self.assertTrue(Path(result["backup_path"]).is_file())
        self.assertGreaterEqual(log_info.call_count, 2)

        with self.db._read_ctx() as conn:
            rows = {
                row["id"]: row["content"]
                for row in conn.execute(
                    "SELECT id, content FROM messages WHERE id IN (?, ?)",
                    (marker_id, normal_id),
                ).fetchall()
            }
        self.assertEqual(rows[marker_id], "")
        self.assertEqual(rows[normal_id], "normal text")

    def test_prune_empty_tui_ghosts_removes_files_and_preserves_content(self) -> None:
        self.db.create_session("ghost", "tui")
        self._set_started_at("ghost", 1)
        self.db.end_session("ghost", "complete")
        ghost_files = self._write_session_files("ghost")

        self.db.create_session("titled", "tui")
        self._set_started_at("titled", 1)
        self.db.set_session_title("titled", "Keep me")
        self.db.end_session("titled", "complete")

        self.db.create_session("with-message", "tui")
        self._set_started_at("with-message", 1)
        self.db.append_message("with-message", "user", "keep", timestamp=1)
        self.db.end_session("with-message", "complete")

        with patch.object(session_db.time, "time", return_value=200_000.0) as now:
            removed = self.db.prune_empty_ghost_sessions(self.sessions_dir)

        self.assertEqual(removed, 1)
        self.assertEqual(now.call_count, 1)
        self.assertIsNone(self.db.get_session("ghost"))
        self.assertTrue(all(not path.exists() for path in ghost_files))
        self.assertIsNotNone(self.db.get_session("titled"))
        self.assertIsNotNone(self.db.get_session("with-message"))

    def test_finalize_only_old_message_bearing_compression_orphan(self) -> None:
        self.db.create_session("parent", "cli")
        self.db.end_session("parent", "compression")

        self.db.create_session("old-child", "cli", parent_session_id="parent")
        self._set_started_at("old-child", 1)
        self.db.append_message("old-child", "user", "preserve", timestamp=1)

        self.db.create_session("empty-child", "cli", parent_session_id="parent")
        self._set_started_at("empty-child", 1)

        self.db.create_session("recent-child", "cli", parent_session_id="parent")
        self._set_started_at("recent-child", 999_999)
        self.db.append_message("recent-child", "user", "recent", timestamp=999_999)

        with patch.object(session_db.time, "time", return_value=1_000_000.0) as now:
            finalized = self.db.finalize_orphaned_compression_sessions()

        self.assertEqual(finalized, 1)
        self.assertEqual(now.call_count, 2)
        old_child = self.db.get_session("old-child")
        self.assertEqual(old_child["end_reason"], "orphaned_compression")
        self.assertEqual(old_child["ended_at"], 1_000_000.0)
        self.assertEqual(
            [message["content"] for message in self.db.get_messages("old-child")],
            ["preserve"],
        )
        self.assertIsNone(self.db.get_session("empty-child")["ended_at"])
        self.assertIsNone(self.db.get_session("recent-child")["ended_at"])


if __name__ == "__main__":
    unittest.main()
