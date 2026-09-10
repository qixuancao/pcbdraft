"""Review regressions: isolated Git plumbing, process races, no remote services."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _migration_worker(base, entered, release, after_transaction, results):
    from pcbdraft.tools import checkpoint_manager as checkpoints

    checkpoints.CHECKPOINT_BASE = Path(base)
    original = checkpoints._run_git

    def controlled(args, *positional, **kwargs):
        transaction = "update-ref" in args and "--stdin" in args
        if transaction and not after_transaction:
            entered.set()
            if not release.wait(15):
                raise TimeoutError("migration barrier")
        result = original(args, *positional, **kwargs)
        if transaction and after_transaction:
            entered.set()
            if not release.wait(15):
                raise TimeoutError("migration barrier")
        return result

    try:
        with patch.object(checkpoints, "_run_git", side_effect=controlled):
            error = checkpoints._copy_legacy_refs(Path(base) / "store", base)
        results.put(error)
    except Exception as exc:  # noqa: BLE001 - relay worker failures to the parent test
        results.put(repr(exc))


def _maintenance_worker(base, ref, operation, started, done, results):
    from pcbdraft.tools import checkpoint_manager as checkpoints

    checkpoints.CHECKPOINT_BASE = Path(base)
    started.set()
    try:
        if operation == "prune":
            result = checkpoints.prune_checkpoints(
                retention_days=1, delete_orphans=False, checkpoint_base=Path(base)
            )
        elif operation == "clear":
            result = checkpoints.clear_all(Path(base))
        else:
            result = checkpoints._delete_ref(Path(base) / "store", ref)
        results.put(result)
    except Exception as exc:  # noqa: BLE001 - relay worker failures to the parent test
        results.put(repr(exc))
    finally:
        done.set()


class _RuntimeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pcbdraft-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)


class OfflineReviewTests(_RuntimeCase):
    def test_tirith_circuit_limits_spawn_without_relaxing_failure_policy(self):
        from pcbdraft.tools import tirith_security as tirith

        for fail_open in (False, True):
            with self.subTest(fail_open=fail_open):
                cfg = {
                    "tirith_enabled": True,
                    "tirith_path": "tirith",
                    "tirith_timeout": 1,
                    "tirith_fail_open": fail_open,
                    "tirith_allow_download": False,
                }
                with (
                    patch.object(tirith, "_load_security_config", return_value=cfg),
                    patch.object(tirith, "_resolved_path", None),
                    patch.object(tirith, "_circuit_open", False),
                    patch.object(tirith, "_crash_count", 0),
                    patch.object(tirith, "_CRASH_LIMIT", 3),
                    patch.object(tirith, "is_platform_supported", return_value=True),
                    patch.object(tirith.shutil, "which", return_value=None),
                    patch.object(tirith, "_install_tirith") as install,
                    patch.object(tirith.threading, "Thread") as thread,
                    patch.object(tirith.urllib.request, "urlopen") as download,
                    patch.object(tirith.logger, "warning"),
                    patch.object(
                        tirith.subprocess,
                        "run",
                        side_effect=FileNotFoundError(2, "scanner missing"),
                    ) as spawn,
                ):
                    results = [tirith.check_command_security("pwd") for _ in range(6)]
                    expected = "allow" if fail_open else "block"
                    self.assertEqual([r["action"] for r in results], [expected] * 6)
                    self.assertTrue(tirith._circuit_open)
                    self.assertEqual(spawn.call_count, 3)
                    for result in results[3:]:
                        self.assertIn("circuit breaker", result["summary"])
                        self.assertIn(
                            "fail-open" if fail_open else "fail-closed",
                            result["summary"],
                        )
                    # An already-open breaker must honor a changed policy too.
                    cfg["tirith_fail_open"] = not fail_open
                    self.assertEqual(
                        tirith.check_command_security("pwd")["action"],
                        "block" if fail_open else "allow",
                    )
                    self.assertEqual(spawn.call_count, 3)
                    install.assert_not_called()
                    thread.assert_not_called()
                    download.assert_not_called()

    def test_tirith_missing_defaults_do_not_download_or_spawn_install_thread(self):
        from pcbdraft.tools import tirith_security as tirith

        cfg = {
            "tirith_enabled": True,
            "tirith_path": "tirith",
            "tirith_timeout": 1,
            "tirith_fail_open": False,
        }
        with (
            patch.object(tirith, "_load_security_config", return_value=cfg),
            patch.object(tirith, "_resolved_path", None),
            patch.object(tirith, "_circuit_open", False),
            patch.object(tirith, "_crash_count", 0),
            patch.object(tirith, "is_platform_supported", return_value=True),
            patch.object(tirith.shutil, "which", return_value=None),
            patch.object(tirith, "_install_tirith") as install,
            patch.object(tirith.threading, "Thread") as thread,
            patch.object(tirith.subprocess, "run", side_effect=FileNotFoundError),
        ):
            self.assertIsNone(tirith.ensure_installed())
            self.assertEqual(tirith._resolve_tirith_path("tirith"), "tirith")
            self.assertEqual(tirith.check_command_security("pwd")["action"], "block")
            install.assert_not_called()
            thread.assert_not_called()
            self.assertFalse(Path(tirith._failure_marker_path()).exists())

    def test_tirith_download_requires_explicit_opt_in(self):
        from pcbdraft.tools import tirith_security as tirith

        with (
            patch.object(tirith, "_resolved_path", None),
            patch.object(tirith, "_install_thread", None),
            patch.object(tirith, "is_platform_supported", return_value=True),
            patch.object(tirith.shutil, "which", return_value=None),
            patch.object(
                tirith, "_install_tirith", return_value=("/installed/tirith", "")
            ) as install,
        ):
            self.assertEqual(
                tirith._resolve_tirith_path("tirith", allow_download=False), "tirith"
            )
            install.assert_not_called()
            self.assertEqual(
                tirith._resolve_tirith_path("tirith", allow_download=True),
                "/installed/tirith",
            )
            install.assert_called_once()

    def test_tirith_installed_binary_remains_available_offline(self):
        from pcbdraft.tools import tirith_security as tirith

        binary = self.root / "bin" / "tirith"
        binary.parent.mkdir()
        binary.write_text("installed")
        binary.chmod(0o700)
        with (
            patch.object(tirith, "_resolved_path", None),
            patch.object(tirith, "is_platform_supported", return_value=False),
            patch.object(tirith.shutil, "which", return_value=None),
            patch.object(
                tirith,
                "_load_security_config",
                return_value={"tirith_enabled": True, "tirith_path": "tirith"},
            ),
            patch.object(tirith, "_install_tirith") as install,
        ):
            self.assertEqual(tirith.ensure_installed(allow_download=False), str(binary))
            install.assert_not_called()

    def test_tirith_config_download_gate_is_independent_of_enable(self):
        from pcbdraft.tools import tirith_security as tirith

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pcbdraft.model.configuration.load_config_readonly", return_value={}),
        ):
            self.assertTrue(tirith._load_security_config()["tirith_enabled"])
            self.assertFalse(tirith._load_security_config()["tirith_allow_download"])
            with patch.dict(os.environ, {"TIRITH_ALLOW_DOWNLOAD": "1"}):
                self.assertTrue(tirith._load_security_config()["tirith_allow_download"])

    def test_singularity_explicit_mounts_match_runtime_and_media_paths(self):
        from pcbdraft.tools import credential_files, image_generation_tool
        from pcbdraft.tools.environments.singularity import SingularityEnvironment

        env = SingularityEnvironment.__new__(SingularityEnvironment)
        env.executable = "apptainer"
        env._persistent = False
        env._overlay_dir = None
        env._memory = env._cpu = 0
        env.image = "fixture.sif"
        env.instance_id = "pcbdraft-fixture"
        env._instance_started = False
        self.addCleanup(setattr, env, "_instance_started", False)
        root = "/root/.pcbdraft/runtime"
        credential = {
            "host_path": str(self.root / "token.json"),
            "container_path": root + "/token.json",
        }
        skill = {
            "host_path": str(self.root / "skills"),
            "container_path": root + "/skills",
        }
        with (
            patch.object(
                credential_files,
                "get_credential_file_mounts",
                return_value=[credential],
            ),
            patch.object(
                credential_files, "get_skills_directory_mount", return_value=[skill]
            ),
            patch(
                "pcbdraft.tools.environments.singularity.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            patch.dict(os.environ, {"TERMINAL_ENV": "singularity"}),
        ):
            env._start_instance()
            command = run.call_args.args[0]
            self.assertIn("--containall", command)
            self.assertIn("--no-home", command)
            self.assertIn(f"{credential['host_path']}:{root}/token.json:ro", command)
            self.assertIn(f"{skill['host_path']}:{root}/skills:ro", command)
            image = self.root / "cache/images/late.png"
            image.write_bytes(b"late media")
            self.assertIn(f"{image.parent}:{root}/cache/images:ro", command)
            visible = credential_files.to_agent_visible_cache_path(str(image))
            self.assertEqual(visible, root + "/cache/images/late.png")
            self.assertEqual(
                credential_files.from_agent_visible_cache_path(visible), str(image)
            )
            self.assertEqual(
                image_generation_tool._agent_visible_cache_path(str(image), env),
                visible,
            )
            self.assertEqual(env._remote_runtime_home, root)

    def test_singularity_mount_preparation_failure_does_not_start_container(self):
        from pcbdraft.tools.environments.singularity import SingularityEnvironment

        env = SingularityEnvironment.__new__(SingularityEnvironment)
        env.executable = "apptainer"
        env._persistent = False
        env._instance_started = False
        with (
            patch(
                "pcbdraft.tools.credential_files.get_credential_file_mounts",
                side_effect=OSError("unreadable"),
            ),
            patch("pcbdraft.tools.environments.singularity.subprocess.run") as run,
            self.assertRaisesRegex(RuntimeError, "runtime mounts"),
        ):
            env._start_instance()
        run.assert_not_called()

    def test_metadata_adapter_uses_shared_agent_reader(self):
        from pcbdraft.agent.legacy_compat import read_skill_metadata
        from pcbdraft.tools.legacy_metadata import read_pcbdraft_metadata

        metadata = {
            "hermes": {"tags": ["old"], "related_skills": ["keep"]},
            "pcbdraft": {"tags": []},
        }
        with patch(
            "pcbdraft.agent.legacy_compat.read_skill_metadata",
            wraps=read_skill_metadata,
        ) as reader:
            self.assertEqual(
                read_pcbdraft_metadata(metadata),
                {"tags": [], "related_skills": ["keep"]},
            )
            reader.assert_called_once_with({"metadata": metadata})


@unittest.skipUnless(shutil.which("git"), "local Git plumbing is required")
class CheckpointReviewTests(_RuntimeCase):
    def setUp(self):
        super().setUp()
        from pcbdraft.tools import checkpoint_manager as checkpoints

        self.checkpoints = checkpoints
        self.base = self.root / "checkpoints"
        self.base.mkdir()
        self.store = self.base / "store"
        self.work = self.root / "project"
        self.work.mkdir()
        override = patch.object(checkpoints, "CHECKPOINT_BASE", self.base)
        override.start()
        self.addCleanup(override.stop)
        self.git("init", "--bare", str(self.store))
        (self.store / "indexes").mkdir()
        self.sha = self.commit_object("old content")
        self.other_sha = self.commit_object("new content")
        self.project_hash = checkpoints._project_hash(str(self.work))
        self.old_ref = "refs/hermes/" + self.project_hash
        self.ref = checkpoints._ref_name(self.project_hash)
        self.git("update-ref", self.old_ref, self.sha)
        checkpoints._register_project(self.store, str(self.work))

    def git(self, *args, input_text=None, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        )
        command = ["git", "--git-dir", str(self.store), *args]
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            env=env,
            cwd=self.root,
            check=check,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def commit_object(self, content):
        # Write fixture objects, not working-branch commits; no git commit/push.
        blob = self.git("hash-object", "-w", "--stdin", input_text=content)
        tree = self.git("mktree", input_text=f"100644 blob {blob}\tboard.txt\n")
        commit = (
            f"tree {tree}\nauthor Fixture <fixture@local> 1700000000 +0000\n"
            "committer Fixture <fixture@local> 1700000000 +0000\n\nfixture\n"
        )
        return self.git(
            "hash-object", "-t", "commit", "-w", "--stdin", input_text=commit
        )

    def ref_value(self, ref):
        return self.git("show-ref", "--verify", "--hash", ref, check=False)

    def migrate(self):
        return self.checkpoints._copy_legacy_refs(self.store, str(self.base))

    def test_first_list_migrates_before_reading_native_history(self):
        manager = self.checkpoints.CheckpointManager()
        entries = manager.list_checkpoints(str(self.work))
        self.assertEqual([entry["hash"] for entry in entries], [self.sha])
        self.assertEqual(self.ref_value(self.ref), self.sha)
        self.assertEqual(self.ref_value(self.old_ref), self.sha)
        self.assertIsNotNone(self.ref_value(self.checkpoints._MIGRATION_REF))

    def test_first_status_migrates_before_counting_commits(self):
        status = self.checkpoints.store_status(self.base)
        self.assertEqual(status["projects"][0]["commits"], 1)
        self.assertEqual(self.ref_value(self.ref), self.sha)

    def test_first_restore_opens_migrated_store_before_rollback(self):
        (self.work / "board.txt").write_text("user working tree")
        manager = self.checkpoints.CheckpointManager()
        with patch.object(manager, "_take", return_value=False):
            result = manager.restore(str(self.work), self.sha, file_path="board.txt")
        self.assertTrue(result["success"], result)
        self.assertEqual((self.work / "board.txt").read_text(), "old content")
        self.assertEqual(self.ref_value(self.ref), self.sha)

    def test_existing_native_conflict_is_retained(self):
        self.git("update-ref", self.ref, self.other_sha)
        self.assertIsNone(self.migrate())
        self.assertEqual(self.ref_value(self.ref), self.other_sha)
        self.assertEqual(self.ref_value(self.old_ref), self.sha)

    def test_rejected_batch_publishes_neither_native_refs_nor_completion(self):
        original = self.checkpoints._run_git

        def reject(args, *positional, **kwargs):
            if "update-ref" in args and "--stdin" in args:
                kwargs["input_text"] = kwargs["input_text"].replace(
                    "prepare\n",
                    "create refs/pcbdraft/invalid " + "f" * 40 + "\nprepare\n",
                )
            return original(args, *positional, **kwargs)

        with patch.object(self.checkpoints, "_run_git", side_effect=reject):
            self.assertIn("transaction failed", self.migrate())
        self.assertIsNone(self.ref_value(self.ref))
        self.assertIsNone(self.ref_value(self.checkpoints._MIGRATION_REF))
        self.assertIsNone(self.migrate())
        self.assertEqual(self.ref_value(self.ref), self.sha)

    def test_lost_commit_ack_does_not_replay_after_delete(self):
        original = self.checkpoints._run_git

        def lost_ack(args, *positional, **kwargs):
            result = original(args, *positional, **kwargs)
            if "update-ref" in args and "--stdin" in args:
                return False, "", "lost acknowledgement"
            return result

        with patch.object(self.checkpoints, "_run_git", side_effect=lost_ack):
            self.assertIn("transaction failed", self.migrate())
        self.assertTrue(self.checkpoints._delete_ref(self.store, self.ref))
        self.assertIsNone(self.migrate())
        self.assertIsNone(self.ref_value(self.ref))

    def test_invalid_completion_is_reported_instead_of_empty_history(self):
        bad = self.git("hash-object", "-w", "--stdin", input_text="corrupt")
        self.git("update-ref", self.checkpoints._MIGRATION_REF, bad)
        with self.assertRaisesRegex(RuntimeError, "Invalid checkpoint migration"):
            self.checkpoints.CheckpointManager().list_checkpoints(str(self.work))
        self.assertEqual(self.ref_value(self.old_ref), self.sha)
        self.assertIsNone(self.ref_value(self.ref))

    def test_failed_migration_blocks_prune_and_does_not_stamp_success(self):
        bad = self.git("hash-object", "-w", "--stdin", input_text="corrupt")
        self.git("update-ref", self.checkpoints._MIGRATION_REF, bad)
        archive = self.base / "legacy-old"
        archive.mkdir()
        os.utime(archive, (1, 1))
        with self.assertLogs(self.checkpoints.logger, level="WARNING"):
            result = self.checkpoints.maybe_auto_prune_checkpoints(
                retention_days=1, checkpoint_base=self.base
            )
        self.assertEqual(result["result"]["errors"], 1)
        self.assertIn("error", result)
        self.assertTrue(archive.exists())
        self.assertFalse((self.base / self.checkpoints._PRUNE_MARKER_NAME).exists())
        self.assertEqual(self.ref_value(self.old_ref), self.sha)

    def test_previous_completion_file_is_upgraded_without_resurrection(self):
        (self.store / "pcbdraft-refs-migrated").write_text("1\n")
        self.assertIsNone(self.migrate())
        self.assertIsNotNone(self.ref_value(self.checkpoints._MIGRATION_REF))
        self.assertIsNone(self.ref_value(self.ref))
        self.assertEqual(self.ref_value(self.old_ref), self.sha)

    def test_ref_delete_failure_preserves_project_metadata(self):
        self.assertIsNone(self.migrate())
        ref_lock = self.store / (self.ref + ".lock")
        ref_lock.write_text("another Git writer")
        metadata = self.checkpoints._project_meta_path(self.store, self.project_hash)
        with self.assertLogs(self.checkpoints.logger, level="ERROR"):
            deleted = self.checkpoints._delete_project_checkpoint(
                self.store, self.project_hash
            )
        self.assertFalse(deleted)
        self.assertTrue(metadata.exists())
        self.assertEqual(self.ref_value(self.ref), self.sha)

    def _stop_process(self, process):
        if process.is_alive():
            process.terminate()
        process.join(5)

    def _racing_maintenance(self, operation, after_transaction):
        ctx = multiprocessing.get_context("spawn")
        entered, release, started, done = [ctx.Event() for _ in range(4)]
        migration_results, maintenance_results = ctx.Queue(), ctx.Queue()
        # Make this project's retention metadata stale, without touching its data.
        metadata = self.checkpoints._project_meta_path(self.store, self.project_hash)
        import json

        value = json.loads(metadata.read_text())
        value["last_touch"] = 1
        metadata.write_text(json.dumps(value))
        migration = ctx.Process(
            target=_migration_worker,
            args=(
                str(self.base),
                entered,
                release,
                after_transaction,
                migration_results,
            ),
        )
        migration.start()
        self.addCleanup(self._stop_process, migration)
        self.assertTrue(entered.wait(8), "migration never entered barrier")
        maintenance = ctx.Process(
            target=_maintenance_worker,
            args=(
                str(self.base),
                self.ref,
                operation,
                started,
                done,
                maintenance_results,
            ),
        )
        maintenance.start()
        self.addCleanup(self._stop_process, maintenance)
        self.assertTrue(started.wait(8))
        self.assertFalse(
            done.wait(0.2), "maintenance bypassed migration's cross-process lock"
        )
        release.set()
        migration.join(8)
        maintenance.join(8)
        self.assertEqual(migration.exitcode, 0)
        self.assertEqual(maintenance.exitcode, 0)
        self.assertIsNone(migration_results.get(timeout=2))
        result = maintenance_results.get(timeout=2)
        self.assertNotIsInstance(result, str)
        if operation == "clear":
            self.assertTrue(result["deleted"])
            self.assertFalse(self.base.exists())
            self.assertTrue(any((self.root / ".checkpoint-locks").glob("*.lock")))
        else:
            self.assertIsNone(self.ref_value(self.ref))
            self.assertIsNone(self.migrate())
            self.assertIsNone(
                self.ref_value(self.ref), "legacy ref resurrected after maintenance"
            )

    def test_prune_waits_for_migration_and_cannot_resurrect_deleted_ref(self):
        self._racing_maintenance("prune", False)

    def test_delete_waits_until_migration_transaction_releases_lock(self):
        self._racing_maintenance("delete", True)

    def test_clear_all_shares_non_deletable_lock(self):
        self._racing_maintenance("clear", False)

    def test_crash_after_ref_transaction_keeps_completion_reliable(self):
        ctx = multiprocessing.get_context("spawn")
        entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        migration = ctx.Process(
            target=_migration_worker,
            args=(str(self.base), entered, release, True, results),
        )
        migration.start()
        self.addCleanup(self._stop_process, migration)
        self.assertTrue(entered.wait(8))
        migration.terminate()
        migration.join(5)
        self.assertIsNotNone(self.ref_value(self.checkpoints._MIGRATION_REF))
        self.assertTrue(self.checkpoints._delete_ref(self.store, self.ref))
        self.assertIsNone(self.migrate())
        self.assertIsNone(self.ref_value(self.ref))

    def test_crash_before_ref_transaction_is_retryable(self):
        ctx = multiprocessing.get_context("spawn")
        entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        migration = ctx.Process(
            target=_migration_worker,
            args=(str(self.base), entered, release, False, results),
        )
        migration.start()
        self.addCleanup(self._stop_process, migration)
        self.assertTrue(entered.wait(8))
        migration.terminate()
        migration.join(5)
        self.assertIsNone(self.ref_value(self.checkpoints._MIGRATION_REF))
        self.assertIsNone(self.ref_value(self.ref))
        self.assertIsNone(self.migrate())
        self.assertEqual(self.ref_value(self.ref), self.sha)


if __name__ == "__main__":
    unittest.main()
