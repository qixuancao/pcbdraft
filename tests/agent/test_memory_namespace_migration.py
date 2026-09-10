"""Offline regressions for existing implicit memory namespaces and user isolation."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.agent.legacy_compat import (
    NAMESPACE_ORIGIN_KEY,
    NAMESPACE_VERSION_KEY,
    effective_memory_namespaces,
    honcho_effective_namespaces,
    materialize_honcho_namespaces,
    read_skill_metadata,
)


class MemoryNamespaceMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "runtime"
        self.home.mkdir()
        environment = patch.dict(
            os.environ,
            {"HOME": str(self.root), "PCBDRAFT_RUNTIME_HOME": str(self.home)},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write_json(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_metadata_merges_fields_with_native_empty_values_winning(self):
        legacy = {"requires_tools": ["legacy"], "tags": ["old"], "config": [1]}
        for empty in (None, [], {}, "", False):
            result = read_skill_metadata(
                {"metadata": {"hermes": legacy, "pcbdraft": {"requires_tools": empty}}}
            )
            self.assertEqual(result, {**legacy, "requires_tools": empty})
        self.assertEqual(
            read_skill_metadata({"metadata": {"hermes": legacy, "pcbdraft": {}}}),
            legacy,
        )
        from pcbdraft.agent.skill_utils import extract_skill_conditions

        conditions = extract_skill_conditions(
            {
                "metadata": {
                    "hermes": {
                        "requires_tools": ["old"],
                        "requires_toolsets": ["files"],
                    },
                    "pcbdraft": {"requires_tools": []},
                }
            }
        )
        self.assertEqual(conditions["requires_tools"], [])
        self.assertEqual(conditions["requires_toolsets"], ["files"])

    def test_mem0_both_placeholders_isolate_gateway_users_but_keep_cli_identity(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider

        for placeholder in ("hermes-user", "pcbdraft-user"):
            self.write_json("mem0.json", {"user_id": placeholder, "agent_id": "old"})
            for user in ("alice", "bob", None):
                provider = Mem0MemoryProvider()
                with patch.object(provider, "_create_backend", return_value=None):
                    provider.initialize("test", user_id=user)
                self.assertEqual(
                    provider._read_filters(), {"user_id": user or placeholder}
                )
                self.assertEqual(provider._agent_id, "old")

    def test_mem0_implicit_old_user_isolates_gateway_and_keeps_old_cli(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider

        self.write_json("mem0.json", {"mode": "platform"})
        for user in ("alice", "bob", None):
            provider = Mem0MemoryProvider()
            with patch.object(provider, "_create_backend", return_value=None):
                provider.initialize("test", user_id=user)
            self.assertEqual(provider._user_id, user or "hermes-user")
            self.assertEqual(provider._agent_id, "hermes")
        self.write_json("mem0.json", {"user_id": "operator-selected"})
        provider = Mem0MemoryProvider()
        with patch.object(provider, "_create_backend", return_value=None):
            provider.initialize("test", user_id="alice")
        self.assertEqual(provider._user_id, "operator-selected")

    def test_versioned_native_and_unversioned_existing_defaults(self):
        cases = {
            "mem0": {"user_id": "hermes-user", "agent_id": "hermes"},
            "hindsight": {"bank_id": "hermes", "profile": "hermes"},
            "supermemory": {"container_tag": "hermes"},
            "openviking": {"agent": "hermes"},
        }
        for provider, defaults in cases.items():
            with self.subTest(provider=provider):
                old = effective_memory_namespaces(provider, {}, existing=True)
                self.assertEqual({key: old[key] for key in defaults}, defaults)
                self.assertEqual(old[NAMESPACE_ORIGIN_KEY], "legacy")
                self.assertEqual(effective_memory_namespaces(provider, old), old)
                native = effective_memory_namespaces(provider, {}, existing=False)
                for key, value in defaults.items():
                    self.assertEqual(native[key], value.replace("hermes", "pcbdraft"))
                self.assertEqual(native[NAMESPACE_VERSION_KEY], 1)
                self.assertEqual(native[NAMESPACE_ORIGIN_KEY], "native")
                versioned = effective_memory_namespaces(
                    provider, {NAMESPACE_VERSION_KEY: 1}, existing=True
                )
                self.assertEqual(versioned, native)

    def test_json_backend_reads_and_saves_freeze_old_implicit_ids(self):
        from pcbdraft.agent.memory_backends import hindsight, mem0, supermemory

        cases = (
            (
                "mem0.json",
                mem0._load_config,
                mem0.Mem0MemoryProvider().save_config,
                {"user_id": "hermes-user", "agent_id": "hermes"},
            ),
            (
                "hindsight/config.json",
                hindsight._load_config,
                hindsight.HindsightMemoryProvider().save_config,
                {"bank_id": "hermes", "profile": "hermes"},
            ),
            (
                "supermemory.json",
                lambda: supermemory._load_supermemory_config(str(self.home)),
                supermemory._save_supermemory_config,
                {"container_tag": "hermes"},
            ),
        )
        for relative, load, save, ids in cases:
            with self.subTest(provider=relative):
                path = self.write_json(relative, {"enabled": False})
                config = load()
                self.assertEqual({key: config[key] for key in ids}, ids)
                save({"enabled": False}, str(self.home))
                saved = json.loads(path.read_text())
                self.assertEqual({key: saved[key] for key in ids}, ids)
                self.assertEqual(saved[NAMESPACE_VERSION_KEY], 1)
                path.unlink()
                save({}, str(self.home))
                saved = json.loads(path.read_text())
                for key, value in ids.items():
                    self.assertEqual(saved[key], value.replace("hermes", "pcbdraft"))

    def test_unversioned_environment_only_backends_keep_old_defaults(self):
        from pcbdraft.agent.memory_backends import (
            hindsight,
            mem0,
            openviking,
            supermemory,
        )
        from pcbdraft.agent.memory_backends.honcho.client import HonchoClientConfig

        with patch.dict(
            os.environ,
            {
                "MEM0_HOST": "https://example.invalid",
                "HINDSIGHT_MODE": "local_external",
                "SUPERMEMORY_BASE_URL": "https://example.invalid",
                "OPENVIKING_ENDPOINT": "https://example.invalid",
                "HONCHO_BASE_URL": "https://example.invalid",
            },
        ):
            self.assertEqual(mem0._load_config()["user_id"], "hermes-user")
            self.assertEqual(hindsight._load_config()["profile"], "hermes")
            self.assertEqual(hindsight._load_config()["bank_id"], "hermes")
            self.assertEqual(
                supermemory._load_supermemory_config(str(self.home))["container_tag"],
                "hermes",
            )
            with patch.object(
                openviking, "_normalize_openviking_url", side_effect=lambda value: value
            ):
                self.assertEqual(
                    openviking._resolve_connection_settings({})["agent"], "hermes"
                )
            config = HonchoClientConfig.from_env(host="pcbdraft_work")
            self.assertEqual(
                (config.workspace_id, config.ai_peer), ("hermes", "hermes_work")
            )

    def test_hindsight_legacy_shared_config_uses_old_bank_and_profile(self):
        from pcbdraft.agent.memory_backends.hindsight import (
            HindsightMemoryProvider,
            _load_config,
        )

        shared = self.root / ".hindsight" / "config.json"
        shared.parent.mkdir()
        shared.write_text('{"mode": "local_embedded"}', encoding="utf-8")
        config = _load_config()
        self.assertEqual((config["bank_id"], config["profile"]), ("hermes", "hermes"))
        HindsightMemoryProvider().save_config({}, str(self.home))
        saved = json.loads((self.home / "hindsight" / "config.json").read_text())
        self.assertEqual((saved["bank_id"], saved["profile"]), ("hermes", "hermes"))
        self.assertEqual(json.loads(shared.read_text()), {"mode": "local_embedded"})

    def test_openviking_saved_and_linked_configs_preserve_implicit_agent(self):
        from pcbdraft.agent.memory_backends import openviking

        with (
            patch(
                "pcbdraft.model.configuration.load_config_readonly",
                return_value={
                    "memory": {"openviking": {"endpoint": "https://example.invalid"}},
                },
            ),
            patch.object(
                openviking, "_normalize_openviking_url", side_effect=lambda value: value
            ),
        ):
            raw = openviking._load_pcbdraft_openviking_config()
            self.assertEqual(
                openviking._resolve_connection_settings(raw)["agent"], "hermes"
            )
            raw["agent"] = "explicit-peer"
            self.assertEqual(
                openviking._resolve_connection_settings(raw)["agent"], "explicit-peer"
            )
        linked = self.write_json("ovcli.json", {"url": "https://example.invalid"})
        with patch.object(
            openviking, "_normalize_openviking_url", side_effect=lambda value: value
        ):
            config = openviking._resolve_connection_settings(
                {"use_ovcli_config": True, "ovcli_config_path": str(linked)}
            )
        self.assertEqual(config["agent"], "hermes")

    def test_retaindb_uses_pre_migration_basename_rules(self):
        from pcbdraft.agent.memory_backends import retaindb

        for name, project in (
            (".hermes", "default"),
            ("work", "hermes-work"),
            ("hermes", "hermes-hermes"),
            ("runtime", "hermes-runtime"),
        ):
            target = self.root / name
            with (
                patch(
                    "pcbdraft.model.configuration.load_config_readonly",
                    return_value={
                        "memory": {"retaindb": {"base_url": "https://example.invalid"}},
                    },
                ),
                patch.object(retaindb, "_WriteQueue"),
            ):
                provider = retaindb.RetainDBMemoryProvider()
                provider.initialize("test", runtime_home=str(target))
            self.assertEqual(provider._client.project, project)
            self.assertEqual(provider._agent_id, "hermes")
        (self.root / "runtime-migration.json").write_text(
            json.dumps(
                {
                    "outcome": "migrated",
                    "source_directory": "hermes",
                    "target_directory": "runtime",
                }
            ),
            encoding="utf-8",
        )
        migrated = effective_memory_namespaces(
            "retaindb", {}, existing=True, runtime_home=self.home
        )
        self.assertEqual(migrated["project"], "hermes-hermes")
        with (
            patch(
                "pcbdraft.model.configuration.load_config_readonly",
                return_value={"memory": {"retaindb": {}}},
            ),
            patch.object(retaindb, "_WriteQueue"),
        ):
            provider = retaindb.RetainDBMemoryProvider()
            provider.initialize("no-explicit-home")
        self.assertEqual(provider._client.project, "default")

    def test_honcho_dot_block_is_not_the_effective_identity(self):
        from pcbdraft.agent.memory_backends.honcho.client import HonchoClientConfig

        for key in ("hermes.work", "hermes_work"):
            path = self.write_json("honcho.json", {"hosts": {key: {"enabled": False}}})
            config = HonchoClientConfig.from_global_config(
                host="pcbdraft_work", config_path=path
            )
            self.assertEqual(config.host, "hermes_work")
            self.assertEqual(
                (config.workspace_id, config.ai_peer), ("hermes_work", "hermes_work")
            )
            self.assertFalse(config.enabled)
        path = self.write_json("honcho.json", {"baseUrl": "https://example.invalid"})
        config = HonchoClientConfig.from_global_config(
            host="pcbdraft", config_path=path
        )
        self.assertEqual((config.workspace_id, config.ai_peer), ("hermes", "hermes"))

    def test_honcho_native_save_and_legacy_materialization(self):
        from pcbdraft.agent.memory_backends.honcho import HonchoMemoryProvider

        HonchoMemoryProvider().save_config({}, str(self.home))
        native = json.loads((self.home / "honcho.json").read_text())
        self.assertEqual(
            native["hosts"]["pcbdraft"], {"workspace": "pcbdraft", "aiPeer": "pcbdraft"}
        )
        old = {
            "hosts": {
                "hermes.work": {"enabled": False, "oauth": {"refreshToken": "fixture"}}
            }
        }
        frozen = materialize_honcho_namespaces(old, existing=True)
        block = frozen["hosts"]["hermes.work"]
        self.assertEqual(
            (block["workspace"], block["aiPeer"]), ("hermes_work", "hermes_work")
        )
        self.assertFalse(block["enabled"])
        self.assertEqual(block["oauth"], old["hosts"]["hermes.work"]["oauth"])
        self.assertEqual(
            honcho_effective_namespaces(frozen, "pcbdraft_work"),
            ("hermes_work", "hermes_work", "hermes_work"),
        )
        flat = {"baseUrl": "https://example.invalid", "enabled": False}
        frozen_flat = materialize_honcho_namespaces(flat, existing=True)
        for host in ("pcbdraft", "pcbdraft_work", "pcbdraft_other"):
            self.assertEqual(
                honcho_effective_namespaces(flat, host, existing=True),
                honcho_effective_namespaces(frozen_flat, host, existing=True),
            )

    def test_honcho_save_retains_shared_source_namespaces(self):
        from pcbdraft.agent.memory_backends.honcho import HonchoMemoryProvider
        from pcbdraft.agent.memory_backends.honcho.client import HonchoClientConfig

        shared = self.root / ".honcho" / "config.json"
        shared.parent.mkdir()
        shared.write_text('{"enabled": false}', encoding="utf-8")
        HonchoMemoryProvider().save_config({}, str(self.home))
        path = self.home / "honcho.json"
        config = HonchoClientConfig.from_global_config(
            host="pcbdraft_work", config_path=path
        )
        self.assertEqual(
            (config.workspace_id, config.ai_peer), ("hermes_work", "hermes_work")
        )
        self.assertFalse(config.enabled)
        self.assertEqual(json.loads(shared.read_text()), {"enabled": False})

    def test_explicit_env_mem0_identity_is_preserved_by_setup(self):
        from pcbdraft.agent.memory_backends.mem0._setup import _existing_memory_identity

        with patch.dict(
            os.environ, {"MEM0_USER_ID": "chosen", "MEM0_AGENT_ID": "old-agent"}
        ):
            self.assertEqual(
                _existing_memory_identity(str(self.home)),
                {"user_id": "chosen", "agent_id": "old-agent"},
            )

    def test_supermemory_new_setup_freezes_native_id_before_new_credentials(self):
        from pcbdraft.agent.memory_backends import supermemory

        with (
            patch(
                "pcbdraft.interfaces.tui.memory_setup._prompt", return_value="fixture"
            ),
            patch("pcbdraft.model.configuration.save_config"),
            patch.object(supermemory, "_probe_supermemory_connection", return_value={}),
            patch.object(
                supermemory, "_format_connection_summary", return_value="offline"
            ),
            patch("builtins.print"),
        ):
            supermemory.SupermemoryMemoryProvider().post_setup(str(self.home), {})
        saved = json.loads((self.home / "supermemory.json").read_text())
        self.assertEqual(saved["container_tag"], "pcbdraft")
        self.assertEqual(saved[NAMESPACE_ORIGIN_KEY], "native")

    def test_honcho_clone_and_both_sync_paths_never_shadow_legacy_blocks(self):
        from pcbdraft.agent.memory_backends.honcho import cli

        for alias in ("hermes.work", "hermes_work", "pcbdraft.work"):
            for block in (
                {},
                {"enabled": False},
                {"workspace": "old", "oauth": {"refreshToken": "fixture"}},
            ):
                config = {"hosts": {"hermes": {"workspace": "shared"}, alias: block}}
                with (
                    patch.object(cli, "_read_config", return_value=config),
                    patch.object(cli, "_write_config") as write,
                    patch.object(cli, "_ensure_peer_exists") as connect,
                    patch(
                        "pcbdraft.interfaces.tui.profiles.list_profiles",
                        return_value=[SimpleNamespace(name="work")],
                    ),
                    patch("builtins.print"),
                ):
                    self.assertFalse(cli.clone_honcho_for_profile("work"))
                    cli.cmd_sync(SimpleNamespace())
                    self.assertEqual(cli.sync_honcho_profiles_quiet(), 0)
                    write.assert_not_called()
                    connect.assert_not_called()
                self.assertNotIn("pcbdraft_work", config["hosts"])

    def test_mem0_oss_profile_paths(self):
        from pcbdraft.agent.memory_backends.mem0 import _oss_providers, _setup

        with patch(
            "pcbdraft.core.runtime_environment.get_runtime_home",
            return_value=self.root / "A",
        ) as resolve:
            importlib.reload(_oss_providers)
            resolve.assert_not_called()
        for name in ("A", "B"):
            with patch(
                "pcbdraft.core.runtime_environment.get_runtime_home",
                return_value=self.root / name,
            ):
                config, _ = _setup.build_oss_config({})
                self.assertEqual(
                    config["vector_store"]["config"]["path"],
                    str(self.root / name / "mem0_qdrant"),
                )
        config, _ = _setup.build_oss_config({}, runtime_home=str(self.home))
        self.assertEqual(
            config["vector_store"]["config"]["path"], str(self.home / "mem0_qdrant")
        )
        self.write_json(
            "mem0.json",
            {
                "oss": {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {"path": "/existing/qdrant"},
                    }
                }
            },
        )
        config, _ = _setup.build_oss_config({}, runtime_home=str(self.home))
        self.assertEqual(config["vector_store"]["config"]["path"], "/existing/qdrant")
        config, _ = _setup.build_oss_config(
            {"oss_vector_path": "/explicit/qdrant"}, runtime_home=str(self.home)
        )
        self.assertEqual(config["vector_store"]["config"]["path"], "/explicit/qdrant")

    def test_yaml_backend_saves_materialize_ids_for_old_and_new_configs(self):
        import yaml

        from pcbdraft.agent.memory_backends.openviking import OpenVikingMemoryProvider
        from pcbdraft.agent.memory_backends.retaindb import RetainDBMemoryProvider

        for old in (True, False):
            config = {"memory": {"openviking": {}} if old else {}}
            with (
                patch("pcbdraft.model.configuration.load_config", return_value=config),
                patch("pcbdraft.model.configuration.save_config") as save,
            ):
                OpenVikingMemoryProvider().save_config({}, str(self.home))
            saved = save.call_args.args[0]["memory"]["openviking"]
            self.assertEqual(saved["agent"], "hermes" if old else "pcbdraft")
            self.assertEqual(saved[NAMESPACE_VERSION_KEY], 1)

            path = self.home / "config.yaml"
            path.write_text(
                yaml.safe_dump({"memory": {"retaindb": {}} if old else {}}),
                encoding="utf-8",
            )
            RetainDBMemoryProvider().save_config({}, str(self.home))
            saved = yaml.safe_load(path.read_text())["memory"]["retaindb"]
            self.assertEqual(saved["project"], "hermes-runtime" if old else "default")
            self.assertEqual(saved["agent_id"], "hermes" if old else "pcbdraft")
            self.assertEqual(saved[NAMESPACE_VERSION_KEY], 1)

    def test_honcho_oauth_reads_and_writes_the_same_legacy_alias_block(self):
        from pcbdraft.agent.legacy_compat import HONCHO_OAUTH_CLIENT_ID
        from pcbdraft.agent.memory_backends.honcho import oauth

        credential = oauth.OAuthCredential(
            oauth.ACCESS_TOKEN_PREFIX + "offline-fixture",
            oauth.REFRESH_TOKEN_PREFIX + "offline-fixture",
            1000,
            HONCHO_OAUTH_CLIENT_ID,
            "https://example.invalid/token",
        )
        path = self.write_json(
            "honcho.json", {"hosts": {"hermes.work": {"enabled": False}}}
        )
        with (
            patch.dict(oauth._expiry_cache),
            patch.object(
                oauth, "_http_post_form", side_effect=AssertionError("network")
            ),
        ):
            oauth._persist_credential(path, "hermes_work", credential)
            raw = json.loads(path.read_text())
            self.assertEqual(list(raw["hosts"]), ["hermes.work"])
            block = raw["hosts"]["hermes.work"]
            self.assertFalse(block["enabled"])
            self.assertEqual(block["workspace"], "hermes_work")
            self.assertEqual(block["aiPeer"], "hermes_work")
            value, refreshed = oauth.ensure_fresh_token(
                path, "hermes_work", raw=raw, now=0
            )
            self.assertEqual(value, credential.access_token)
            self.assertFalse(refreshed)


if __name__ == "__main__":
    unittest.main()
