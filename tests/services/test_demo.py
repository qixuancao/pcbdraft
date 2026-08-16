from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.interfaces.cli import main as cli_main
from pcbdraft.services.demo import DEMO_SENTENCE, run_first_board_demo


class FirstBoardDemoTests(unittest.TestCase):
    def test_one_sentence_generates_disclosed_reference_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "first-board"
            project = SimpleNamespace(
                root=target,
                schematic_path=target / "demo.kicad_sch",
                board_path=target / "demo.kicad_pcb",
                project_path=target / "demo.kicad_pro",
            )
            generated = SimpleNamespace(project=project)
            validation = SimpleNamespace(
                candidate_ready=True,
                report_path=target / "evidence" / "validation.json",
            )
            with (
                patch(
                    "pcbdraft.services.demo.generate_managed_project",
                    return_value=generated,
                ) as generate,
                patch(
                    "pcbdraft.services.demo.validate_managed_project",
                    return_value=validation,
                ) as validate,
            ):
                result = run_first_board_demo(DEMO_SENTENCE, output=target)

            self.assertEqual(result["project"], str(target))
            self.assertIn("deterministic", result["mapping"])
            self.assertTrue(result["validation"]["candidate_ready"])
            self.assertEqual(generate.call_args.args[1], target)
            validate.assert_called_once()

    def test_unrelated_sentence_is_not_silently_mapped_to_fixture(self) -> None:
        with self.assertRaisesRegex(ValidationError, "intentionally fixed"):
            run_first_board_demo("做一个电机控制器", output="unused")

    def test_demo_cli_is_a_single_command_path(self) -> None:
        value = {
            "kicad_project": "/tmp/demo/demo.kicad_pro",
            "validation": {"candidate_ready": True, "report": "/tmp/report.json"},
        }
        output = StringIO()
        with (
            patch("pcbdraft.interfaces.cli.run_first_board_demo", return_value=value),
            redirect_stdout(output),
        ):
            code = cli_main(["demo", DEMO_SENTENCE])
        self.assertEqual(code, 0)
        self.assertIn("First board generated", output.getvalue())


if __name__ == "__main__":
    unittest.main()
