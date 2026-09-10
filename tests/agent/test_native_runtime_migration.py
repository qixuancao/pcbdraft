"""Offline contract tests for the agent's native runtime migration."""

from __future__ import annotations

import ast
import base64
import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.legacy_compat import (
    BITWARDEN_CACHE_KDF_INFO,
    HONCHO_OAUTH_CLIENT_ID,
    configured_host_key,
    normalize_compaction_mode,
    read_memory_bank,
    read_skill_metadata,
)


class NativeRuntimeMigrationTests(unittest.TestCase):
    def test_native_skill_metadata_fields_are_authoritative(self):
        legacy = {"requires_tools": ["legacy-tool"]}
        self.assertEqual(read_skill_metadata({"metadata": {"hermes": legacy}}), legacy)
        for native in ({}, None, "invalid", {"requires_tools": ["native-tool"]}):
            self.assertEqual(
                read_skill_metadata(
                    {"metadata": {"pcbdraft": native, "hermes": legacy}}
                ),
                {**legacy, **(native if isinstance(native, dict) else {})},
            )

    def test_legacy_compaction_converts_without_disabling_it(self):
        self.assertEqual(normalize_compaction_mode("hermes"), "pcbdraft")
        for mode in ("native", "pcbdraft", "off"):
            self.assertEqual(normalize_compaction_mode(mode), mode)

    def test_persisted_memory_namespaces_are_not_rebranded(self):
        self.assertEqual(configured_host_key({}, "pcbdraft"), "pcbdraft")
        self.assertEqual(configured_host_key({"hermes": {}}, "pcbdraft"), "hermes")
        self.assertEqual(
            configured_host_key({"hermes.work": {}}, "pcbdraft_work"), "hermes.work"
        )
        self.assertEqual(
            configured_host_key({"hermes.work": {}}, "hermes_work"), "hermes.work"
        )
        self.assertEqual(
            configured_host_key({"pcbdraft": {}, "hermes": {}}, "pcbdraft"), "pcbdraft"
        )
        bank = {"bankId": "hermes-existing-data"}
        self.assertEqual(read_memory_bank({"banks": {"hermes": bank}}), bank)

    def test_external_registration_and_cipher_format_are_unchanged(self):
        self.assertEqual(HONCHO_OAUTH_CLIENT_ID, "hermes-agent")
        self.assertEqual(BITWARDEN_CACHE_KDF_INFO, b"hermes-bws-encrypted-cache-v1")

    def test_manifest_uses_native_project_directory_only(self):
        from pcbdraft.agent.verify import load_manifest, manifest_path, save_manifest
        from pcbdraft.agent.verify.recipes import Recipe

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / ".hermes" / "environment.json"
            legacy.parent.mkdir()
            legacy.write_text(json.dumps({"kind": "legacy"}), encoding="utf-8")
            self.assertIsNone(load_manifest(root))
            self.assertEqual(
                manifest_path(root), root / ".pcbdraft" / "environment.json"
            )
            recipe = Recipe(kind="python", name="migration-test")
            save_manifest(root, recipe)
            self.assertEqual(load_manifest(root).kind, "python")
            self.assertEqual(json.loads(legacy.read_text()), {"kind": "legacy"})

    def test_runtime_fallback_never_uses_standalone_home(self):
        from pcbdraft.agent.file_safety import _runtime_home_path

        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "runtime"
            with (
                patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(expected)}),
                patch(
                    "pcbdraft.core.runtime_environment.get_runtime_home",
                    side_effect=ImportError,
                ),
            ):
                self.assertEqual(_runtime_home_path(), expected)

    def test_honcho_config_preserves_legacy_and_custom_namespaces(self):
        from pcbdraft.agent.memory_backends.honcho.client import HonchoClientConfig

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "honcho.json"
            for host in ("hermes", "pcbdraft"):
                path.write_text(
                    json.dumps({"hosts": {host: {"workspace": "old-bank"}}}),
                    encoding="utf-8",
                )
                config = HonchoClientConfig.from_global_config(
                    host="pcbdraft", config_path=path
                )
                self.assertEqual(config.host, host)
                self.assertEqual(config.workspace_id, "old-bank")
                self.assertEqual(config.ai_peer, host)
            self.assertEqual(HonchoClientConfig().workspace_id, "pcbdraft")

    def test_mem0_config_and_setup_preserve_persisted_identity(self):
        from pcbdraft.agent.memory_backends.mem0 import _load_config
        from pcbdraft.agent.memory_backends.mem0._setup import (
            _existing_memory_identity,
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": directory, "MEM0_AGENT_ID": "pcbdraft"},
            ),
        ):
            self.assertEqual(_load_config()["agent_id"], "pcbdraft")
            identity = {"user_id": "hermes-user", "agent_id": "hermes"}
            (Path(directory) / "mem0.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            config = _load_config()
            self.assertEqual(config["agent_id"], "hermes")
            self.assertEqual(config["user_id"], "hermes-user")
            self.assertEqual(_existing_memory_identity(directory), identity)

    def test_bitwarden_reads_actual_legacy_ciphertext(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        from pcbdraft.agent.secret_sources.bitwarden import (
            _read_encrypted_disk_cache,
        )

        token = "offline-migration-fixture"  # noqa: S105 - synthetic fixture
        salt, nonce = bytes(range(16)), bytes(range(12))
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"hermes-bws-encrypted-cache-v1",
        ).derive(token.encode())
        cache_key = ("fingerprint", "project", "https://example.invalid")
        serialized_key = "|".join(cache_key)
        plaintext = json.dumps(
            {"secrets": {"EXAMPLE": "fixture"}, "fetched_at": time.time()}
        ).encode()
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, serialized_key.encode())
        payload = {
            "version": 1,
            "key": serialized_key,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "cache" / "bws_cache.enc.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = _read_encrypted_disk_cache(
                cache_key=cache_key,
                access_token=token,
                max_age_seconds=60,
                home_path=home,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.secrets, {"EXAMPLE": "fixture"})
            self.assertEqual(json.loads(path.read_text()), payload)

    def test_memory_store_native_write_keeps_legacy_database_path(self):
        import yaml

        from pcbdraft.agent.memory_backends.holographic import (
            HolographicMemoryProvider,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            legacy = {"db_path": "/existing/memory.db", "recall_limit": 4}
            path.write_text(
                yaml.safe_dump({"plugins": {"hermes-memory-store": legacy}}),
                encoding="utf-8",
            )
            HolographicMemoryProvider().save_config({"recall_limit": 8}, directory)
            plugins = yaml.safe_load(path.read_text())["plugins"]
            self.assertEqual(plugins["hermes-memory-store"], legacy)
            self.assertEqual(
                plugins["pcbdraft-memory-store"],
                {"db_path": "/existing/memory.db", "recall_limit": 8},
            )

    def test_native_discovery_ignores_old_project_directories(self):
        from pcbdraft.agent.extensions import manager
        from pcbdraft.agent.memory_backends import _get_project_plugins_dir
        from pcbdraft.agent.prompt_builder import _find_pcbdraft_md
        from pcbdraft.agent.skill_utils import PROJECT_SKILLS_SUBDIRS

        self.assertEqual(
            PROJECT_SKILLS_SUBDIRS,
            (os.path.join(".pcbdraft", "skills"), os.path.join(".agents", "skills")),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".pcbdraft", ".hermes"):
                (root / name / "plugins").mkdir(parents=True)
            (root / ".hermes.md").write_text("legacy", encoding="utf-8")
            self.assertIsNone(_find_pcbdraft_md(root))
            native = root / "PCBDRAFT.md"
            native.write_text("native", encoding="utf-8")
            self.assertEqual(_find_pcbdraft_md(root), native)
            with (
                patch("pathlib.Path.cwd", return_value=root),
                patch.dict(
                    os.environ, {"PCBDRAFT_RUNTIME_ENABLE_PROJECT_PLUGINS": "1"}
                ),
            ):
                self.assertEqual(
                    _get_project_plugins_dir(), root / ".pcbdraft" / "plugins"
                )
                instance = manager.PluginManager()
                with patch.object(instance, "_scan_directory", return_value=[]) as scan:
                    instance._collect_directory_manifests()
                project_calls = [
                    call.args[0]
                    for call in scan.call_args_list
                    if call.kwargs.get("source") == "project"
                ]
                self.assertEqual(project_calls, [root / ".pcbdraft" / "plugins"])

    def test_directory_plugin_relative_imports_use_native_namespace(self):
        from pcbdraft.agent.extensions.manager import PluginManager, PluginManifest

        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules):
            root = Path(directory)
            (root / "__init__.py").write_text(
                "from .helper import VALUE\n", encoding="utf-8"
            )
            (root / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
            manifest = PluginManifest(name="migration-fixture", path=str(root))
            module = PluginManager()._load_directory_module(manifest)
            self.assertTrue(module.__name__.startswith("pcbdraft_plugins."))
            self.assertEqual(module.VALUE, 42)
            self.assertIn(f"{module.__name__}.helper", sys.modules)

    def test_native_import_contracts_and_entrypoints(self):
        contracts = {
            "pcbdraft.model.env_loader": "load_pcbdraft_dotenv",
            "pcbdraft.core.runtime_environment": "get_pcbdraft_dir",
            "pcbdraft.tools.xai_http": "pcbdraft_xai_default_headers",
            "pcbdraft.tools.toolsets": "_PCBDRAFT_CORE_TOOLS",
            "pcbdraft.model.model_switch": "_check_pcbdraft_model_warning",
            "pcbdraft.agent.portal_tags": "pcbdraft_client_tag",
            "pcbdraft.agent.lsp.install": "pcbdraft_lsp_bin_dir",
            "pcbdraft.agent.lsp.servers": "pcbdraft_lsp_session_dir",
        }
        for module, symbol in contracts.items():
            with self.subTest(module=module, symbol=symbol):
                self.assertTrue(hasattr(importlib.import_module(module), symbol))
        from pcbdraft.agent.extensions import manager
        from pcbdraft.agent.memory_backends import ENTRY_POINTS_GROUP

        self.assertEqual(manager.ENTRY_POINTS_GROUP, "pcbdraft.plugins")
        self.assertEqual(ENTRY_POINTS_GROUP, "pcbdraft.memory_providers")
        self.assertEqual(manager._NS_PARENT, "pcbdraft_plugins")

    def test_verify_module_detect_only_is_a_real_offline_entrypoint(self):
        from pcbdraft.agent.verify.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "pyproject.toml").write_text(
                '[project]\nname = "offline-fixture"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([directory, "--detect-only", "--json"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["source"], "detected")

    def test_python_identifiers_have_no_permanent_old_aliases(self):
        root = Path(__file__).resolve().parents[2] / "src" / "pcbdraft" / "agent"
        self.assertTrue(root.is_dir())
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "tooling.py":
                continue  # owned by the preceding task-contract iteration
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                values = []
                if isinstance(node, ast.Name):
                    values = [node.id]
                elif isinstance(node, ast.Attribute):
                    values = [node.attr]
                elif isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    values = [node.name]
                elif isinstance(node, ast.arg):
                    values = [node.arg]
                elif isinstance(node, ast.alias):
                    values = [node.name, node.asname or ""]
                for value in values:
                    if "hermes" in value.lower():
                        offenders.append(
                            f"{path.relative_to(root)}:{node.lineno}:{value}"
                        )
        self.assertFalse(offenders, "\n".join(offenders))

    def test_runtime_strings_use_native_branding(self):
        root = Path(__file__).resolve().parents[2] / "src" / "pcbdraft" / "agent"
        offenders = set()
        for path in root.rglob("*.py"):
            if path.name in {"tooling.py", "legacy_compat.py"}:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            docs = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    not isinstance(node, ast.Constant)
                    or not isinstance(node.value, (str, bytes))
                    or id(node) in docs
                ):
                    continue
                value = (
                    node.value.decode("utf-8", errors="replace")
                    if isinstance(node.value, bytes)
                    else node.value
                )
                if "hermes" not in value.lower():
                    continue
                # Upstream issue URLs, actual model IDs and registered integration
                # identifiers describe third parties, not the PCBDraft runtime.
                if any(
                    external in value
                    for external in (
                        "NousResearch/hermes-agent",
                        "Nous Research Hermes",
                        "hermes-agent#",
                        "https://docs.honcho.dev",
                        "https://app.supermemory.ai/integrations?connect=hermes",
                    )
                ):
                    continue
                if path.name == "templates.py" and node.value == "hermes":
                    continue  # Hindsight's provider-owned catalog tag
                for line_number in range(
                    node.lineno, (node.end_lineno or node.lineno) + 1
                ):
                    line = source.splitlines()[line_number - 1]
                    if "hermes" in line.lower():
                        offenders.add(
                            f"{path.relative_to(root)}:{line_number}: {line.strip()}"
                        )
        self.assertFalse(offenders, "\n".join(sorted(offenders)))


if __name__ == "__main__":
    unittest.main()
