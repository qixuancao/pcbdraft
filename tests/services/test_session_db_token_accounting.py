"""Focused coverage for the SessionDB token-accounting mixin boundary."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_token_accounting


class SessionTokenAccountingMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_accounting_api_without_reverse_import(self) -> None:
        method_names = (
            "queue_token_counts",
            "flush_token_counts",
            "_token_writer_loop",
            "_apply_token_batch",
            "_coalesce_token_deltas",
            "_stop_token_writer",
            "_drain_token_queue_at_exit",
            "update_token_counts",
            "_record_model_usage",
        )
        self.assertTrue(
            issubclass(
                session_db.SessionDB,
                session_db_token_accounting.SessionTokenAccountingMixin,
            )
        )
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(session_db.SessionDB, name),
                    getattr(
                        session_db_token_accounting.SessionTokenAccountingMixin, name
                    ),
                )
        self.assertIs(session_db_token_accounting.logger, session_db.logger)

        tree = ast.parse(
            Path(session_db_token_accounting.__file__).read_text(encoding="utf-8")
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

    def test_queued_deltas_flush_to_real_sqlite(self) -> None:
        self.db.create_session("queued", "cli", model="seed-model")
        route = {
            "model": "served-model",
            "billing_provider": "provider-a",
            "billing_base_url": "https://provider.example/v1",
            "billing_mode": "api",
        }
        self.db.queue_token_counts(
            "queued",
            input_tokens=3,
            output_tokens=5,
            estimated_cost_usd=0.01,
            api_call_count=1,
            **route,
        )
        self.db.queue_token_counts(
            "queued",
            input_tokens=7,
            output_tokens=11,
            estimated_cost_usd=0.02,
            api_call_count=1,
            **route,
        )

        self.assertTrue(self.db.flush_token_counts())
        session = self.db.get_session("queued")
        self.assertEqual(session["input_tokens"], 10)
        self.assertEqual(session["output_tokens"], 16)
        self.assertEqual(session["api_call_count"], 2)
        self.assertAlmostEqual(session["estimated_cost_usd"], 0.03)

        with self.db._read_ctx() as conn:
            usage = conn.execute(
                "SELECT model, billing_provider, input_tokens, output_tokens, "
                "api_call_count, estimated_cost_usd "
                "FROM session_model_usage WHERE session_id = ?",
                ("queued",),
            ).fetchone()
        self.assertEqual(usage["model"], "served-model")
        self.assertEqual(usage["billing_provider"], "provider-a")
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 16)
        self.assertEqual(usage["api_call_count"], 2)
        self.assertAlmostEqual(usage["estimated_cost_usd"], 0.03)

    def test_direct_writes_preserve_increment_absolute_increment_order(self) -> None:
        self.db.create_session("ordered", "cli", model="served-model")
        route = {"model": "served-model", "billing_provider": "provider-a"}

        self.db.update_token_counts(
            "ordered", input_tokens=3, api_call_count=1, **route
        )
        self.db.update_token_counts(
            "ordered",
            input_tokens=20,
            api_call_count=4,
            absolute=True,
            **route,
        )
        self.db.update_token_counts(
            "ordered", input_tokens=2, api_call_count=1, **route
        )

        session = self.db.get_session("ordered")
        self.assertEqual(session["input_tokens"], 22)
        self.assertEqual(session["api_call_count"], 5)
        with self.db._read_ctx() as conn:
            usage = conn.execute(
                "SELECT input_tokens, api_call_count FROM session_model_usage "
                "WHERE session_id = ? AND task = ''",
                ("ordered",),
            ).fetchone()
        # Absolute summary writes cannot be attributed to a route. The two
        # incremental calls remain in their original order and total 3 + 2.
        self.assertEqual(usage["input_tokens"], 5)
        self.assertEqual(usage["api_call_count"], 2)


if __name__ == "__main__":
    unittest.main()
