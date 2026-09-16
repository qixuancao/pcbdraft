"""Focused coverage for the SessionDB metadata mixin boundary."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from pcbdraft.agent.session_activity import ActivityProvenance
from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_metadata


class SessionMetadataMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session(
            "session-1",
            "cli",
            model="old-model",
            model_config={
                "_branched_from": "parent-session",
                "browser_model_lock": {"confirmed": True},
            },
        )

    def test_session_db_inherits_metadata_api_and_compatibility_exports(self) -> None:
        method_names = (
            "touch_session_activity",
            "clear_session_activity_labels",
            "get_session_activity",
            "update_session_meta",
            "update_system_prompt",
            "update_session_model",
            "_merge_model_config_json",
            "patch_session_model_config",
            "get_session_model_config_value",
            "update_session_runtime_lock",
            "set_session_yolo",
            "session_yolo_enabled",
            "session_gateway_runtime",
            "update_session_billing_route",
        )
        self.assertTrue(
            issubclass(session_db.SessionDB, session_db_metadata.SessionMetadataMixin)
        )
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(session_db.SessionDB, name),
                    getattr(session_db_metadata.SessionMetadataMixin, name),
                )
        self.assertIs(
            session_db._MODEL_CONFIG_ROW_MISSING,
            session_db_metadata._MODEL_CONFIG_ROW_MISSING,
        )
        self.assertIs(
            session_db._BARE_BILLING_PROVIDERS,
            session_db_metadata._BARE_BILLING_PROVIDERS,
        )
        self.assertIs(session_db.ActivityProvenance, ActivityProvenance)
        self.assertIs(session_db_metadata.logger, session_db.logger)

        tree = ast.parse(Path(session_db_metadata.__file__).read_text(encoding="utf-8"))
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

    def test_activity_timestamp_is_monotonic_and_labels_can_be_cleared(self) -> None:
        self.db.touch_session_activity(
            "session-1",
            100.0,
            description="compressing context",
            provenance=ActivityProvenance.AGENT_COMPRESSION,
        )
        self.db.touch_session_activity(
            "session-1",
            50.0,
            description="stale update",
            provenance=ActivityProvenance.UNKNOWN,
        )

        activity = self.db.get_session_activity("session-1")
        self.assertEqual(activity["last_activity_at"], 100.0)
        self.assertEqual(activity["description"], "compressing context")
        self.assertEqual(
            activity["provenance"], ActivityProvenance.AGENT_COMPRESSION.value
        )

        self.db.clear_session_activity_labels("session-1")
        cleared = self.db.get_session_activity("session-1")
        self.assertEqual(cleared["last_activity_at"], 100.0)
        self.assertEqual(cleared["description"], "")
        self.assertEqual(cleared["provenance"], ActivityProvenance.UNKNOWN.value)

    def test_metadata_updates_preserve_lineage_and_runtime_behavior(self) -> None:
        self.db.update_system_prompt("session-1", "model-specific prompt")
        self.db.update_session_model("session-1", "new-model", "new-provider")
        model_config = json.loads(self.db.get_session("session-1")["model_config"])
        self.assertEqual(model_config["_branched_from"], "parent-session")
        self.assertEqual(model_config["model"], "new-model")
        self.assertEqual(model_config["provider"], "new-provider")
        self.assertNotIn("browser_model_lock", model_config)

        self.db.set_session_yolo("session-1", True)
        self.db.update_session_runtime_lock(
            "session-1",
            model="locked-model",
            provider="locked-provider",
            model_options={"temperature": 0},
            route_source="test",
            confirmed=True,
        )
        session = self.db.get_session("session-1")
        self.assertTrue(self.db.session_yolo_enabled(session))
        self.assertEqual(
            self.db.get_session_model_config_value("session-1", "browser_model_lock")[
                "provider"
            ],
            "locked-provider",
        )

        self.db.update_session_billing_route(
            "session-1",
            provider="billed-provider",
            base_url="https://provider.example/v1",
            billing_mode="api",
        )
        session = self.db.get_session("session-1")
        self.assertEqual(session["billing_provider"], "billed-provider")
        self.assertEqual(session["billing_base_url"], "https://provider.example/v1")
        self.assertEqual(session["billing_mode"], "api")
        self.assertEqual(
            self.db.session_gateway_runtime(session)["provider"], "new-provider"
        )
        self.assertIsNone(session["system_prompt_hash"])

        self.db.update_session_meta(
            "session-1", json.dumps({"manual": True}), model=None
        )
        session = self.db.get_session("session-1")
        self.assertEqual(session["model"], "locked-model")
        self.assertEqual(json.loads(session["model_config"]), {"manual": True})


if __name__ == "__main__":
    unittest.main()
