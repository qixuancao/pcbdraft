from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pcbdraft_public_example_runner_hardened",
        ROOT / "scripts" / "run-public-examples.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load public-example runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


class PublicExampleRunnerHardeningTests(unittest.TestCase):
    """Offline runner contracts; no provider or KiCad command is invoked."""

    def test_worker_environment_drops_inherited_workspace_overrides(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": "/dirty/src",
                "PYTHONHOME": "/dirty/python",
                "PCBDRAFT_HOME": "/inherited/workspace",
                "PCBDRAFT_REPOSITORY_CONFIG": "/inherited/repository.json",
            },
            clear=False,
        ):
            env = RUNNER._worker_environment(
                Path("/private/runtime"), Path("/private/app/config.json")
            )
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PCBDRAFT_HOME", env)
        self.assertNotIn("PCBDRAFT_REPOSITORY_CONFIG", env)
        self.assertEqual(env["PCBDRAFT_RUNTIME_HOME"], "/private/runtime")
        self.assertEqual(env["PCBDRAFT_CONFIG"], "/private/app/config.json")

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_runtime_template_rejects_internal_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime-template"
            destination = root / "runtime-copy"
            target = root / "outside"
            source.mkdir()
            (source / "nested").mkdir()
            target.mkdir()
            (target / "auth.json").write_text("private", encoding="utf-8")
            (source / "nested" / "linked-auth.json").symlink_to(target / "auth.json")
            with self.assertRaisesRegex(RuntimeError, "must not contain symlinks"):
                RUNNER._copy_runtime_template(source, destination)
            self.assertFalse(destination.exists())

    def test_runtime_probe_records_the_launcher_interpreter_version(self) -> None:
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "python_version": "3.12.9",
                    "package_version": "0.1.0",
                    "configured": True,
                    "usable": True,
                    "provider": "offline-provider",
                    "model": "offline-model",
                    "auth_kind": "isolated",
                }
            ),
        )
        with mock.patch.object(RUNNER.subprocess, "run", return_value=response) as run:
            value = RUNNER._runtime_probe(
                Path("/venv/bin/python"), {}, Path("/private/case")
            )
        self.assertEqual(value["python_version"], "3.12.9")
        self.assertEqual(run.call_args.args[0][0], "/venv/bin/python")
        self.assertIn("python_version", run.call_args.args[0][2])

    def test_worker_output_is_combined_and_hard_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = RUNNER._run_worker(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys;"
                        "sys.stdout.buffer.write(b'x' * 1000000);"
                        "sys.stdout.flush()"
                    ),
                ],
                cwd=Path(temporary),
                env={"PATH": os.environ.get("PATH", "")},
                timeout=5,
                max_output_bytes=2048,
            )
        self.assertTrue(result[5])
        self.assertLessEqual(len(result[2]) + len(result[3]), 2048)

    def test_parent_interruption_invokes_child_termination(self) -> None:
        process = mock.Mock()
        process.pid = 12345
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.poll.return_value = None
        with (
            mock.patch.object(RUNNER.subprocess, "Popen", return_value=process),
            mock.patch.object(RUNNER.time, "sleep", side_effect=KeyboardInterrupt),
            mock.patch.object(RUNNER, "_terminate") as terminate,
            self.assertRaises(KeyboardInterrupt),
        ):
            RUNNER._run_worker(
                ["/venv/bin/python", "-c", "pass"],
                cwd=Path("/private/case"),
                env={},
                timeout=5,
                max_output_bytes=1024,
            )
        terminate.assert_called_once_with(process)

    def _run_args(self, root: Path, python: Path) -> object:
        source = root / "source"
        source.mkdir()
        template = root / "runtime-template"
        template.mkdir()
        (template / "provider.json").write_text("offline", encoding="utf-8")
        return RUNNER._parser().parse_args(
            [
                "--python",
                str(python),
                "--source-root",
                str(source),
                "--runtime-template",
                str(template),
                "--private-root",
                str(root / "runs"),
                "--wall-timeout-seconds",
                "5",
                "--max-output-bytes",
                "1024",
            ]
        )

    def test_runner_preserves_launcher_records_version_and_continues_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "venv" / "bin" / "python"
            launcher.parent.mkdir(parents=True)
            try:
                launcher.symlink_to(Path(sys.executable))
            except OSError as exc:
                self.skipTest(f"cannot create interpreter launcher symlink: {exc}")
            args = self._run_args(root, launcher)
            worker_argv: list[list[str]] = []
            worker_calls = 0

            def runtime_probe(
                python: Path, env: dict[str, str], cwd: Path
            ) -> dict[str, object]:
                del cwd
                self.assertEqual(python, launcher.absolute())
                self.assertNotIn("PCBDRAFT_HOME", env)
                return {
                    "python_version": "3.13.14",
                    "package_version": "0.1.0",
                    "configured": True,
                    "usable": True,
                    "provider": "offline-provider",
                    "model": "offline-model",
                    "auth_kind": "isolated",
                }

            def worker(
                argv: list[str],
                *,
                cwd: Path,
                env: dict[str, str],
                timeout: int,
                max_output_bytes: int,
            ) -> tuple[int, bool, bytes, bytes, float, bool]:
                nonlocal worker_calls
                del cwd, env, timeout, max_output_bytes
                worker_calls += 1
                worker_argv.append(argv)
                if worker_calls == 1:
                    raise RuntimeError("synthetic worker failure")
                return 0, False, b"", b"", 0.01, False

            with (
                mock.patch.object(
                    RUNNER, "_source_identity", return_value={"commit": "a" * 40}
                ),
                mock.patch.object(
                    RUNNER, "_checked_output", return_value="offline-kicad"
                ),
                mock.patch.object(RUNNER, "_runtime_probe", side_effect=runtime_probe),
                mock.patch.object(RUNNER, "_run_worker", side_effect=worker),
                mock.patch.object(
                    RUNNER,
                    "_inventory",
                    return_value={
                        "project_count": 1,
                        "native_artifacts": [],
                        "board_svgs": [],
                        "receipts": [],
                    },
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                status = RUNNER._run(args)

            self.assertEqual(status, 1)
            self.assertEqual(worker_calls, len(RUNNER.CASES))
            self.assertTrue(
                all(argv[0] == str(launcher.absolute()) for argv in worker_argv)
            )
            manifest_path = next((root / "runs").glob("tutorial-*/manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["cases"]), 3)
            self.assertEqual(manifest["cases"][0]["failure_reason"], "worker_failed")
            self.assertEqual(manifest["cases"][1]["worker_python_version"], "3.13.14")
            self.assertEqual(
                manifest["cases"][1]["worker_python_launcher"],
                str(launcher.absolute()),
            )
            first_case = manifest_path.parent / RUNNER.CASES[0][0]
            self.assertTrue((first_case / "failure.json").is_file())
            self.assertFalse(
                (manifest_path.parent / RUNNER.CASES[1][0] / "failure.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
