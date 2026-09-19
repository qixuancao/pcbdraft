from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _load_scorer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pcbdraft_lq_eda_score", ROOT / "scripts" / "score-lq-eda-pilot.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load LQ-EDA scorer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCORE = _load_scorer()


class LqEdaScoreTests(unittest.TestCase):
    def test_private_library_environment_is_visible_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "libraries" / "symbols").mkdir(parents=True)
            (root / "libraries" / "footprints").mkdir(parents=True)
            schematic = root / "main.kicad_sch"
            schematic.write_text("(kicad_sch)", encoding="utf-8")
            project = SimpleNamespace(root=root, schematic_path=schematic)
            observed: dict[str, str] = {}

            def probe(*_args: object, **_kwargs: object) -> object:
                for name in (
                    "KICAD_SYMBOL_DIR",
                    "KICAD10_SYMBOL_DIR",
                    "KICAD_FOOTPRINT_DIR",
                    "KICAD10_FOOTPRINT_DIR",
                ):
                    observed[name] = os.environ[name]
                return SimpleNamespace(
                    timed_out=False, output_limited=False, returncode=0
                )

            original = {
                name: f"original-{name}"
                for name in (
                    "KICAD_SYMBOL_DIR",
                    "KICAD10_SYMBOL_DIR",
                    "KICAD_FOOTPRINT_DIR",
                    "KICAD10_FOOTPRINT_DIR",
                )
            }
            with mock.patch.dict(os.environ, original, clear=False):
                with (
                    mock.patch.object(
                        SCORE, "find_kicad_cli", return_value="/usr/bin/kicad-cli"
                    ),
                    mock.patch.object(SCORE, "run_command", side_effect=probe),
                ):
                    with SCORE._scoped_library_environment(root):
                        SCORE._export_netlist(project, root / "derived", timeout=1.0)
                    self.assertEqual(
                        observed["KICAD_SYMBOL_DIR"],
                        str((root / "libraries" / "symbols").resolve()),
                    )
                    self.assertEqual(
                        observed["KICAD10_FOOTPRINT_DIR"],
                        str((root / "libraries" / "footprints").resolve()),
                    )
                    with (
                        self.assertRaisesRegex(RuntimeError, "restore"),
                        SCORE._scoped_library_environment(root),
                    ):
                        raise RuntimeError("restore")
                for name, value in original.items():
                    self.assertEqual(os.environ[name], value)
            with mock.patch.dict(os.environ, {}, clear=False):
                for name in original:
                    os.environ.pop(name, None)
                with (
                    self.assertRaisesRegex(RuntimeError, "absent"),
                    SCORE._scoped_library_environment(root),
                ):
                    raise RuntimeError("absent")
                for name in original:
                    self.assertNotIn(name, os.environ)

    def test_stock_connector_pinfunction_normalization_preserves_pin_number(
        self,
    ) -> None:
        self.assertEqual(SCORE._canonical_pin_function("Pin_1"), "pin_1")
        self.assertEqual(SCORE._canonical_pin_function("Pin_1_1"), "pin_1")
        self.assertEqual(SCORE._canonical_pin_function("Pin_2"), "pin_2")
        self.assertEqual(SCORE._canonical_pin_function("Pin_2_2"), "pin_2")

    def test_score_run_scopes_private_libraries_for_all_kicad_checks(self) -> None:
        names = (
            "KICAD_SYMBOL_DIR",
            "KICAD10_SYMBOL_DIR",
            "KICAD_FOOTPRINT_DIR",
            "KICAD10_FOOTPRINT_DIR",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            libraries = run / "libraries"
            (libraries / "symbols").mkdir(parents=True)
            (libraries / "footprints").mkdir(parents=True)
            source_project = (
                run / "output" / "repository" / "projects" / "case" / "design"
            )
            source_project.mkdir(parents=True)
            manifest_value = {"design": {"content_hash": "a" * 64}}
            (source_project / SCORE.MANIFEST_NAME).write_text(
                json.dumps(manifest_value), encoding="utf-8"
            )
            answer = root / "answer.json"
            answer.write_text(
                json.dumps(
                    {
                        "schema": SCORE.ANSWER_SCHEMA,
                        "version": 1,
                        "task_id": "scoped-env",
                        "expected_components": {
                            "R1": {
                                "value": "1k",
                                "symbol": "Device:R",
                                "footprint": "Resistor_SMD:R_0805_2012Metric",
                            }
                        },
                        "expected_nets": {"GND": ["R1.1"]},
                        "rules": {"layers": 2},
                    }
                ),
                encoding="utf-8",
            )
            seen: dict[str, dict[str, str]] = {}

            def capture(label: str) -> None:
                seen[label] = {name: os.environ[name] for name in names}

            def copy_project(_source: Path, destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / SCORE.MANIFEST_NAME).write_text(
                    json.dumps(manifest_value), encoding="utf-8"
                )

            def open_project(path: Path) -> SimpleNamespace:
                design = SimpleNamespace(content_hash=lambda: "a" * 64, revision=0)
                return SimpleNamespace(
                    root=path,
                    manifest_path=path / SCORE.MANIFEST_NAME,
                    manifest=manifest_value,
                    design=design,
                    schematic_path=path / "main.kicad_sch",
                    board_path=path / "main.kicad_pcb",
                    assert_synchronized=lambda: None,
                )

            def export(_project: object, output: Path, timeout: float) -> Path:
                del timeout
                capture("export")
                target = output / "netlist.kicadxml"
                target.write_text("<export/>", encoding="utf-8")
                return target

            def inspect(*_args: object, **_kwargs: object) -> dict[str, object]:
                capture("inspect")
                return {}

            def validate(*_args: object, **_kwargs: object) -> dict[str, object]:
                capture("validation")
                return SCORE._check("managed_validation", True)

            with mock.patch.dict(os.environ, {}, clear=False):
                for name in names:
                    os.environ.pop(name, None)
                with (
                    mock.patch.object(
                        SCORE,
                        "_input_contract_binding_check",
                        return_value=SCORE._check("input_contract_binding", True),
                    ),
                    mock.patch.object(
                        SCORE, "_locate_project", return_value=source_project
                    ),
                    mock.patch.object(
                        SCORE,
                        "_artifact_inventory_check",
                        return_value=(SCORE._check("raw_run_receipt", True), "r", "i"),
                    ),
                    mock.patch.object(SCORE, "_copy_project", side_effect=copy_project),
                    mock.patch.object(
                        SCORE, "open_managed_project", side_effect=open_project
                    ),
                    mock.patch.object(SCORE, "_export_netlist", side_effect=export),
                    mock.patch.object(
                        SCORE,
                        "_parse_netlist",
                        return_value=(
                            {
                                "R1": {
                                    "value": "1k",
                                    "symbol": "Device:R",
                                    "footprint": "Resistor_SMD:R_0805_2012Metric",
                                }
                            },
                            {"GND": (SCORE._ObservedEndpoint("R1", "1", None),)},
                        ),
                    ),
                    mock.patch.object(
                        SCORE, "inspect_native_board", side_effect=inspect
                    ),
                    mock.patch.object(SCORE, "_board_checks", return_value=[]),
                    mock.patch.object(SCORE, "_validation_check", side_effect=validate),
                ):
                    score = SCORE.score_run(run, answer, root / "score")
                self.assertEqual(score["overall"]["status"], "pass")
                for label in ("export", "inspect", "validation"):
                    self.assertEqual(
                        seen[label]["KICAD_SYMBOL_DIR"],
                        str((libraries / "symbols").resolve()),
                    )
                    self.assertEqual(
                        seen[label]["KICAD10_FOOTPRINT_DIR"],
                        str((libraries / "footprints").resolve()),
                    )
                for name in names:
                    self.assertNotIn(name, os.environ)

    def test_symmetric_two_terminal_mapping_is_global(self) -> None:
        endpoint = SCORE._Endpoint
        observed = SCORE._ObservedEndpoint
        expected = {
            "A": (endpoint("R1", "1"), endpoint("X1", "1")),
            "B": (endpoint("R1", "2"), endpoint("X1", "2")),
        }
        actual = {
            "A": (observed("R1", "2", None), observed("X1", "1", None)),
            "B": (observed("R1", "1", None), observed("X1", "2", None)),
        }
        passed, details = SCORE._compare_networks(expected, actual, frozenset({"R1"}))
        self.assertTrue(passed)
        self.assertEqual(details["pin_swap_mapping"], {"R1": True})

        inconsistent = {
            "A": (observed("R1", "2", None), observed("X1", "1", None)),
            "B": (observed("R1", "2", None), observed("X1", "2", None)),
        }
        passed, _ = SCORE._compare_networks(expected, inconsistent, frozenset({"R1"}))
        self.assertFalse(passed)

    def test_known_native_rule_failure_is_not_hidden_by_unknown_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "project.kicad_pro"
            settings.write_text(
                json.dumps(
                    {"board": {"design_settings": {"rules": {"min_track_width": 0.1}}}}
                ),
                encoding="utf-8",
            )
            project = SimpleNamespace(project_path=settings)
            result = SCORE._native_rules_check(
                project,
                {"board": {"layers": 2}},
                {
                    "layers": 2,
                    "minimum_track_width_mil": 10,
                    "pad_to_slot_clearance_mil": 7,
                },
            )
        self.assertEqual(result["status"], "fail")

    def test_missing_project_still_writes_bound_score_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            manifest = run / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "result": {"status": "completed", "artifacts": {}},
                    }
                ),
                encoding="utf-8",
            )
            answer = root / "answer.json"
            answer.write_text(
                json.dumps(
                    {
                        "schema": "pcbdraft-lq-eda-answer-key",
                        "version": 1,
                        "task_id": "missing-project",
                        "public_input_binding": {"contract_sha256": "a" * 64},
                        "expected_components": {
                            "R1": {
                                "value": "10k",
                                "symbol": "Device:R",
                                "footprint": "Resistor_SMD:R_0805_2012Metric",
                            }
                        },
                        "expected_nets": {"GND": ["R1.1"]},
                        "rules": {"layers": 2},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "score"
            score = SCORE.score_run(run, answer, output)
            receipt = json.loads((output / "score.json").read_text(encoding="utf-8"))
            self.assertEqual(score["overall"]["status"], "fail")
            self.assertTrue((output / "score.json").is_file())
            self.assertEqual(
                receipt["identity"]["run_receipt_sha256"],
                score["identity"]["run_receipt_sha256"],
            )
            self.assertNotIn("run_sha256", receipt["identity"])
            self.assertEqual(receipt["human_review"]["status"], "not_reviewed")

    def test_runner_shaped_inventory_and_binding_reject_tampering(self) -> None:
        def make_fixture(root: Path) -> tuple[Path, Path, dict[str, str]]:
            run = root / "run"
            project = run / "output" / "repository" / "projects" / "case" / "design"
            project.mkdir(parents=True)
            files = {
                suffix: project / f"main{suffix}"
                for suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb")
            }
            for suffix, path in files.items():
                path.write_text(f"{suffix}\n", encoding="utf-8")
            records = [
                {
                    "path": path.relative_to(run / "output" / "repository").as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": __import__("hashlib")
                    .sha256(path.read_bytes())
                    .hexdigest(),
                }
                for path in files.values()
            ]
            result_value = {
                "status": "completed",
                "artifacts": {
                    "project_count": 1,
                    "native_artifacts": records,
                    "board_svgs": [],
                    "receipts": [],
                },
            }
            result_path = run / "result.json"
            result_path.write_text(json.dumps(result_value), encoding="utf-8")
            manifest = {
                "status": "completed",
                "contract": {"sha256": "c" * 64, "prompt_sha256": "p" * 64},
                "task": {
                    "prompt_sha256": "p" * 64,
                    "input_files": [],
                },
                "result": {
                    **result_value,
                    "path": "result.json",
                    "sha256": __import__("hashlib")
                    .sha256(result_path.read_bytes())
                    .hexdigest(),
                },
            }
            (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return run, project, {suffix: str(path) for suffix, path in files.items()}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, project, files = make_fixture(root)
            check, _, _ = SCORE._artifact_inventory_check(run, project)
            self.assertEqual(check["status"], "pass")

            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            manifest["result"]["artifacts"]["native_artifacts"] = manifest["result"][
                "artifacts"
            ]["native_artifacts"][:-1]
            (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            check, _, _ = SCORE._artifact_inventory_check(run, project)
            self.assertEqual(check["status"], "fail")

            run, project, files = make_fixture(root / "changed-file")
            Path(files[".kicad_pcb"]).write_text("changed\n", encoding="utf-8")
            check, _, _ = SCORE._artifact_inventory_check(run, project)
            self.assertEqual(check["status"], "fail")

            run, project, _ = make_fixture(root / "changed-result")
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            manifest["result"]["sha256"] = "d" * 64
            (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            check, _, _ = SCORE._artifact_inventory_check(run, project)
            self.assertEqual(check["status"], "fail")

            run, project, _ = make_fixture(root / "failed")
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            manifest["status"] = "failed"
            manifest["result"]["status"] = "failed"
            (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            check, _, _ = SCORE._artifact_inventory_check(run, project)
            self.assertEqual(check["status"], "fail")

            binding_root = root / "binding"
            (binding_root / "input" / "symbols").mkdir(parents=True)
            (binding_root / "input" / "footprints").mkdir()
            prompt = binding_root / "input" / "prompt.txt"
            symbol = binding_root / "input" / "symbols" / "LQEDA.kicad_sym"
            footprint = binding_root / "input" / "footprints" / "LQEDA.kicad_mod"
            prompt.write_text("prompt", encoding="utf-8")
            symbol.write_text("symbol", encoding="utf-8")
            footprint.write_text("footprint", encoding="utf-8")
            digest = lambda path: (
                __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            )
            prompt_hash, symbol_hash, footprint_hash = map(
                digest, (prompt, symbol, footprint)
            )
            manifest = {
                "status": "completed",
                "contract": {"sha256": "e" * 64, "prompt_sha256": prompt_hash},
                "task": {
                    "prompt_sha256": prompt_hash,
                    "input_files": [
                        {"path": "prompt.txt", "sha256": prompt_hash},
                        {"path": "symbols/LQEDA.kicad_sym", "sha256": symbol_hash},
                        {
                            "path": "footprints/LQEDA.kicad_mod",
                            "sha256": footprint_hash,
                        },
                    ],
                },
            }
            (binding_root / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            answer = {
                "public_input_binding": {
                    "contract_sha256": "e" * 64,
                    "prompt_sha256": prompt_hash,
                    "resource_sha256": {
                        "input/symbols/LQEDA.kicad_sym": symbol_hash,
                        "input/footprints/LQEDA.kicad_mod": footprint_hash,
                    },
                }
            }
            self.assertEqual(
                SCORE._input_contract_binding_check(binding_root, answer)["status"],
                "pass",
            )
            symbol.write_text("tampered", encoding="utf-8")
            self.assertEqual(
                SCORE._input_contract_binding_check(binding_root, answer)["status"],
                "fail",
            )


if __name__ == "__main__":
    unittest.main()
