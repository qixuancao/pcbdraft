from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
