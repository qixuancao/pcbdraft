"""Focused behavior checks for runtime and SessionDB lint repairs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from pcbdraft.core import clock, runtime_environment, runtime_logging, runtime_utils
from pcbdraft.services import session_db, session_db_common
from pcbdraft.services.session_db_schema import SessionSchemaMixin


class RuntimeLintRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)

    def test_safe_yaml_loaders_reject_python_objects(self) -> None:
        payload = "!!python/object/apply:builtins.eval ['1 + 1']"
        for c_loader in (getattr(yaml, "CSafeLoader", None), None):
            with (
                self.subTest(c_loader=c_loader),
                patch.object(yaml, "CSafeLoader", c_loader, create=True),
                patch.object(runtime_utils, "_fast_yaml_loader", None),
            ):
                self.assertEqual(
                    runtime_utils.fast_safe_load("enabled: true"), {"enabled": True}
                )
                with self.assertRaises(yaml.constructor.ConstructorError):
                    runtime_utils.fast_safe_load(payload)

    def test_timezone_failure_keeps_stack_without_exception_values(self) -> None:
        private_value = "opaque-private-provider-value"
        with (
            patch.object(clock, "ZoneInfo", side_effect=ValueError(private_value)),
            self.assertLogs(clock.logger, level="WARNING") as captured,
        ):
            self.assertIsNone(clock._get_zoneinfo(private_value))
        output = "\n".join(captured.output)
        self.assertIn("Traceback", output)
        self.assertIn("ValueError", output)
        self.assertNotIn(private_value, output)

    def test_config_parse_failure_falls_back_without_exposing_yaml(self) -> None:
        # Import before creating malformed input: configuration has its own
        # import-time recovery behavior, which is outside this logging probe.
        from pcbdraft.model import configuration

        private_value = "private-config-source-value"
        (self.home / "config.yaml").write_text(
            "logging: [" + private_value, encoding="utf-8"
        )
        with (
            patch.object(
                configuration,
                "read_raw_config",
                side_effect=RuntimeError(private_value),
            ),
            self.assertLogs(runtime_logging.logger, level="DEBUG") as captured,
        ):
            self.assertEqual(runtime_logging._read_logging_config(), (None, None, None))
        self.assertNotIn(private_value, "\n".join(captured.output))
        self.assertTrue(all(record.exc_info for record in captured.records))

    def test_node_script_path_is_literal_shell_argument(self) -> None:
        if runtime_environment.shutil.which("bash") is None:
            self.skipTest("bash is required for the POSIX bootstrap probe")
        script = self.home / 'node $(false) "bootstrap".sh'
        script.write_text(
            "_nb_install_bundled_node() { return 0; }\n"
            "heal_managed_node() { return 0; }\n",
            encoding="utf-8",
        )
        with (
            patch.object(runtime_environment, "_NODE_BOOTSTRAP_SCRIPT", script),
            patch.object(runtime_environment.sys, "platform", "linux"),
            patch.object(runtime_environment, "_managed_node_heal_attempted", False),
            patch.object(
                runtime_environment,
                "hermes_managed_node_tree_present",
                return_value=True,
            ),
        ):
            self.assertTrue(runtime_environment._bootstrap_managed_node_posix())
            self.assertTrue(runtime_environment.heal_hermes_managed_node())

    def test_missing_shell_does_not_spawn_a_process(self) -> None:
        script = self.home / "bootstrap.sh"
        script.touch()
        with (
            patch.object(runtime_environment, "_NODE_BOOTSTRAP_SCRIPT", script),
            patch.object(runtime_environment.shutil, "which", return_value=None),
            patch("subprocess.run") as run,
        ):
            self.assertFalse(runtime_environment._bootstrap_managed_node_posix())
        run.assert_not_called()

    def test_home_fallback_uses_platform_temp_directory(self) -> None:
        with patch.object(
            runtime_environment, "_iter_real_home_candidates", return_value=[]
        ):
            self.assertEqual(runtime_environment.get_real_home(), tempfile.gettempdir())


class SessionDBLintRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_compatibility_exports_remain_importable(self) -> None:
        self.assertIs(session_db.SCHEMA_SQL, session_db_common.SCHEMA_SQL)
        self.assertIs(session_db.FTS_SQL, session_db_common.FTS_SQL)
        self.assertIs(
            session_db._ephemeral_child_sql, session_db_common._ephemeral_child_sql
        )
        self.assertTrue(callable(session_db.describe_skill_invocation))

    def test_search_filters_sorting_and_hostile_fragments(self) -> None:
        if not self.db._fts_enabled:
            self.skipTest("SQLite FTS5 unavailable")
        self.db.create_session("early", "cli")
        self.db.create_session("late", "cron")
        self.db.append_message("early", "user", "needle early", timestamp=10)
        self.db.append_message("late", "user", "needle late", timestamp=20)
        oldest = self.db.search_messages(
            "needle", sort="oldest", fields=("session_id",)
        )
        self.assertEqual(oldest, [{"session_id": "early"}, {"session_id": "late"}])
        self.assertEqual(self.db.search_messages("needle", source_filter=[]), [])
        self.assertEqual(
            self.db.search_messages("needle", source_filter=["cli') OR 1=1 --"]), []
        )
        for kwargs in (
            {
                "table": "messages_fts_trigram; DROP TABLE sessions; --",
                "order_by_sql": "ORDER BY rank",
            },
            {"order_by_sql": "ORDER BY rank; DELETE FROM messages"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.db._run_trigram_search("needle", include_inactive=False, **kwargs)
        self.assertEqual(self.db.session_count(), 2)

    def test_cjk_boolean_and_short_token_fallbacks(self) -> None:
        if not self.db._trigram_available:
            self.skipTest("SQLite trigram tokenizer unavailable")
        self.db.create_session("cjk", "cli")
        self.db.append_message("cjk", "user", "大别山项目 广西施工")
        with patch.object(self.db, "_fts_cjk_available", False):
            for query in ("大别山 OR 桂林山", "广西"):
                with self.subTest(query=query):
                    self.assertEqual(
                        self.db.search_messages(query, fields=("session_id",)),
                        [{"session_id": "cjk"}],
                    )

    def test_reopen_preserves_legacy_reset_child_identity(self) -> None:
        self.db.create_session("parent", "cli", session_key="peer")
        self.db.end_session("parent", "session_reset")
        self.db.create_session(
            "child", "cli", session_key="peer", parent_session_id="parent"
        )
        self.db.reopen_session("parent")
        config = self.db.get_session("child")["model_config"]
        if isinstance(config, str):
            config = json.loads(config)
        self.assertEqual(config["_reset_from"], "parent")
        self.assertIsNone(self.db.get_session("parent")["end_reason"])

    def test_activity_aliases_reject_sql_injection(self) -> None:
        bad = "s.id); DROP TABLE sessions; --"
        for helper in (
            session_db_common._sql_session_last_active,
            session_db_common._sql_session_last_active_by_id,
            session_db_common._ephemeral_child_sql,
        ):
            with self.subTest(helper=helper), self.assertRaises(ValueError):
                helper(bad)
        with self.assertRaises(ValueError):
            session_db_common._legacy_reset_child_sql("child", "'idle') OR 1=1 --")
        with self.assertRaises(ValueError):
            self.db._fts_table_probe(self.db._conn, "sessions")

    def test_rich_rows_and_archived_workspace_counts(self) -> None:
        for sid in ("one", "two"):
            self.db.create_session(sid, "cli", cwd="/repo")
            self.db.append_message(sid, "user", "workspace preview")
        self.db.set_session_archived("two", True)
        self.assertEqual(self.db.distinct_session_cwds()[0]["sessions"], 1)
        self.assertEqual(
            self.db.distinct_session_cwds(include_archived=True)[0]["sessions"], 2
        )
        rows = self.db._get_session_rich_rows_batch(["one", "two"], compact_rows=True)
        self.assertEqual(set(rows), {"one", "two"})
        self.assertEqual(rows["one"]["preview"], "workspace preview")
        self.assertGreater(rows["one"]["last_active"], 0)

    def test_fts_trash_integer_key_teardown_is_chunked(self) -> None:
        self.db._conn.executescript(
            "CREATE TABLE fts_v22_trash_messages_fts_data (id INTEGER PRIMARY KEY);"
            "INSERT INTO fts_v22_trash_messages_fts_data VALUES (1), (2), (3);"
        )
        with patch.object(self.db, "_FTS_REBUILD_CHUNK_ROWS", 2):
            self.assertTrue(self.db._fts_teardown_trash_step())
            rows = self.db._conn.execute(
                "SELECT id FROM fts_v22_trash_messages_fts_data"
            ).fetchall()
            self.assertEqual([row[0] for row in rows], [3])
            self.assertTrue(self.db._fts_teardown_trash_step())
            self.assertTrue(self.db._fts_teardown_trash_step())
            self.assertFalse(self.db._fts_teardown_trash_step())

    def test_fts_trash_compound_key_teardown(self) -> None:
        self.db._conn.executescript(
            "CREATE TABLE fts_v22_trash_messages_fts_idx "
            "(segid INTEGER, term BLOB, PRIMARY KEY (segid, term));"
            "INSERT INTO fts_v22_trash_messages_fts_idx VALUES (1, X'01'), (1, X'02');"
        )
        with patch.object(self.db, "_FTS_REBUILD_CHUNK_ROWS", 1):
            self.assertTrue(self.db._fts_teardown_trash_step())
            count = self.db._conn.execute(
                "SELECT COUNT(*) FROM fts_v22_trash_messages_fts_idx"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_unrecognized_fts_trash_is_not_deleted(self) -> None:
        self.db._conn.execute(
            "CREATE TABLE fts_v22_trash_unexpected (id INTEGER PRIMARY KEY)"
        )
        self.db._conn.execute("INSERT INTO fts_v22_trash_unexpected VALUES (1)")
        with self.assertRaises(ValueError):
            self.db._fts_teardown_trash_step()
        self.assertEqual(
            self.db._conn.execute(
                "SELECT COUNT(*) FROM fts_v22_trash_unexpected"
            ).fetchone()[0],
            1,
        )

    def test_fts_trigger_migration_uses_bound_name_set(self) -> None:
        if not self.db._fts_enabled:
            self.skipTest("SQLite FTS5 unavailable")
        self.db._conn.executescript(
            "DROP TRIGGER messages_fts_update;"
            "CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN SELECT 1; END;"
        )
        before = SessionSchemaMixin._fts_trigger_count(self.db._conn)
        self.assertEqual(self.db._migrate_broad_fts_update_triggers(self.db._conn), 1)
        self.assertEqual(SessionSchemaMixin._fts_trigger_count(self.db._conn), before)
        sql = self.db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts_update'"
        ).fetchone()[0]
        self.assertIn("AFTER UPDATE OF", sql)

    def test_write_failure_rolls_back_and_preserves_original_error(self) -> None:
        def fail_after_insert(conn):
            conn.execute("INSERT INTO state_meta VALUES ('rollback-probe', 'value')")
            raise RuntimeError("write callback failed")

        with self.assertRaisesRegex(RuntimeError, "write callback failed"):
            self.db._execute_write(fail_after_insert)
        self.assertIsNone(self.db.get_meta("rollback-probe"))


if __name__ == "__main__":
    unittest.main()
