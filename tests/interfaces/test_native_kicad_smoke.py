from __future__ import annotations

import importlib.util
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.core.project import sha256_file


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "native-kicad-smoke.py"
    spec = importlib.util.spec_from_file_location("native_kicad_smoke", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("native KiCad smoke script is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeKiCadSmokeScriptTests(unittest.TestCase):
    def test_receipt_rejects_a_failed_kicad_gate(self) -> None:
        module = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "validation.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema": "pcbdraft-validation",
                        "version": 2,
                        "readiness": {"engineering_candidate": True},
                    }
                ),
                encoding="utf-8",
            )
            report_sha256 = sha256_file(report_path)
            receipt = {
                "schema": "pcbdraft-validation-receipt",
                "version": 1,
                "status": "complete",
                "candidate_ready": True,
                "report": report_path.name,
                "report_sha256": report_sha256,
                "tool_runs": {
                    "erc": {"status": "failed", "failure": "exit_code_2"},
                    "drc": {"status": "completed", "failure": None},
                },
            }
            (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            validation = SimpleNamespace(
                output_dir=root,
                report_path=report_path,
                report_sha256=report_sha256,
                candidate_ready=True,
            )

            with self.assertRaisesRegex(
                module.ValidationError, "receipt is inconsistent"
            ):
                module._validated_receipt(validation)

    def test_invalid_timeout_and_existing_output_fail_before_generation(self) -> None:
        module = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / "existing"
            existing.mkdir()
            for output, timeout in (
                (Path(temporary) / "new", math.nan),
                (Path(temporary) / "new", 0.0),
                (existing, 30.0),
            ):
                with (
                    self.subTest(output=output, timeout=timeout),
                    patch.object(module, "generate_managed_project") as generate,
                    self.assertRaisesRegex(
                        module.ValidationError, "timeout|safe, fresh, and absent"
                    ),
                ):
                    module.run_smoke(output, timeout=timeout)
                generate.assert_not_called()

    def test_main_prints_the_canonical_validation_receipt(self) -> None:
        module = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "smoke"
            project_root = output / "project"
            validation_root = output / "validation"
            report_path = validation_root / "validation.json"
            receipt: dict[str, object] = {}
            synchronized = Mock()
            generated = SimpleNamespace(
                project=SimpleNamespace(
                    root=project_root, assert_synchronized=synchronized
                )
            )

            def generate_fixture(_requirements: Path, target: Path) -> object:
                target.mkdir(parents=True)
                return generated

            def validate_fixture(
                _project: object, *, output: Path, timeout: float
            ) -> object:
                self.assertEqual(output, validation_root)
                self.assertEqual(timeout, 42.0)
                output.mkdir()
                report_path.write_text(
                    json.dumps(
                        {
                            "schema": "pcbdraft-validation",
                            "version": 2,
                            "readiness": {"engineering_candidate": True},
                        }
                    ),
                    encoding="utf-8",
                )
                report_sha256 = sha256_file(report_path)
                receipt.update(
                    {
                        "schema": "pcbdraft-validation-receipt",
                        "version": 1,
                        "status": "complete",
                        "candidate_ready": True,
                        "report": report_path.name,
                        "report_sha256": report_sha256,
                        "tool_runs": {
                            "erc": {"status": "completed", "failure": None},
                            "drc": {"status": "completed", "failure": None},
                        },
                    }
                )
                (validation_root / "receipt.json").write_text(
                    json.dumps(receipt), encoding="utf-8"
                )
                return SimpleNamespace(
                    output_dir=validation_root,
                    report_path=report_path,
                    report_sha256=report_sha256,
                    candidate_ready=True,
                )

            stdout = io.StringIO()
            with (
                patch.object(
                    module,
                    "bundled_requirements_path",
                    return_value=Path("acceptance_requirements.json"),
                ),
                patch.object(
                    module, "generate_managed_project", side_effect=generate_fixture
                ) as generate,
                patch.object(
                    module, "validate_managed_project", side_effect=validate_fixture
                ) as validate,
                redirect_stdout(stdout),
            ):
                status = module.main([str(output), "--timeout", "42"])

        self.assertEqual(status, 0)
        generate.assert_called_once_with(
            Path("acceptance_requirements.json"), output / "project"
        )
        synchronized.assert_called_once_with()
        validate.assert_called_once_with(
            generated.project,
            output=output / "validation",
            timeout=42.0,
        )
        self.assertEqual(json.loads(stdout.getvalue()), receipt)

    def test_main_fails_closed_when_validation_rejects_the_board(self) -> None:
        module = _load_script()
        generated = SimpleNamespace(project=SimpleNamespace(assert_synchronized=Mock()))
        validation = SimpleNamespace(candidate_ready=False)
        stderr = io.StringIO()
        with (
            patch.object(module, "generate_managed_project", return_value=generated),
            patch.object(module, "validate_managed_project", return_value=validation),
            redirect_stderr(stderr),
        ):
            status = module.main(["unused"])

        self.assertEqual(status, 1)
        self.assertIn("native KiCad smoke failed", stderr.getvalue())
        self.assertIn("candidate-ready", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
