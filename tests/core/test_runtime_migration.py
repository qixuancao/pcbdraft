"""Offline regressions for product runtime migration and path isolation."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import legacy_migration, platform_paths, runtime_environment
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.runtime_paths import default_runtime_home, runtime_home


class RuntimeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.base = self.root / "config" / "pcbdraft"
        self.legacy = self.base / "hermes"
        self.native = self.base / "runtime"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.root / "user"),
                    "USERPROFILE": str(self.root / "user"),
                    "PCBDRAFT_CONFIG": str(self.base / "config.json"),
                    "PCBDRAFT_RUNTIME_HOME": "",
                    "PCBDRAFT_HERMES_HOME": "",
                    "HERMES_HOME": str(self.root / "user" / ".hermes"),
                },
                clear=True,
            )
        )
        token = runtime_environment.set_runtime_home_override(None)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)

    def test_path_getters_do_not_inspect_or_create_state(self) -> None:
        os.environ["PCBDRAFT_HERMES_HOME"] = str(self.root / "ignored")
        with (
            patch.object(Path, "exists", side_effect=AssertionError("state probe")),
            patch.object(Path, "read_text", side_effect=AssertionError("state read")),
            patch.object(Path, "mkdir", side_effect=AssertionError("state write")),
        ):
            for getter in (
                runtime_home,
                default_runtime_home,
                runtime_environment.get_runtime_home,
                runtime_environment.get_process_runtime_home,
                runtime_environment.get_default_runtime_root,
            ):
                with self.subTest(getter=getter.__name__):
                    self.assertEqual(getter(), self.native)
        self.assertFalse(self.base.exists())

    def test_startup_migrates_whole_directory_without_reading_credentials(self) -> None:
        from pcbdraft.services.provider_connection import activate_provider_runtime

        self.legacy.mkdir(parents=True, mode=0o700)
        payloads = {
            "auth.json": b"opaque-auth-bytes",
            "state.db": b"opaque-db-bytes",
            "state.db-wal": b"opaque-wal-bytes",
            "state.db-shm": b"opaque-shm-bytes",
            "profiles/coder/config.yaml": b"opaque-profile-bytes",
        }
        for relative, content in payloads.items():
            path = self.legacy / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
        standalone = self.root / "user" / ".hermes"
        standalone.mkdir(parents=True)
        sentinel = standalone / "auth.json"
        sentinel.write_bytes(b"independent-credentials")
        self.assertEqual(runtime_home(), self.native)
        self.assertTrue(self.legacy.exists())
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("credential read")
        ):
            activate_provider_runtime()
        self.assertFalse(self.legacy.exists())
        for relative, content in payloads.items():
            path = self.native / relative
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sentinel.read_bytes(), b"independent-credentials")
        record = self.base / "runtime-migration.json"
        before = record.read_bytes()
        self.assertEqual(json.loads(before)["outcome"], "migrated")
        self.assertNotIn(b"opaque", before)
        activate_provider_runtime()
        self.assertEqual(record.read_bytes(), before)

    def test_conflict_keeps_both_trees_and_records_once(self) -> None:
        for directory, content in ((self.legacy, b"old"), (self.native, b"new")):
            directory.mkdir(parents=True)
            (directory / "auth.json").write_bytes(content)
        result = legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual(result.migration, "conflict")
        self.assertEqual(result.path, self.native)
        self.assertEqual((self.legacy / "auth.json").read_bytes(), b"old")
        self.assertEqual((self.native / "auth.json").read_bytes(), b"new")
        self.assertIsNotNone(result.record_path)
        before = result.record_path.read_bytes()
        legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual(result.record_path.read_bytes(), before)
        self.assertTrue(json.loads(before)["source_retained"])

    def test_startup_conflict_is_visible_before_binding_or_creating_auth(self) -> None:
        from pcbdraft.services.provider_connection import activate_provider_runtime

        self.legacy.mkdir(parents=True)
        self.native.mkdir()
        (self.legacy / "auth.json").write_bytes(b"account")
        with self.assertRaisesRegex(PCBDraftError, "conflict.*both legacy and native"):
            activate_provider_runtime()
        self.assertEqual(os.environ["PCBDRAFT_RUNTIME_HOME"], "")
        self.assertFalse((self.native / "shared").exists())
        self.assertEqual((self.legacy / "auth.json").read_bytes(), b"account")

    def test_deprecated_override_requires_explicit_upgrade_without_probes(self) -> None:
        os.environ["PCBDRAFT_HERMES_HOME"] = "~/old-product-data"
        for explicit in ("", str(self.root / "new-explicit")):
            with (
                self.subTest(explicit=explicit),
                patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": explicit}),
                patch.object(Path, "exists", side_effect=AssertionError("probe")),
                self.assertRaisesRegex(PCBDraftError, "Set PCBDRAFT_RUNTIME_HOME"),
            ):
                legacy_migration.migrate_legacy_runtime_home()
        self.assertFalse(self.base.exists())

    def test_absolute_links_into_old_root_block_rename_and_preserve_tree(self) -> None:
        self.legacy.mkdir(parents=True)
        (self.legacy / "state.db").write_bytes(b"database")
        link = self.legacy / "nested" / "db-link"
        link.parent.mkdir()
        # Cover missing targets as well: those references must not create a new
        # database at the abandoned root after migration.
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.root)
        for target in (
            self.legacy / "state.db",
            self.legacy / "missing.db",
            "~/config/pcbdraft/hermes/state.db",
        ):
            with self.subTest(target=target):
                link.symlink_to(target)
                with self.assertRaisesRegex(PCBDraftError, "symbolic link refers"):
                    legacy_migration.migrate_legacy_runtime_home()
                self.assertEqual(os.readlink(link), str(target))
                self.assertEqual((self.legacy / "state.db").read_bytes(), b"database")
                self.assertFalse(self.native.exists())
                link.unlink()

    def test_saved_json_yaml_paths_including_tilde_block_without_rewriting(
        self,
    ) -> None:
        self.legacy.mkdir(parents=True)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.root)
        variants = (
            str(self.legacy / "memory.db"),
            "~/config/pcbdraft/hermes/memory.db",
        )
        for filename in ("config.json", "config.yaml", "settings.yml"):
            for value in variants:
                with self.subTest(filename=filename, value=value):
                    path = self.legacy / filename
                    document = {"memory": {"holographic": {"db_path": value}}}
                    original = json.dumps(document).encode()
                    path.write_bytes(original)  # JSON is also valid YAML.
                    with self.assertRaisesRegex(
                        PCBDraftError, "saved configuration path"
                    ):
                        legacy_migration.migrate_legacy_runtime_home()
                    self.assertEqual(path.read_bytes(), original)
                    self.assertFalse(self.native.exists())
                    path.unlink()

    def test_external_links_and_unknown_credentials_are_preserved_without_reading(
        self,
    ) -> None:
        self.legacy.mkdir(parents=True)
        external = self.root / "external"
        external.mkdir()
        (external / "config.yaml").write_text("invalid: [", encoding="utf-8")
        (self.legacy / "external").symlink_to(external, target_is_directory=True)
        (self.legacy / "auth.json").write_bytes(b"unknown-credential-format")
        (self.legacy / ".env").write_bytes(b"unknown-env-format")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name in {"auth.json", ".env"} or path.is_relative_to(external):
                self.fail("migration read external state or unknown credentials")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            result = legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual(result.migration, "migrated")
        self.assertEqual(os.readlink(self.native / "external"), str(external))
        self.assertEqual(
            (self.native / "auth.json").read_bytes(), b"unknown-credential-format"
        )

    def test_invalid_known_configuration_is_fail_closed_and_secret_free(self) -> None:
        self.legacy.mkdir(parents=True)
        config = self.legacy / "config.yaml"
        original = b"private-secret: [invalid"
        config.write_bytes(original)
        with self.assertRaises(PCBDraftError) as raised:
            legacy_migration.migrate_legacy_runtime_home()
        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn("private-secret", rendered)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse(self.native.exists())

    def test_unexpected_yaml_parser_errors_are_not_masked(self) -> None:
        self.legacy.mkdir(parents=True)
        (self.legacy / "config.yaml").write_text("model: {}", encoding="utf-8")
        with (
            patch("yaml.safe_load", side_effect=TypeError("unexpected parser failure")),
            self.assertRaisesRegex(TypeError, "unexpected parser failure"),
        ):
            legacy_migration.migrate_legacy_runtime_home()
        self.assertTrue(self.legacy.is_dir())
        self.assertFalse(self.native.exists())

    def test_macos_platform_config_under_var_parent_alias_can_migrate(self) -> None:
        physical_var = self.root / "private" / "var"
        physical_var.mkdir(parents=True)
        alias_var = self.root / "var"
        alias_var.symlink_to(physical_var, target_is_directory=True)
        user_home = alias_var / "folders" / "session" / "home"
        relative_base = Path("Library") / "Application Support" / "pcbdraft"
        physical_base = physical_var / "folders" / "session" / "home" / relative_base
        old = physical_base / "hermes"
        old.mkdir(parents=True)
        (old / "state.db").write_bytes(b"preserved")
        with (
            patch.dict(os.environ, {"PCBDRAFT_CONFIG": ""}),
            patch.object(platform_paths.platform, "system", return_value="Darwin"),
            patch.object(Path, "home", return_value=user_home),
        ):
            self.assertEqual(
                default_runtime_home(), user_home / relative_base / "runtime"
            )
            result = legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual(result.migration, "migrated")
        self.assertEqual(result.path, physical_base / "runtime")
        self.assertEqual((result.path / "state.db").read_bytes(), b"preserved")
        self.assertFalse(old.exists())
        self.assertTrue(alias_var.is_symlink())

    def test_parent_alias_does_not_hide_saved_or_linked_old_root_references(
        self,
    ) -> None:
        self.legacy.mkdir(parents=True)
        parent_alias = self.root / "config-alias"
        parent_alias.symlink_to(self.base.parent, target_is_directory=True)
        alias_base = parent_alias / "pcbdraft"
        os.environ["PCBDRAFT_CONFIG"] = str(alias_base / "config.json")
        for root in (self.legacy, alias_base / "hermes"):
            for kind in ("config", "link"):
                with self.subTest(root=root, kind=kind):
                    target = str(root / "memory.db")
                    entry = self.legacy / (
                        "config.yaml" if kind == "config" else "db-link"
                    )
                    if kind == "config":
                        entry.write_text(
                            json.dumps({"db_path": target}), encoding="utf-8"
                        )
                    else:
                        entry.symlink_to(target)
                    with self.assertRaisesRegex(PCBDraftError, "old runtime root"):
                        legacy_migration.migrate_legacy_runtime_home()
                    self.assertFalse(self.native.exists())
                    entry.unlink()

    def test_config_root_symlink_is_still_rejected(self) -> None:
        self.base.parent.mkdir(parents=True)
        external = self.root / "external"
        external.mkdir()
        (external / "sentinel").write_bytes(b"unchanged")
        self.base.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(PCBDraftError, "symbolic link"):
            legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual((external / "sentinel").read_bytes(), b"unchanged")
        self.assertEqual([entry.name for entry in external.iterdir()], ["sentinel"])

    def test_mem0_qdrant_old_root_references_preserve_every_source_entry(self) -> None:
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.root)
        for relative, vector_path in (
            ("mem0.json", str(self.legacy / "mem0_qdrant")),
            ("profiles/coder/mem0.json", "~/config/pcbdraft/hermes/mem0_qdrant"),
        ):
            with self.subTest(relative=relative, vector_path=vector_path):
                payloads = {
                    relative: json.dumps(
                        {
                            "oss": {
                                "vector_store": {
                                    "provider": "qdrant",
                                    "config": {"path": vector_path},
                                }
                            }
                        }
                    ).encode(),
                    "mem0_qdrant/collection/storage.sqlite": b"qdrant-store",
                    "state.db": b"session-store",
                    "state.db-wal": b"session-wal",
                    "auth.json": b"fixture-credentials-not-to-be-read",
                    ".env": b"FIXTURE_KEY=do-not-read\n",
                }
                for name, data in payloads.items():
                    path = self.legacy / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    path.chmod(0o600)

                def snapshot():
                    return {
                        path.relative_to(self.legacy).as_posix(): (
                            path.stat().st_mode,
                            path.stat().st_ino,
                            path.stat().st_mtime_ns,
                            path.read_bytes() if path.is_file() else None,
                        )
                        for path in (self.legacy, *self.legacy.rglob("*"))
                    }

                before = snapshot()
                original_open = Path.open
                opened: list[str] = []

                def config_only_open(
                    path, *args, _opened=opened, _open=original_open, **kwargs
                ):
                    self.assertEqual(
                        path.name, "mem0.json", "read outside known provider config"
                    )
                    _opened.append(path.name)
                    return _open(path, *args, **kwargs)

                with (
                    patch.object(Path, "open", config_only_open),
                    patch.object(Path, "rename") as rename,
                    self.assertRaisesRegex(PCBDraftError, "saved configuration path"),
                ):
                    legacy_migration.migrate_legacy_runtime_home()
                rename.assert_not_called()
                self.assertEqual(opened, ["mem0.json"])
                self.assertEqual(snapshot(), before)
                self.assertFalse(self.native.exists())
                self.assertFalse((self.base / "runtime-migration.json").exists())
                (self.legacy / relative).unlink()

    def test_actual_provider_path_fields_in_config_yaml_block_rename(self) -> None:
        self.legacy.mkdir(parents=True)
        for document in (
            {
                "plugins": {
                    "pcbdraft-memory-store": {"db_path": str(self.legacy / "memory.db")}
                }
            },
            {
                "memory": {
                    "openviking": {"ovcli_config_path": str(self.legacy / "ovcli.conf")}
                }
            },
        ):
            with self.subTest(document=document):
                config = self.legacy / "config.yaml"
                original = json.dumps(document).encode()
                config.write_bytes(original)
                with self.assertRaisesRegex(PCBDraftError, "saved configuration path"):
                    legacy_migration.migrate_legacy_runtime_home()
                self.assertEqual(config.read_bytes(), original)
                self.assertFalse(self.native.exists())

    def test_record_failure_rolls_back_whole_directory(self) -> None:
        self.legacy.mkdir(parents=True)
        (self.legacy / "state.db").write_bytes(b"preserved")
        with (
            patch.object(legacy_migration, "atomic_write_json", side_effect=OSError),
            self.assertRaisesRegex(PCBDraftError, "rolled back"),
        ):
            legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual((self.legacy / "state.db").read_bytes(), b"preserved")
        self.assertFalse(self.native.exists())

    def test_rename_failure_preserves_source_without_a_success_record(self) -> None:
        self.legacy.mkdir(parents=True)
        (self.legacy / "state.db").write_bytes(b"preserved")
        with (
            patch.object(Path, "rename", side_effect=OSError("rename refused")),
            self.assertRaisesRegex(PCBDraftError, "cannot atomically migrate"),
        ):
            legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual((self.legacy / "state.db").read_bytes(), b"preserved")
        self.assertFalse(self.native.exists())
        self.assertFalse((self.base / "runtime-migration.json").exists())

    def test_concurrent_startup_only_moves_source_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.legacy.mkdir(parents=True)
        (self.legacy / "state.db").write_bytes(b"preserved")
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(legacy_migration.migrate_legacy_runtime_home)
                for _ in range(2)
            ]
            outcomes = [future.result(timeout=15).migration for future in futures]
        self.assertCountEqual(outcomes, ["migrated", "native"])
        self.assertEqual((self.native / "state.db").read_bytes(), b"preserved")
        self.assertFalse(self.legacy.exists())

    def test_symlink_sources_targets_and_records_are_not_followed(self) -> None:
        self.base.mkdir(parents=True)
        parent_alias = self.root / "config-alias"
        parent_alias.symlink_to(self.base.parent, target_is_directory=True)
        os.environ["PCBDRAFT_CONFIG"] = str(parent_alias / "pcbdraft" / "config.json")
        external = self.root / "user" / ".hermes"
        external.mkdir(parents=True)
        sentinel = external / "sentinel"
        sentinel.write_bytes(b"untouched")
        for candidate in (
            self.legacy,
            self.native,
            self.base / ".runtime-migration-locks",
        ):
            with self.subTest(candidate=candidate):
                self.legacy.mkdir(exist_ok=True)
                if candidate == self.legacy:
                    self.legacy.rmdir()
                candidate.symlink_to(external, target_is_directory=True)
                with self.assertRaises(PCBDraftError):
                    legacy_migration.migrate_legacy_runtime_home()
                candidate.unlink()
                self.assertEqual(sentinel.read_bytes(), b"untouched")
        record = self.base / "runtime-migration.json"
        record.symlink_to(external / "missing")
        with self.assertRaisesRegex(PCBDraftError, "rolled back"):
            legacy_migration.migrate_legacy_runtime_home()
        self.assertFalse((external / "missing").exists())
        self.assertTrue(self.legacy.is_dir())
        self.assertFalse(self.native.exists())

    def test_explicit_override_skips_all_migration_probes(self) -> None:
        os.environ["PCBDRAFT_RUNTIME_HOME"] = " ~/custom-runtime "
        expected = self.root / "user" / "custom-runtime"
        with patch.object(Path, "is_symlink", side_effect=AssertionError("probe")):
            result = legacy_migration.migrate_legacy_runtime_home()
        self.assertEqual(result.path, expected)
        self.assertEqual(result.migration, "explicit")
        self.assertEqual(runtime_home(), expected)
        self.assertFalse(self.base.exists())

    def test_profile_root_uses_process_override_not_context_or_default(self) -> None:
        token = runtime_environment.set_runtime_home_override(" ~/task ")
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        for root in (self.native, self.root / "custom"):
            with self.subTest(root=root):
                os.environ["PCBDRAFT_RUNTIME_HOME"] = str(root / "profiles" / "coder")
                self.assertEqual(runtime_environment.get_default_runtime_root(), root)
                self.assertEqual(
                    runtime_environment.get_process_runtime_home(),
                    root / "profiles" / "coder",
                )
                self.assertEqual(
                    runtime_environment.get_runtime_home(), self.root / "user" / "task"
                )
        os.environ["PCBDRAFT_RUNTIME_HOME"] = " ~/custom/profiles/coder "
        self.assertEqual(
            runtime_environment.get_default_runtime_root(),
            self.root / "user" / "custom",
        )
        os.environ["PCBDRAFT_RUNTIME_HOME"] = " "
        self.assertEqual(runtime_environment.get_default_runtime_root(), self.native)

    def test_home_failure_has_no_cross_product_fallback(self) -> None:
        os.environ["PCBDRAFT_CONFIG"] = ""
        with (
            patch.object(platform_paths.platform, "system", return_value="Linux"),
            patch.object(Path, "home", side_effect=RuntimeError("home unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "home unavailable"):
                runtime_home()
            os.environ["PCBDRAFT_RUNTIME_HOME"] = str(self.root / "explicit")
            self.assertEqual(runtime_home(), self.root / "explicit")
            self.assertEqual(
                runtime_environment.display_runtime_home(), str(self.root / "explicit")
            )
            self.assertEqual(
                runtime_environment.get_default_runtime_root(), self.root / "explicit"
            )
            os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg")
            self.assertEqual(
                default_runtime_home(), self.root / "xdg" / "pcbdraft" / "runtime"
            )


class PlatformRuntimeMigrationTests(unittest.TestCase):
    def test_native_paths_and_guard_roots_share_cross_platform_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            for system, values, expected in (
                ("Linux", {}, home / ".config"),
                ("Linux", {"XDG_CONFIG_HOME": "~/xdg"}, home / "xdg"),
                ("Darwin", {}, home / "Library" / "Application Support"),
                ("Windows", {}, home / "AppData" / "Roaming"),
                ("Windows", {"APPDATA": "~/roaming"}, home / "roaming"),
            ):
                with (
                    self.subTest(system=system, values=values),
                    patch.dict(
                        os.environ,
                        {"HOME": str(home), "USERPROFILE": str(home), **values},
                        clear=True,
                    ),
                    patch.object(
                        platform_paths.platform, "system", return_value=system
                    ),
                    patch.object(
                        Path, "home", side_effect=AssertionError("patched home")
                    ),
                    patch.object(
                        Path, "read_bytes", side_effect=AssertionError("credentials")
                    ),
                ):
                    self.assertEqual(
                        platform_paths.user_config_home(home=home), expected
                    )
                    self.assertEqual(
                        platform_paths.production_runtime_roots(),
                        (expected / "pcbdraft" / "runtime",),
                    )

    def test_first_party_diagnostics_use_real_package_and_public_command(self) -> None:
        self.assertTrue(
            runtime_environment.is_first_party_module("pcbdraft.model.auth")
        )
        for name in ("hermes_cli", "hermes_state", "agent", "agents", None):
            self.assertFalse(runtime_environment.is_first_party_module(name))
        hint = runtime_environment.partial_update_hint(
            ImportError("missing", name="pcbdraft.model.auth")
        )
        self.assertIn("    pcbdraft doctor", hint)
        self.assertNotIn("pcbdraft update", "\n".join(hint))


if __name__ == "__main__":
    unittest.main()
