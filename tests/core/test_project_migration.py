"""Offline failure regressions for the explicit project metadata migration CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.legacy_migration import migrate_project


class ProjectMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.project / ".hermes"
        self.target = self.project / ".pcbdraft"

    def populate(self) -> dict[str, bytes]:
        payloads = {
            "environment.json": b'{"terminal":{"backend":"local"}}\n',
            "skills/pcb/SKILL.md": b"# PCB skill\n",
            "plugins/pcb/plugin.py": b"ENABLED = True\n",
            "plans/unchanged.md": b"private plan\n",
            "auth.json": b"not-project-metadata\n",
        }
        for relative, payload in payloads.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o600)
        return payloads

    def test_allowlisted_copy_preserves_source_and_is_idempotent(self) -> None:
        payloads = self.populate()
        result = migrate_project(self.project)
        self.assertFalse(result.conflicts)
        for relative, payload in payloads.items():
            self.assertEqual((self.source / relative).read_bytes(), payload)
            if relative.startswith("plans/") or relative == "auth.json":
                self.assertFalse((self.target / relative).exists())
            else:
                destination = self.target / relative
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        before = (self.target / "environment.json").stat().st_mtime_ns
        second = migrate_project(self.project)
        self.assertFalse(second.copied)
        self.assertFalse(second.conflicts)
        self.assertIn("environment.json", second.unchanged)
        self.assertEqual((self.target / "environment.json").stat().st_mtime_ns, before)

    def test_conflicting_file_blocks_all_copies_without_overwriting(self) -> None:
        self.populate()
        self.target.mkdir()
        target = self.target / "environment.json"
        target.write_bytes(b"existing-new-config")
        result = migrate_project(self.project)
        self.assertEqual(result.conflicts, ("environment.json",))
        self.assertFalse(result.copied)
        self.assertEqual(target.read_bytes(), b"existing-new-config")
        self.assertFalse((self.target / "skills").exists())
        self.assertTrue((self.source / "skills" / "pcb" / "SKILL.md").is_file())

    def test_conflict_comparison_uses_bytes_even_with_identical_stat_metadata(
        self,
    ) -> None:
        self.source.mkdir()
        self.target.mkdir()
        source = self.source / "environment.json"
        target = self.target / "environment.json"
        source.write_bytes(b"one")
        target.write_bytes(b"two")
        os.utime(target, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
        result = migrate_project(self.project)
        self.assertEqual(result.conflicts, ("environment.json",))
        self.assertEqual(target.read_bytes(), b"two")

    def test_directory_file_collision_is_reported_without_partial_copy(self) -> None:
        self.populate()
        self.target.mkdir()
        (self.target / "skills").write_bytes(b"existing file")
        result = migrate_project(self.project)
        self.assertEqual(result.conflicts, ("skills",))
        self.assertFalse(result.copied)
        self.assertFalse((self.target / "environment.json").exists())
        self.assertEqual((self.target / "skills").read_bytes(), b"existing file")

    def test_source_and_destination_symlinks_are_rejected_before_copy(self) -> None:
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_bytes(b"untouched")
        for index, (side, relative, dangling) in enumerate(
            (
                (".hermes", "", False),
                (".pcbdraft", "", True),
                (".hermes", "environment.json", True),
                (".hermes", "skills", False),
                (".hermes", "plugins/pcb/link", False),
                (".pcbdraft", "skills/extra-link", True),
                (".pcbdraft", "environment.json", False),
            )
        ):
            with self.subTest(side=side, relative=relative, dangling=dangling):
                project = self.root / f"case-{index}"
                project.mkdir()
                source = project / ".hermes"
                if side != ".hermes" or relative:
                    source.mkdir()
                    if relative != "environment.json" or side != ".hermes":
                        (source / "environment.json").write_bytes(b"{}")
                link = project / side / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(external / "missing" if dangling else external)
                with self.assertRaisesRegex(PCBDraftError, "symbolic"):
                    migrate_project(parent_alias / project.name)
                self.assertEqual(sentinel.read_bytes(), b"untouched")
                self.assertFalse((project / ".pcbdraft-migration-locks").exists())

    def test_project_argument_symlink_is_rejected(self) -> None:
        self.populate()
        alias = self.root / "project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(PCBDraftError, "symbolic"):
            migrate_project(parent_alias / alias.name)
        self.assertFalse(self.target.exists())

    def test_project_under_var_parent_alias_can_migrate(self) -> None:
        physical_var = self.root / "private" / "var"
        physical_var.mkdir(parents=True)
        alias_var = self.root / "var"
        alias_var.symlink_to(physical_var, target_is_directory=True)
        project = physical_var / "project"
        source = project / ".hermes"
        source.mkdir(parents=True)
        (source / "environment.json").write_bytes(b"{}")
        result = migrate_project(alias_var / "project")
        self.assertEqual(result.copied, ("environment.json",))
        self.assertEqual(
            (project / ".pcbdraft" / "environment.json").read_bytes(), b"{}"
        )
        self.assertEqual((source / "environment.json").read_bytes(), b"{}")
        self.assertFalse(migrate_project(alias_var / "project").copied)
        self.assertTrue(alias_var.is_symlink())

    def test_ignored_plans_tree_is_never_followed(self) -> None:
        self.source.mkdir()
        (self.source / "environment.json").write_bytes(b"{}")
        (self.source / "plans").symlink_to(self.root / "missing-plans")
        self.assertFalse(migrate_project(self.project).conflicts)
        self.assertTrue((self.source / "plans").is_symlink())
        self.assertFalse((self.target / "plans").exists())

    def test_exclusive_copy_does_not_overwrite_a_late_destination(self) -> None:
        from pcbdraft.core import legacy_migration

        self.source.mkdir()
        (self.source / "environment.json").write_bytes(b"source")
        original_copy = legacy_migration._copy_project_file

        def race(source, target, mode):
            target.write_bytes(b"late writer")
            original_copy(source, target, mode)

        with (
            patch.object(legacy_migration, "_copy_project_file", side_effect=race),
            self.assertRaises(FileExistsError),
        ):
            migrate_project(self.project)
        self.assertEqual(
            (self.target / "environment.json").read_bytes(), b"late writer"
        )
        self.assertEqual((self.source / "environment.json").read_bytes(), b"source")

    def test_module_cli_requires_project_and_never_discovers_home(self) -> None:
        home = self.root / "home"
        standalone = home / ".hermes"
        standalone.mkdir(parents=True)
        sentinel = standalone / "environment.json"
        sentinel.write_bytes(b"independent-state")
        environment = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HERMES_HOME": str(standalone),
            "PCBDRAFT_HERMES_HOME": str(standalone),
            "PCBDRAFT_RUNTIME_HOME": str(standalone),
        }
        command = [sys.executable, "-m", "pcbdraft.core.legacy_migration"]
        missing = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--project", missing.stderr)
        empty = subprocess.run(
            command + ["--project", str(self.project)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout)["copied"], [])
        self.assertEqual(sentinel.read_bytes(), b"independent-state")
        self.assertFalse(self.target.exists())
        self.assertFalse((home / ".pcbdraft").exists())

    def test_module_cli_handles_tilde_and_reports_conflict_exit_status(self) -> None:
        self.populate()
        command = [
            sys.executable,
            "-m",
            "pcbdraft.core.legacy_migration",
            "--project",
            "~/project",
        ]
        environment = {
            **os.environ,
            "HOME": str(self.root),
            "USERPROFILE": str(self.root),
        }
        first = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)["source_retained"])
        second = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["copied"], [])
        (self.target / "environment.json").write_bytes(b"new-value")
        conflict = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(conflict.returncode, 1, conflict.stderr)
        self.assertEqual(json.loads(conflict.stdout)["conflicts"], ["environment.json"])
        self.assertEqual((self.target / "environment.json").read_bytes(), b"new-value")


if __name__ == "__main__":
    unittest.main()
