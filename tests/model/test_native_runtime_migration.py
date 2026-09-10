"""Offline regression coverage for native runtime and owned-source migration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pcbdraft.core.runtime_environment import (
    get_runtime_home,
    reset_runtime_home_override,
    set_runtime_home_override,
)
from pcbdraft.model import (
    anthropic_adapter,
    auth,
    credential_pool,
    credential_sources,
    env_loader,
    model_catalog,
)
from pcbdraft.model.credential_persistence import (
    is_borrowed_credential_source,
    sanitize_borrowed_credential_payload,
)


class OwnedSourceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        path = patch.object(
            auth, "_auth_file_path", return_value=self.home / "auth.json"
        )
        path.start()
        self.addCleanup(path.stop)

    @staticmethod
    def payload(source: str) -> dict:
        return {
            "id": "owned-fixture",
            "source": source,
            "access_token": "TEST_ACCESS",
            "refresh_token": "TEST_REFRESH",
            "auth_type": "oauth",
            "expires_at_ms": 0,
        }

    def test_legacy_owned_pkce_survives_read_and_native_write(self) -> None:
        for source, native in (
            ("hermes_pkce", "pcbdraft_pkce"),
            ("manual:hermes_pkce", "manual:pcbdraft_pkce"),
        ):
            with self.subTest(source=source):
                path = self.home / "auth.json"
                original = json.dumps(
                    {
                        "providers": {},
                        "credential_pool": {"anthropic": [self.payload(source)]},
                    }
                )
                path.write_text(original)
                store = auth._load_auth_store()
                self.assertEqual(path.read_text(), original, "reading must not write")
                entry = store["credential_pool"]["anthropic"][0]
                self.assertEqual(entry["source"], native)
                self.assertEqual(entry["refresh_token"], "TEST_REFRESH")
                auth._save_auth_store(store)
                saved = json.loads(path.read_text())["credential_pool"]["anthropic"][0]
                self.assertEqual(saved["source"], native)
                self.assertEqual(saved["access_token"], "TEST_ACCESS")
                self.assertEqual(saved["refresh_token"], "TEST_REFRESH")

    def test_native_and_legacy_pkce_do_not_grant_other_providers_ownership(
        self,
    ) -> None:
        for source in ("hermes_pkce", "pcbdraft_pkce"):
            with self.subTest(source=source):
                self.assertFalse(is_borrowed_credential_source(source, "anthropic"))
                self.assertTrue(is_borrowed_credential_source(source, "openai-api"))
                saved = sanitize_borrowed_credential_payload(
                    self.payload(source), "openai-api"
                )
                self.assertNotIn("access_token", saved)
                self.assertNotIn("refresh_token", saved)

    def test_final_store_boundary_keeps_borrowed_sources_reference_only(self) -> None:
        for source in (
            "env:ANTHROPIC_API_KEY",
            "claude_code",
            "bitwarden",
            "hermes-auth-store",
            "pcbdraft-auth-store",
            "future-secret-provider",
        ):
            with self.subTest(source=source):
                payload = self.payload(source)
                payload["agent_key"] = "TEST_AGENT_KEY"
                auth._save_auth_store(
                    {"providers": {}, "credential_pool": {"anthropic": [payload]}}
                )
                disk = json.loads((self.home / "auth.json").read_text())
                saved = disk["credential_pool"]["anthropic"][0]
                for key in ("access_token", "refresh_token", "agent_key"):
                    self.assertNotIn(key, saved)
                self.assertTrue(saved["secret_fingerprint"].startswith("sha256:"))

    def test_old_suppression_blocks_native_seed_and_unsuppresses_both_labels(
        self,
    ) -> None:
        for markers in (
            ["hermes_pkce"],
            {"hermes_pkce": True},
            ["hermes_pkce", "pcbdraft_pkce"],
        ):
            with self.subTest(markers=markers):
                (self.home / "auth.json").write_text(
                    json.dumps(
                        {
                            "providers": {},
                            "suppressed_sources": {"anthropic": markers},
                        }
                    )
                )
                self.assertTrue(auth.is_source_suppressed("anthropic", "pcbdraft_pkce"))
                with (
                    patch.object(
                        auth, "is_provider_explicitly_configured", return_value=True
                    ),
                    patch.object(credential_pool, "load_env", return_value={}),
                    patch.object(credential_pool, "_get_secret", return_value=""),
                    patch.object(
                        anthropic_adapter,
                        "read_pcbdraft_oauth_credentials",
                        return_value={
                            "accessToken": "TEST_ACCESS",
                            "refreshToken": "TEST_REFRESH",
                        },
                    ),
                ):
                    entries = []
                    credential_pool._seed_from_singletons("anthropic", entries)
                    self.assertEqual(entries, [])
                self.assertTrue(
                    auth.unsuppress_credential_source("anthropic", "pcbdraft_pkce")
                )
                self.assertFalse(auth.is_source_suppressed("anthropic", "hermes_pkce"))
                auth.suppress_credential_source("anthropic", "hermes_pkce")
                disk = json.loads((self.home / "auth.json").read_text())
                self.assertEqual(
                    disk["suppressed_sources"]["anthropic"], ["pcbdraft_pkce"]
                )

    def test_legacy_delete_dispatch_deletes_only_owned_singleton(self) -> None:
        owned = self.home / ".anthropic_oauth.json"
        external = self.home / ".claude" / ".credentials.json"
        external.parent.mkdir()
        external.write_text("TEST_EXTERNAL_STORE")
        owned.write_text("TEST_OWNED_STORE")
        step = credential_sources.find_removal_step("anthropic", "hermes_pkce")
        self.assertIsNotNone(step)
        with patch(
            "pcbdraft.core.runtime_environment.get_runtime_home", return_value=self.home
        ):
            result = step.remove_fn("anthropic", Mock(source="hermes_pkce"))
        self.assertTrue(result.suppress)
        self.assertFalse(owned.exists())
        self.assertEqual(external.read_text(), "TEST_EXTERNAL_STORE")
        self.assertIsNone(
            credential_sources.find_removal_step("openai-api", "hermes_pkce")
        )

    def test_manual_legacy_pkce_removal_does_not_delete_singleton(self) -> None:
        owned = self.home / ".anthropic_oauth.json"
        owned.write_text("TEST_OWNED_STORE")
        step = credential_sources.find_removal_step("anthropic", "manual:hermes_pkce")
        # Manual grants have no external source cleanup or suppression step.
        self.assertIsNone(step)
        self.assertTrue(owned.exists())

    def test_legacy_pkce_refresh_preserves_json_protocol_and_native_source(
        self,
    ) -> None:
        for source in ("hermes_pkce", "manual:hermes_pkce", "pcbdraft_pkce"):
            with self.subTest(source=source):
                entry = credential_pool.PooledCredential.from_dict(
                    "anthropic", self.payload(source)
                )
                with patch.object(
                    credential_pool, "get_pool_strategy", return_value="round_robin"
                ):
                    pool = credential_pool.CredentialPool("anthropic", [entry])
                with (
                    patch.object(
                        anthropic_adapter,
                        "refresh_anthropic_oauth_pure",
                        return_value={
                            "access_token": "TEST_ROTATED_ACCESS",
                            "refresh_token": "TEST_ROTATED_REFRESH",
                            "expires_at_ms": 9999999999999,
                        },
                    ) as refresh,
                    patch.object(pool, "_persist"),
                    patch.object(pool, "_sync_device_code_entry_to_auth_store"),
                ):
                    updated = pool._refresh_entry_impl(entry, force=True)
                refresh.assert_called_once_with("TEST_REFRESH", use_json=True)
                self.assertIsNotNone(updated)
                self.assertNotIn("hermes", updated.source)
                self.assertEqual(
                    updated.to_dict()["refresh_token"], "TEST_ROTATED_REFRESH"
                )


class NativeRuntimeCatalogTests(unittest.TestCase):
    def test_dotenv_uses_core_home_and_ignores_old_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                patch.dict(os.environ, {"PCBDRAFT_HERMES_HOME": "/ignored/legacy"}),
                patch(
                    "pcbdraft.core.runtime_environment.get_process_runtime_home",
                    return_value=home,
                ) as core_home,
                patch.object(env_loader, "_apply_managed_env"),
                patch.object(env_loader, "_reapply_terminal_config_bridge"),
            ):
                self.assertEqual(
                    env_loader.load_pcbdraft_dotenv(load_external_secrets=False), []
                )
            core_home.assert_called()

    def test_dotenv_process_home_ignores_task_home_unless_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_a = Path(tmp) / "a"
            home_b = Path(tmp) / "b"
            for home, label in ((home_a, "A"), (home_b, "B")):
                home.mkdir()
                (home / ".env").write_text(
                    f"OPENAI_API_KEY=TEST_ONLY_{label}_API_KEY\n", encoding="utf-8"
                )
            with (
                patch.dict(
                    os.environ,
                    {"PCBDRAFT_RUNTIME_HOME": str(home_a)},
                    clear=True,
                ),
                patch.object(env_loader, "_apply_managed_env"),
                patch.object(env_loader, "_reapply_terminal_config_bridge"),
            ):
                token = set_runtime_home_override(home_b)
                try:
                    self.assertEqual(get_runtime_home(), home_b)
                    loaded = env_loader.load_pcbdraft_dotenv(
                        load_external_secrets=False
                    )
                    self.assertEqual(loaded, [home_a / ".env"])
                    self.assertEqual(
                        os.environ["OPENAI_API_KEY"], "TEST_ONLY_A_API_KEY"
                    )
                    self.assertEqual(get_runtime_home(), home_b)

                    loaded = env_loader.load_pcbdraft_dotenv(
                        runtime_home=home_b, load_external_secrets=False
                    )
                    self.assertEqual(loaded, [home_b / ".env"])
                    self.assertEqual(
                        os.environ["OPENAI_API_KEY"], "TEST_ONLY_B_API_KEY"
                    )
                    self.assertEqual(os.environ["PCBDRAFT_RUNTIME_HOME"], str(home_a))

                    env_loader.load_pcbdraft_dotenv(load_external_secrets=False)
                    self.assertEqual(
                        os.environ["OPENAI_API_KEY"], "TEST_ONLY_A_API_KEY"
                    )
                    self.assertEqual(get_runtime_home(), home_b)
                finally:
                    reset_runtime_home_override(token)

    def test_secret_config_reader_does_not_mix_process_and_task_homes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_a = Path(tmp) / "a"
            home_b = Path(tmp) / "b"
            for home, label in ((home_a, "A"), (home_b, "B")):
                home.mkdir()
                (home / "config.yaml").write_text(
                    f"secrets:\n  fixture: TEST_ONLY_{label}_SECRET\n",
                    encoding="utf-8",
                )
            with patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(home_a)}):
                token = set_runtime_home_override(home_b)
                try:
                    self.assertEqual(
                        env_loader._load_secrets_config(home_a),
                        {"fixture": "TEST_ONLY_A_SECRET"},
                    )
                    self.assertEqual(
                        env_loader._load_secrets_config(home_b),
                        {"fixture": "TEST_ONLY_B_SECRET"},
                    )
                finally:
                    reset_runtime_home_override(token)

    def test_terminal_bridge_reads_process_home_and_restores_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_a = Path(tmp) / "a"
            home_b = Path(tmp) / "b"
            for failure in (False, True):
                with (
                    self.subTest(failure=failure),
                    patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(home_a)}),
                ):
                    token = set_runtime_home_override(home_b)
                    try:
                        observed = []

                        def bridge(*, env, _observed=observed, _failure=failure):
                            _observed.append((env, get_runtime_home()))
                            if _failure:
                                raise ValueError("TEST_ONLY_CONFIG_FAILURE")

                        with patch(
                            "pcbdraft.model.configuration.apply_terminal_config_to_env",
                            side_effect=bridge,
                        ) as apply:
                            env_loader._reapply_terminal_config_bridge(home_a)
                            apply.assert_called_once_with(env=None)
                        self.assertEqual(observed, [(None, home_a)])
                        self.assertEqual(get_runtime_home(), home_b)
                    finally:
                        reset_runtime_home_override(token)

    def test_catalog_is_offline_by_default_even_with_old_cached_manifest(self) -> None:
        stale = {
            "version": 1,
            "providers": {"nous": {"models": [{"id": "obsolete", "default": True}]}},
        }
        for config in (
            {},
            {"model_catalog": {"enabled": True}},
            {"model_catalog": {"url": "https://catalog.example/models.json"}},
        ):
            with (
                self.subTest(config=config),
                patch(
                    "pcbdraft.model.configuration.read_raw_config", return_value=config
                ),
                patch.object(model_catalog, "_catalog_cache", stale),
                patch.object(model_catalog, "_read_disk_cache") as disk,
                patch.object(model_catalog, "_fetch_manifest") as network,
            ):
                self.assertEqual(model_catalog.get_catalog(force_refresh=True), {})
                self.assertIsNone(model_catalog.get_default_model_from_cache("nous"))
                self.assertIsNone(model_catalog.get_curated_nous_models())
                disk.assert_not_called()
                network.assert_not_called()

    def test_explicit_catalog_fetch_does_not_fall_back_to_another_service(self) -> None:
        url = "https://catalog.example/models.json"
        with (
            patch(
                "pcbdraft.model.configuration.read_raw_config",
                return_value={
                    "model_catalog": {"enabled": True, "url": url},
                },
            ),
            patch.object(model_catalog, "_read_disk_cache", return_value=(None, 0)),
            patch.object(
                model_catalog, "_fetch_manifest", return_value=None
            ) as network,
        ):
            self.assertEqual(model_catalog.get_catalog(force_refresh=True), {})
        network.assert_called_once_with(url, model_catalog.DEFAULT_FETCH_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
