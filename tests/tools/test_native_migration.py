"""Offline behavioral contracts for the tools-owned PCBDraft migration."""

from __future__ import annotations

import ast
import base64
import builtins
import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class NativeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcbdraft-migration-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "native-runtime"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_media_materializes_without_messaging_and_contains_names(self):
        from pcbdraft.tools import mcp_tool

        block = SimpleNamespace(
            data=base64.b64encode(b"media").decode(), mimeType="image/png"
        )
        marker = mcp_tool._cache_mcp_image_block(block)
        self.assertTrue(marker.startswith("MEDIA:"), marker)
        image = Path(marker.removeprefix("MEDIA:"))
        self.assertEqual(image.read_bytes(), b"media")
        self.assertTrue(image.is_relative_to(self.root / "cache" / "images"))
        block.mimeType = "audio/wav"
        audio = Path(mcp_tool._cache_mcp_audio_block(block).removeprefix("MEDIA:"))
        self.assertEqual(audio.read_bytes(), b"media")
        from pcbdraft.tools.media_cache import cache_document_from_bytes

        document = Path(cache_document_from_bytes(b"document", "../../bad\\name\n.txt"))
        self.assertEqual(document.parent, self.root / "cache" / "documents")
        self.assertNotIn("\n", document.name)
        self.assertEqual(document.read_bytes(), b"document")

    def test_metadata_native_wins_without_losing_legacy_fields(self):
        from pcbdraft.tools.legacy_metadata import read_pcbdraft_metadata

        metadata = {
            "hermes": {"tags": ["legacy"], "custom": {"v": 1}},
            "pcbdraft": {"tags": []},
        }
        result = read_pcbdraft_metadata(metadata)
        self.assertEqual(result, {"tags": [], "custom": {"v": 1}})
        self.assertEqual(metadata["hermes"]["tags"], ["legacy"])
        self.assertEqual(read_pcbdraft_metadata(None), {})

    def test_optional_acp_absence_and_enabled_failure(self):
        from pcbdraft.tools import acp_edit_approval as gate

        real_import = builtins.__import__

        def import_without_acp(name, *args, **kwargs):
            if name.startswith("acp_adapter"):
                raise ModuleNotFoundError("missing ACP", name="acp_adapter")
            return real_import(name, *args, **kwargs)

        with (
            patch.dict(os.environ, {"_PCBDRAFT_ACP": ""}),
            patch("builtins.__import__", side_effect=import_without_acp),
        ):
            self.assertIsNone(gate.maybe_require_edit_approval("write_file", {}))
            token = gate.set_acp_edit_approval_enabled(True)
            try:
                with self.assertRaises(ModuleNotFoundError):
                    gate.maybe_require_edit_approval("write_file", {})
            finally:
                gate.reset_acp_edit_approval_enabled(token)

    def test_wake_requires_real_explicit_model(self):
        from pcbdraft.tools import wake_word

        self.assertEqual(wake_word.wake_phrase({}), "")
        with self.assertRaises(ValueError):
            wake_word._configured_openwakeword_model({})
        model = "/models/custom.onnx"
        self.assertEqual(
            wake_word._configured_openwakeword_model(
                {"openwakeword": {"model": model}}
            ),
            model,
        )

    def test_default_skills_are_offline(self):
        from pcbdraft.tools import skills_hub, skills_sync_client

        with (
            patch.dict(
                os.environ,
                {
                    "PCBDRAFT_RUNTIME_INDEX_URL": "",
                    "PCBDRAFT_RUNTIME_SKILL_SOURCES": "",
                    "PCBDRAFT_RUNTIME_SYNC_BASE_URL": "",
                },
            ),
            patch.object(
                skills_hub.httpx, "get", side_effect=AssertionError("network")
            ),
            patch.object(skills_hub.TapsManager, "list_taps", return_value=[]),
            patch("pcbdraft.model.configuration.load_config", return_value={}),
        ):
            sources = skills_hub.create_source_router(auth=SimpleNamespace())
            self.assertEqual([s.source_id() for s in sources], ["official"])
            sources[0]._optional_dir = self.root / "absent"
            self.assertEqual(sources[0].search("missing"), [])
            self.assertIsNone(sources[0].fetch("missing"))
            self.assertIsNone(sources[0].inspect("missing"))
            self.assertIsNone(skills_hub._load_pcbdraft_index())
            self.assertIsNone(skills_sync_client.resolve_sync_base_url())

    def test_remote_sync_producers_use_native_root(self):
        from pcbdraft.tools import credential_files
        from pcbdraft.tools.environments.file_sync import iter_sync_files

        skill = self.root / "skills" / "example" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("test skill")
        with (
            patch.object(
                credential_files, "get_credential_file_mounts", return_value=[]
            ),
            patch(
                "pcbdraft.agent.skill_utils.get_external_skills_dirs", return_value=[]
            ),
            patch(
                "pcbdraft.agent.skill_utils.get_project_skills_dirs", return_value=[]
            ),
        ):
            native = dict(iter_sync_files())
            remote = dict(iter_sync_files("/home/remote/.pcbdraft/runtime"))
        self.assertEqual(
            native[str(skill)], "/root/.pcbdraft/runtime/skills/example/SKILL.md"
        )
        self.assertEqual(
            remote[str(skill)], "/home/remote/.pcbdraft/runtime/skills/example/SKILL.md"
        )

    def test_public_pcb_toolset_stays_closed(self):
        from pcbdraft.tools.toolsets import TOOLSETS

        self.assertFalse(any(name.startswith("hermes-") for name in TOOLSETS))
        self.assertIn("pcbdraft-cli", TOOLSETS)
        self.assertEqual(TOOLSETS["pcbdraft"]["includes"], [])
        self.assertEqual(
            set(TOOLSETS["pcbdraft"]["tools"]),
            {
                "pcb_plan_request",
                "pcb_generate_candidate",
                "pcb_validate",
                "pcb_repair_candidate",
                "pcb_apply_candidate",
                "pcb_discard_candidate",
                "pcb_undo_last_change",
                "pcb_render_previews",
                "pcb_build_release",
            },
        )

    def test_bidirectional_sync_preserves_credentials_and_ignores_foreign_tree(self):
        from pcbdraft.tools.environments.file_sync import FileSyncManager

        skill = self.root / "skills" / "a.md"
        skill.parent.mkdir()
        skill.write_text("original")
        token = self.root / "service-token.json"
        token.write_text("secret")
        remote = "/root/.pcbdraft/runtime"
        files = [
            (str(skill), f"{remote}/skills/a.md"),
            (str(token), f"{remote}/service-token.json"),
        ]
        uploaded = []

        def download(dest):
            with tarfile.open(dest, "w") as archive:
                for name, content in {
                    f"{remote}/skills/a.md": b"changed",
                    f"{remote}/skills/new.md": b"new",
                    f"{remote}/service-token.json": b"malicious",
                    "/root/.hermes/skills/foreign.md": b"foreign",
                }.items():
                    entry = tarfile.TarInfo(name.lstrip("/"))
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))

        with patch(
            "pcbdraft.tools.environments.file_sync._credential_host_paths",
            return_value={str(token)},
        ):
            manager = FileSyncManager(
                lambda: files,
                lambda host, path: uploaded.append(path),
                lambda paths: None,
                bulk_download_fn=download,
            )
            manager.sync(force=True)
            manager.sync_back(self.root)
        self.assertEqual(
            set(uploaded), {f"{remote}/skills/a.md", f"{remote}/service-token.json"}
        )
        self.assertEqual(skill.read_text(), "changed")
        self.assertEqual(skill.with_name("new.md").read_text(), "new")
        self.assertEqual(token.read_text(), "secret")
        self.assertFalse(skill.with_name("foreign.md").exists())

    def test_ssh_download_matches_upload_root_without_connecting(self):
        from pcbdraft.tools.environments.ssh import SSHEnvironment

        env = SSHEnvironment.__new__(SSHEnvironment)
        env._remote_home = "/home/remote user"
        with (
            patch.object(env, "_build_ssh_command", return_value=["ssh", "example"]),
            patch(
                "pcbdraft.tools.environments.ssh.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            env._ssh_bulk_download(self.root / "download.tar")
        self.assertEqual(
            run.call_args.args[0][-1],
            "tar cf - -C / 'home/remote user/.pcbdraft/runtime'",
        )

    def test_orphan_reaper_selects_only_native_owner(self):
        from pcbdraft.tools.environments import docker

        with patch.object(
            docker.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run:
            self.assertEqual(
                docker.reap_orphan_containers(
                    profile_filter="profile-a", docker_exe="docker"
                ),
                0,
            )
        command = run.call_args.args[0]
        self.assertIn("label=pcbdraft-agent=1", command)
        self.assertIn("label=pcbdraft-profile=profile-a", command)
        self.assertNotIn("hermes", " ".join(command))

    def test_native_child_environment_filters_secrets(self):
        from pcbdraft.tools.environments.local import pcbdraft_subprocess_env

        with patch.dict(
            os.environ,
            {
                "_PCBDRAFT_FORCE_OPENAI_API_KEY": "secret",
                "OPENAI_API_KEY": "secret",
                "PCBDRAFT_RUNTIME_HOME": str(self.root),
            },
        ):
            child = pcbdraft_subprocess_env()
        self.assertNotIn("_PCBDRAFT_FORCE_OPENAI_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertEqual(child["PCBDRAFT_RUNTIME_HOME"], str(self.root))

    def test_rpc_source_uses_native_module_and_protocol(self):
        from pcbdraft.tools.code_execution_tool import generate_pcbdraft_tools_module

        for transport in ("uds", "file"):
            source = generate_pcbdraft_tools_module([], transport=transport)
            compile(source, "pcbdraft_tools.py", "exec")
            self.assertNotIn("hermes", source.lower())

    def test_old_application_secret_protection_is_retained(self):
        import re

        from pcbdraft.tools import approval

        for root in ("~/.hermes", "~/.pcbdraft/runtime", "$PCBDRAFT_RUNTIME_HOME"):
            self.assertIsNotNone(
                re.search(approval._PCBDRAFT_ENV_PATH, f"{root}/.env".lower())
            )
            self.assertIsNotNone(
                re.search(approval._PCBDRAFT_CONFIG_PATH, f"{root}/config.yaml".lower())
            )

    def test_python_identifiers_are_native(self):
        tools = Path(__file__).resolve().parents[2] / "src/pcbdraft/tools"
        stale = []
        for path in tools.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                for field in ("id", "attr", "arg", "name", "asname"):
                    value = getattr(node, field, "")
                    if isinstance(value, str) and "hermes" in value.lower():
                        stale.append(f"{path.name}:{node.lineno}:{value}")
        self.assertEqual(stale, [])

    def test_checkpoint_ref_migration_keeps_native_and_legacy_data(self):
        from pcbdraft.tools import checkpoint_manager as checkpoints

        sha = "a" * 40
        completed = False

        def git(args, *_args, **kwargs):
            nonlocal completed
            if args[0] == "for-each-ref":
                return (
                    True,
                    (
                        f"refs/hermes/existing {sha}\nrefs/hermes/new {sha}\n"
                        f"refs/pcbdraft/existing {sha}\n"
                    ),
                    "",
                )
            if args[0] == "show-ref":
                return completed, sha if completed else "", ""
            if args[0] == "cat-file":
                return True, checkpoints._MIGRATION_CONTENT, ""
            if "hash-object" in args:
                return True, sha, ""
            self.assertIn("update-ref", args)
            transaction = kwargs["input_text"]
            self.assertIn(f"create refs/pcbdraft/new {sha}\n", transaction)
            self.assertNotIn("create refs/pcbdraft/existing", transaction)
            self.assertIn(f"create {checkpoints._MIGRATION_REF} {sha}\n", transaction)
            self.assertTrue(transaction.endswith("prepare\ncommit\n"))
            completed = True
            return True, "", ""

        with patch.object(checkpoints, "_run_git", side_effect=git) as run:
            self.assertIsNone(checkpoints._copy_legacy_refs(self.root, str(self.root)))
            count = run.call_count
            self.assertIsNone(checkpoints._copy_legacy_refs(self.root, str(self.root)))
            self.assertEqual(run.call_count, count + 2)
        self.assertFalse((self.root / "pcbdraft-refs-migrated").exists())

    def test_remote_wrapper_exports_sync_root_and_native_cwd_marker(self):
        from pcbdraft.tools.environments.base import BaseEnvironment

        class OfflineEnvironment(BaseEnvironment):
            def cleanup(self):
                pass

        env = OfflineEnvironment(cwd="/root", timeout=1)
        env._remote_runtime_home = "/home/remote user/.pcbdraft/runtime"
        command = env._wrap_command("pwd", "/root")
        self.assertIn(
            "export PCBDRAFT_RUNTIME_HOME='/home/remote user/.pcbdraft/runtime'",
            command,
        )
        self.assertIn("__PCBDRAFT_CWD_", command)
        self.assertNotIn("HERMES", command)
        self.assertIn("pcbdraft-snap-", env._snapshot_path)

    def test_explicit_index_is_not_native_content_or_builtin_trust(self):
        from pcbdraft.tools.skills_hub import PCBDraftIndexSource

        meta = PCBDraftIndexSource._to_meta(
            {
                "name": "third-party",
                "source": "official",
                "trust_level": "builtin",
                "identifier": "vendor/skill",
            }
        )
        self.assertEqual(meta.source, "configured-index")
        self.assertEqual(meta.trust_level, "community")
        self.assertEqual(meta.identifier, "vendor/skill")

    def test_active_acp_dependency_failure_is_not_swallowed(self):
        from pcbdraft.tools import acp_edit_approval as gate

        real_import = builtins.__import__

        def broken_acp(name, *args, **kwargs):
            if name.startswith("acp_adapter"):
                raise ModuleNotFoundError(
                    "missing adapter dependency", name="adapter_dependency"
                )
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=broken_acp),
            self.assertRaises(ModuleNotFoundError),
        ):
            gate.maybe_require_edit_approval("patch", {})

    def test_managed_modal_reuse_key_and_exec_are_native(self):
        from pcbdraft.tools.environments.managed_modal import ManagedModalEnvironment

        env = ManagedModalEnvironment.__new__(ManagedModalEnvironment)
        env._sandbox_id = None
        self.addCleanup(setattr, env, "_sandbox_id", None)
        env._sandbox_kwargs = {}
        env._image = "python:3.13"
        env.cwd = "/root"
        env.timeout = 30
        env._persistent = True
        env._task_id = "task"
        env._create_idempotency_key = "key"
        response = SimpleNamespace(status_code=200, json=lambda: {"id": "sandbox"})
        with patch.object(env, "_request", return_value=response) as request:
            self.assertEqual(env._create_sandbox(), "sandbox")
            self.assertEqual(
                request.call_args.kwargs["json"]["logicalKey"], "pcbdraft:task"
            )
            env._sandbox_id = "sandbox"
            env._start_modal_exec(
                SimpleNamespace(command="pwd", cwd="/root", timeout=1, stdin_data=None)
            )
            command = request.call_args.kwargs["json"]["command"]
            self.assertIn("PCBDRAFT_RUNTIME_HOME=/root/.pcbdraft/runtime", command)
            self.assertTrue(command.endswith("; pwd"))

    def test_plugin_registry_uses_native_namespace(self):
        from pcbdraft.tools.registry import registry

        self.assertEqual(
            registry._plugin_namespace_of_module("pcbdraft_plugins.example.handlers"),
            "pcbdraft_plugins.example",
        )
        self.assertIsNone(registry._plugin_namespace_of_module("unrelated.handlers"))

    @unittest.skipUnless(
        importlib.util.find_spec("mcp"), "optional MCP SDK not installed"
    )
    def test_mcp_session_identifies_native_client_without_relabeling_registry_ids(self):
        import inspect

        from mcp import ClientSession

        from pcbdraft.tools.mcp_oauth import _build_client_metadata
        from pcbdraft.tools.mcp_tool import _pcbdraft_mcp_client_info

        info = _pcbdraft_mcp_client_info()
        self.assertEqual(info.name, "PCBDraft")
        inspect.signature(ClientSession).bind(None, None, client_info=info)
        self.assertEqual(
            _build_client_metadata({"_resolved_port": 8123}).client_name, "PCBDraft"
        )
        self.assertEqual(
            _build_client_metadata(
                {"_resolved_port": 8123, "client_name": "third-party-registered-id"}
            ).client_name,
            "third-party-registered-id",
        )

    def test_executable_legacy_strings_are_only_compatibility_or_protection(self):
        tools = Path(__file__).resolve().parents[2] / "src/pcbdraft/tools"
        allowed_files = {
            "approval.py",
            "file_tools.py",
            "skills_guard.py",
            "threat_patterns.py",
            "legacy_metadata.py",
            "checkpoint_manager.py",
        }
        unexpected = []
        for path in tools.rglob("*.py"):
            tree = ast.parse(path.read_text())
            docs = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docs
                    and "hermes" in node.value.lower()
                    and path.name not in allowed_files
                ):
                    unexpected.append(f"{path.name}:{node.lineno}")
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main()
