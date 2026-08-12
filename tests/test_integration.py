from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pcb_agent.errors import PcbAgentError, TransactionRejected, ValidationError
from pcb_agent.workflows import run_apply, run_patch, run_review

ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"


def make_project(parent: Path) -> Path:
    project = parent / "project"
    project.mkdir()
    (project / "demo.kicad_sch").write_text(
        "(kicad_sch)\nERC_ERROR=0\nERC_WARNING=1\n", encoding="utf-8"
    )
    (project / "demo.kicad_pcb").write_text(
        "(kicad_pcb)\nDRC_ERROR=0\nDRC_WARNING=2\nOLD\n", encoding="utf-8"
    )
    (project / "demo.kicad_pro").write_text("{}\n", encoding="utf-8")
    return project


class OfflineEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for executable in (FAKES / "codex", FAKES / "kicad-cli"):
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    def test_review_produces_complete_evidence_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            runs = root / "runs"
            run_dir = run_review(
                str(project),
                output_parent=str(runs),
                timeout=30,
                kicad_executable=str(FAKES / "kicad-cli"),
                codex_executable=str(FAKES / "codex"),
            )
            expected = {
                "gates/erc.json",
                "gates/drc.json",
                "evidence.json",
                "inventory.json",
                "semantic-context.json",
                "semantic/schematic.netlist.xml",
                "semantic/board-stats.json",
                "semantic/board-netlist.d356",
                "codex-events.jsonl",
                "report.json",
                "report.md",
                "receipt.json",
            }
            present = {
                path.relative_to(run_dir).as_posix()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            self.assertTrue(expected.issubset(present))
            receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "complete")
            self.assertTrue(receipt["codex"]["completed"])
            self.assertTrue(receipt["codex"]["schema_valid"])
            self.assertTrue(receipt["codex"]["completion_event"])
            self.assertEqual(
                receipt["tool_versions"]["codex"]["version"], "codex-cli 0.147.0-fake"
            )
            self.assertEqual(
                receipt["tool_versions"]["kicad-cli"]["version"], "10.0.5-fake"
            )
            codex_argv = "\0".join(receipt["codex"]["argv"])
            self.assertNotIn("Fake read-only heuristic review", codex_argv)
            self.assertNotIn(str(project), codex_argv)
            self.assertIn("<PROJECT>", codex_argv)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("deterministic evidence", report)
            self.assertIn("AI heuristics", report)
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((run_dir / "receipt.json").stat().st_mode), 0o600
            )

    def test_patch_keeps_source_unchanged_then_apply_uses_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            board = project / "demo.kicad_pcb"
            original = board.read_text(encoding="utf-8")
            run_dir = run_patch(
                str(project),
                request="Replace the fixture marker.",
                output_parent=str(root / "runs"),
                timeout=30,
                kicad_executable=str(FAKES / "kicad-cli"),
                codex_executable=str(FAKES / "codex"),
            )
            self.assertEqual(board.read_text(encoding="utf-8"), original)
            ready = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(ready["status"], "ready")
            self.assertIn(
                "OLD", (run_dir / "changes.patch").read_text(encoding="utf-8")
            )

            run_apply(
                str(run_dir), kicad_executable=str(FAKES / "kicad-cli"), timeout=30
            )
            self.assertIn("NEW", board.read_text(encoding="utf-8"))
            self.assertIn(
                "OLD",
                (run_dir / "backup" / "demo.kicad_pcb").read_text(encoding="utf-8"),
            )
            applied = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(applied["status"], "applied")

    def test_patch_error_regression_is_rejected_and_source_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            board = project / "demo.kicad_pcb"
            original = board.read_bytes()
            runs = root / "runs"
            with self.assertRaises(TransactionRejected):
                run_patch(
                    str(project),
                    request="CAUSE_REGRESSION",
                    output_parent=str(runs),
                    timeout=30,
                    kicad_executable=str(FAKES / "kicad-cli"),
                    codex_executable=str(FAKES / "codex"),
                )
            run_dir = next(runs.iterdir())
            receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
            change_set = json.loads(
                (run_dir / "change_set.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["status"], "rejected")
            self.assertEqual(change_set["status"], "rejected")
            self.assertTrue(
                any(
                    "error count increased" in reason
                    for reason in receipt["rejection_reasons"]
                )
            )
            self.assertEqual(board.read_bytes(), original)

    def test_apply_rejects_baseline_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            board = project / "demo.kicad_pcb"
            run_dir = run_patch(
                str(project),
                request="Replace the fixture marker.",
                output_parent=str(root / "runs"),
                timeout=30,
                kicad_executable=str(FAKES / "kicad-cli"),
                codex_executable=str(FAKES / "codex"),
            )
            (project / "demo.kicad_pro").write_text(
                '{"drift": true}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "drifted"):
                run_apply(
                    str(run_dir), kicad_executable=str(FAKES / "kicad-cli"), timeout=30
                )
            self.assertIn("OLD", board.read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "backup").exists())

    def test_apply_gate_regression_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            board = project / "demo.kicad_pcb"
            original = board.read_bytes()
            run_dir = run_patch(
                str(project),
                request="Replace the fixture marker.",
                output_parent=str(root / "runs"),
                timeout=30,
                kicad_executable=str(FAKES / "kicad-cli"),
                codex_executable=str(FAKES / "codex"),
            )
            previous = os.environ.get("PCB_AGENT_FAKE_APPLY_REGRESSION")
            os.environ["PCB_AGENT_FAKE_APPLY_REGRESSION"] = "1"
            try:
                with self.assertRaisesRegex(PcbAgentError, "restored"):
                    run_apply(
                        str(run_dir),
                        kicad_executable=str(FAKES / "kicad-cli"),
                        timeout=30,
                    )
            finally:
                if previous is None:
                    os.environ.pop("PCB_AGENT_FAKE_APPLY_REGRESSION", None)
                else:
                    os.environ["PCB_AGENT_FAKE_APPLY_REGRESSION"] = previous
            self.assertEqual(board.read_bytes(), original)
            receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "rolled_back")
            self.assertTrue(receipt["rollback_completed"])

    def test_repository_cli_runs_fake_review_without_environment_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            environment = os.environ.copy()
            environment["PATH"] = f"{FAKES}{os.pathsep}{environment.get('PATH', '')}"
            sentinel = "pcb-agent-secret-sentinel-do-not-record"
            environment["PCB_AGENT_TEST_SECRET"] = sentinel
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "pcb-agent"),
                    "review",
                    str(project),
                    "--output",
                    str(root / "runs"),
                    "--timeout",
                    "30",
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=40,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, result.stderr.decode("utf-8", errors="replace")
            )
            run_dir = next((root / "runs").iterdir())
            combined = result.stdout + result.stderr
            for artifact in run_dir.rglob("*"):
                if artifact.is_file():
                    combined += artifact.read_bytes()
            self.assertNotIn(sentinel.encode("utf-8"), combined)

    def test_review_rejects_output_inside_project_before_creating_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            output = project / "runs"
            with self.assertRaisesRegex(PcbAgentError, "outside"):
                run_review(
                    str(project),
                    output_parent=str(output),
                    timeout=30,
                    kicad_executable=str(FAKES / "kicad-cli"),
                    codex_executable=str(FAKES / "codex"),
                )
            self.assertFalse(output.exists())

    def test_agent_workflow_rejects_project_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            outside = root / "outside.txt"
            outside.write_text("not project data", encoding="utf-8")
            (project / "escape.txt").symlink_to(outside)
            with self.assertRaisesRegex(ValidationError, "symlinks"):
                run_review(
                    str(project),
                    output_parent=str(root / "runs"),
                    timeout=30,
                    kicad_executable=str(FAKES / "kicad-cli"),
                    codex_executable=str(FAKES / "codex"),
                )


if __name__ == "__main__":
    unittest.main()
