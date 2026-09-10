"""Regression coverage for namespace sources crossing provider save boundaries."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from pcbdraft.agent import secret_scope
from pcbdraft.agent.legacy_compat import NAMESPACE_ORIGIN_KEY
from pcbdraft.core.runtime_environment import (
    get_runtime_home,
    reset_runtime_home_override,
    set_runtime_home_override,
)


class NamespaceRoundtripTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "profiles" / "work"
        self.home.mkdir(parents=True)
        environment = patch.dict(
            os.environ,
            {"HOME": str(self.root), "PCBDRAFT_RUNTIME_HOME": str(self.home)},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        token = set_runtime_home_override(self.home)
        self.addCleanup(reset_runtime_home_override, token)
        token = secret_scope.set_secret_scope({})
        self.addCleanup(secret_scope.reset_secret_scope, token)
        multiplex = patch.object(secret_scope, "_MULTIPLEX_ACTIVE", True)
        multiplex.start()
        self.addCleanup(multiplex.stop)

    def scoped(self, mapping):
        token = secret_scope.set_secret_scope(mapping)
        self.addCleanup(secret_scope.reset_secret_scope, token)

    def test_mem0_scoped_key_no_json_roundtrip(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider, _load_config

        self.scoped({"MEM0_API_KEY": "scoped-fixture"})
        before = _load_config()
        self.assertEqual(before["user_id"], "hermes-user")
        Mem0MemoryProvider().save_config({}, str(self.home))
        after = _load_config()
        for key in ("user_id", "agent_id", NAMESPACE_ORIGIN_KEY):
            self.assertEqual(after[key], before[key])
        saved = (self.home / "mem0.json").read_text()
        self.assertNotIn("scoped-fixture", saved)

    def test_supermemory_scoped_key_no_json_roundtrip(self):
        from pcbdraft.agent.memory_backends.supermemory import (
            SupermemoryMemoryProvider,
            _load_supermemory_config,
        )

        self.scoped({"SUPERMEMORY_API_KEY": "scoped-fixture"})
        before = _load_supermemory_config(str(self.home))
        self.assertEqual(before["container_tag"], "hermes")
        SupermemoryMemoryProvider().save_config({}, str(self.home))
        after = _load_supermemory_config(str(self.home))
        for key in ("container_tag", NAMESPACE_ORIGIN_KEY):
            self.assertEqual(after[key], before[key])
        self.assertNotIn("scoped-fixture", (self.home / "supermemory.json").read_text())

    def test_saves_resolve_target_profile_without_borrowing_active_scope(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider
        from pcbdraft.agent.memory_backends.supermemory import SupermemoryMemoryProvider

        active = {
            "MEM0_API_KEY": "active-fixture",
            "SUPERMEMORY_API_KEY": "active-fixture",
        }
        self.scoped(active)
        for existing in (False, True):
            with self.subTest(existing=existing):
                target = self.root / f"target-{existing}"
                target.mkdir()
                if existing:
                    (target / ".env").write_text(
                        "MEM0_API_KEY=target-fixture\n"
                        "SUPERMEMORY_API_KEY=target-fixture\n",
                        encoding="utf-8",
                    )
                Mem0MemoryProvider().save_config({}, str(target))
                SupermemoryMemoryProvider().save_config({}, str(target))
                user = json.loads((target / "mem0.json").read_text())["user_id"]
                tag = json.loads((target / "supermemory.json").read_text())[
                    "container_tag"
                ]
                self.assertEqual(user, "hermes-user" if existing else "pcbdraft-user")
                self.assertEqual(tag, "hermes" if existing else "pcbdraft")
                self.assertEqual(secret_scope.current_secret_scope(), active)
                self.assertEqual(get_runtime_home(), self.home)

    def test_honcho_env_only_named_profile_roundtrip(self):
        from pcbdraft.agent.memory_backends.honcho import HonchoMemoryProvider
        from pcbdraft.agent.memory_backends.honcho.client import HonchoClientConfig

        self.scoped({"HONCHO_API_KEY": "scoped-fixture"})
        before = HonchoClientConfig.from_global_config(host="pcbdraft_work")
        self.assertEqual(
            (before.workspace_id, before.ai_peer), ("hermes", "hermes_work")
        )
        provider = HonchoMemoryProvider()
        provider.save_config({}, str(self.home))
        for host in ("pcbdraft_work", "pcbdraft_other"):
            after = HonchoClientConfig.from_global_config(host=host)
            self.assertEqual(after.workspace_id, "hermes")
            self.assertEqual(after.ai_peer, host.replace("pcbdraft", "hermes"))
        provider.save_config({"enabled": False}, str(self.home))
        after = HonchoClientConfig.from_global_config(host="pcbdraft_work")
        self.assertEqual(
            (after.workspace_id, after.ai_peer), (before.workspace_id, before.ai_peer)
        )
        self.assertFalse(after.enabled)
        self.assertNotIn("scoped-fixture", (self.home / "honcho.json").read_text())

    def test_honcho_process_env_only_named_profile_roundtrip(self):
        from pcbdraft.agent.memory_backends.honcho import HonchoMemoryProvider
        from pcbdraft.agent.memory_backends.honcho.client import HonchoClientConfig

        with (
            patch.object(secret_scope, "_MULTIPLEX_ACTIVE", False),
            patch.dict(os.environ, {"HONCHO_BASE_URL": "http://127.0.0.1:8000"}),
        ):
            before = HonchoClientConfig.from_global_config(host="pcbdraft_work")
            HonchoMemoryProvider().save_config({}, str(self.home))
            after = HonchoClientConfig.from_global_config(host="pcbdraft_work")
        self.assertEqual(
            (before.workspace_id, before.ai_peer), ("hermes", "hermes_work")
        )
        self.assertEqual(
            (after.workspace_id, after.ai_peer), (before.workspace_id, before.ai_peer)
        )

    def test_real_openviking_save_link_resolves_source_before_defaults(self):
        from pcbdraft.agent.memory_backends.openviking import (
            OpenVikingMemoryProvider,
            _load_pcbdraft_openviking_config,
            _resolve_connection_settings,
        )

        linked = self.root / "shared-ovcli.json"
        cases = (
            ({}, {}, "hermes"),
            ({"agent_id": "shared-peer"}, {}, "shared-peer"),
            ({}, {"agent": "chosen-peer"}, "chosen-peer"),
        )
        for shared_ids, patch_ids, expected in cases:
            with self.subTest(expected=expected):
                (self.home / "config.yaml").unlink(missing_ok=True)
                linked.write_text(
                    json.dumps({"url": "http://127.0.0.1:1933", **shared_ids}),
                    encoding="utf-8",
                )
                values = {
                    "use_ovcli_config": True,
                    "ovcli_config_path": str(linked),
                    **patch_ids,
                }
                before = _resolve_connection_settings(values)
                self.assertEqual(before["agent"], expected)
                OpenVikingMemoryProvider().save_config(values, str(self.home))
                saved = yaml.safe_load((self.home / "config.yaml").read_text())
                stored = saved["memory"]["openviking"]
                self.assertEqual(stored["agent"], expected)
                self.assertEqual(stored[NAMESPACE_ORIGIN_KEY], "legacy")
                after = _resolve_connection_settings(_load_pcbdraft_openviking_config())
                self.assertEqual(after["agent"], before["agent"])

    def test_empty_scope_masks_other_profile_process_credentials(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider, _load_config
        from pcbdraft.agent.memory_backends.supermemory import (
            SupermemoryMemoryProvider,
            _load_supermemory_config,
        )

        with patch.dict(
            os.environ,
            {
                "MEM0_API_KEY": "other-profile",
                "SUPERMEMORY_API_KEY": "other-profile",
            },
        ):
            self.assertEqual(_load_config()["user_id"], "pcbdraft-user")
            Mem0MemoryProvider().save_config({}, str(self.home))
            SupermemoryMemoryProvider().save_config({}, str(self.home))
            self.assertEqual(_load_config()["user_id"], "pcbdraft-user")
            self.assertEqual(
                _load_supermemory_config(str(self.home))["container_tag"], "pcbdraft"
            )

    def test_scoped_roundtrip_applies_explicit_namespace_patch(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider, _load_config
        from pcbdraft.agent.memory_backends.mem0._setup import _save_mem0_json
        from pcbdraft.agent.memory_backends.supermemory import (
            SupermemoryMemoryProvider,
            _load_supermemory_config,
        )

        self.scoped({"MEM0_API_KEY": "fixture", "SUPERMEMORY_API_KEY": "fixture"})
        _save_mem0_json(str(self.home), {"user_id": "chosen-user", "extra": 7})
        Mem0MemoryProvider().save_config({}, str(self.home))
        self.assertEqual(_load_config()["user_id"], "chosen-user")
        self.assertEqual(json.loads((self.home / "mem0.json").read_text())["extra"], 7)
        provider = SupermemoryMemoryProvider()
        provider.save_config({"container_tag": "chosen_tag"}, str(self.home))
        provider.save_config({}, str(self.home))
        self.assertEqual(
            _load_supermemory_config(str(self.home))["container_tag"], "chosen_tag"
        )

    def test_target_profile_cached_external_secrets_and_ids_roundtrip(self):
        from pcbdraft.agent.memory_backends.mem0 import Mem0MemoryProvider, _load_config
        from pcbdraft.agent.memory_backends.supermemory import (
            SupermemoryMemoryProvider,
            _load_supermemory_config,
        )

        target = self.root / "external-target"
        target.mkdir()
        values = {
            "MEM0_API_KEY": "external-fixture",
            "MEM0_USER_ID": "external-user",
            "SUPERMEMORY_API_KEY": "external-fixture",
            "SUPERMEMORY_CONTAINER_TAG": "external_tag",
        }
        with patch(
            "pcbdraft.model.env_loader.get_secret_source_values", return_value=values
        ) as lookup:
            before = _load_config(str(target))
            Mem0MemoryProvider().save_config({}, str(target))
            SupermemoryMemoryProvider().save_config({}, str(target))
            self.assertEqual(_load_config(str(target))["user_id"], before["user_id"])
            self.assertEqual(before["user_id"], "external-user")
            self.assertEqual(
                _load_supermemory_config(str(target))["container_tag"], "external_tag"
            )
            self.assertTrue(lookup.call_args_list)
            self.assertTrue(
                all(call.args == (target,) for call in lookup.call_args_list)
            )
        self.assertNotIn("external-fixture", (target / "mem0.json").read_text())
        self.assertNotIn("external-fixture", (target / "supermemory.json").read_text())

    def test_honcho_save_preserves_each_explicit_host_identity(self):
        from pcbdraft.agent.memory_backends.honcho import HonchoMemoryProvider

        hosts = {
            "hermes.work": {
                "workspace": "work-bank",
                "aiPeer": "work-peer",
                "enabled": False,
                "oauth": {"refreshToken": "fixture"},
            },
            "hermes_other": {"workspace": "other-bank", "aiPeer": "other-peer"},
        }
        path = self.home / "honcho.json"
        path.write_text(
            json.dumps({"workspace": "root-bank", "hosts": hosts}), encoding="utf-8"
        )
        HonchoMemoryProvider().save_config({}, str(self.home))
        self.assertEqual(json.loads(path.read_text())["hosts"], hosts)


if __name__ == "__main__":
    unittest.main()
