from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pcbdraft_lq_eda_pilot_runner", ROOT / "scripts" / "run-lq-eda-pilot.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load LQ EDA pilot runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


class LqEdaPilotRunnerTests(unittest.TestCase):
    def _task(self, root: Path) -> Path:
        task = root / "task"
        (task / "input" / "symbols").mkdir(parents=True)
        (task / "input" / "footprints").mkdir(parents=True)
        (task / "input" / "prompt.txt").write_text(
            "Build the PCB from the supplied input resources.", encoding="utf-8"
        )
        (task / "input" / "symbols" / "custom.kicad_sym").write_text(
            "(kicad_symbol_lib)", encoding="utf-8"
        )
        (task / "input" / "footprints" / "custom.pretty").mkdir()
        contract = {
            "schema": "pcbdraft-lq-eda-task-contract",
            "version": 1,
            "task_id": "test-task",
            "input": {
                "prompt": "input/prompt.txt",
                "custom_symbols": [
                    {
                        "library_id": "LQEDA:BCON",
                        "path": "input/symbols/custom.kicad_sym",
                        "sha256": hashlib.sha256(
                            (task / "input/symbols/custom.kicad_sym").read_bytes()
                        ).hexdigest(),
                    }
                ],
                "custom_footprints": [
                    {
                        "library_id": "LQEDA:SOP_BCON",
                        "path": "input/footprints/custom.kicad_mod",
                        "sha256": "0" * 64,
                    }
                ],
                "stock_libraries": {
                    "required": True,
                    "symbol_ids": ["Device:R"],
                    "footprint_ids": ["Resistor_SMD:R_0805_2012Metric"],
                },
            },
        }
        custom_footprint = task / "input/footprints/custom.kicad_mod"
        custom_footprint.write_text('(footprint "SOP_BCON")', encoding="utf-8")
        contract["input"]["custom_footprints"][0]["sha256"] = hashlib.sha256(
            custom_footprint.read_bytes()
        ).hexdigest()
        (task / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
        return task

    def _args(self, root: Path, command: str = "prepare") -> object:
        task = self._task(root)
        template = root / "runtime-template"
        template.mkdir()
        (template / "config.yaml").write_text(
            "model:\n  default: test\n", encoding="utf-8"
        )
        (template / "auth.json").write_text('{"token":"private"}\n', encoding="utf-8")
        stock_symbols = root / "stock-symbols"
        stock_footprints = root / "stock-footprints"
        stock_symbols.mkdir()
        stock_footprints.mkdir()
        (stock_symbols / "Device.kicad_sym").write_text("stock", encoding="utf-8")
        (stock_footprints / "Device.pretty").mkdir()
        return RUNNER._parser().parse_args(
            [
                command,
                "--task",
                str(task),
                "--runtime-template",
                str(template),
                "--private-root",
                str(root / "private-run"),
                "--python",
                sys.executable,
                "--source-root",
                str(root / "source"),
                "--stock-symbol-dir",
                str(stock_symbols),
                "--stock-footprint-dir",
                str(stock_footprints),
            ]
        )

    def test_parser_uses_single_attempt_pilot_defaults(self) -> None:
        args = RUNNER._parser().parse_args(
            [
                "run",
                "--task",
                "/private/task",
                "--runtime-template",
                "/private/runtime",
                "--private-root",
                "/private/run",
            ]
        )
        self.assertEqual(args.wall_timeout_seconds, 900)
        self.assertEqual(args.pcb_tool_call_limit, 120)

    def test_runtime_template_and_task_allow_lists_exclude_answer_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self._task(root)
            template = root / "runtime"
            template.mkdir()
            (template / ".env").write_text("API_KEY=private\n", encoding="utf-8")
            destination_input = root / "copied-input"
            destination_runtime = root / "copied-runtime"
            prompt, input_records = RUNNER._task_input(task, destination_input)
            runtime_records = RUNNER._runtime_template(template, destination_runtime)
            self.assertEqual(prompt, "Build the PCB from the supplied input resources.")
            self.assertTrue((destination_input / "prompt.txt").is_file())
            self.assertTrue(
                (destination_input / "symbols" / "custom.kicad_sym").is_file()
            )
            self.assertFalse((destination_input / "answer-key.md").exists())
            self.assertEqual(
                {item["path"] for item in input_records},
                {
                    "prompt.txt",
                    "symbols/custom.kicad_sym",
                    "footprints/custom.kicad_mod",
                },
            )
            self.assertEqual([item["path"] for item in runtime_records], [".env"])

    def test_worker_environment_is_private_and_sets_both_kicad_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(
                os.environ,
                {"PCBDRAFT_HOME": "/inherited", "PYTHONPATH": "/dirty"},
                clear=False,
            ):
                env = RUNNER._worker_environment_for_run(
                    root,
                    root / "runtime",
                    root / "application" / "config.json",
                    root / "input",
                    {
                        "symbols": {"destination": "libraries/symbols"},
                        "footprints": {"destination": "libraries/footprints"},
                    },
                )
            self.assertNotIn("PCBDRAFT_HOME", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env["KICAD_SYMBOL_DIR"], env["KICAD10_SYMBOL_DIR"])
            self.assertEqual(env["KICAD_FOOTPRINT_DIR"], env["KICAD10_FOOTPRINT_DIR"])
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["PCBDRAFT_LQ_EDA_TOOLSET"], "pcbdraft")

    def test_trace_timing_is_derived_and_marks_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            trace.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"event": "model_request", "data": {"duration_ms": 20}}
                        ),
                        json.dumps(
                            {
                                "event": "tool_end",
                                "data": {
                                    "tool_name": "pcb_add_component",
                                    "duration_ms": 7,
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            timing = RUNNER._trace_timing(trace)
            self.assertEqual(
                timing["stage_counts"], {"model_request": 1, "tool_end": 1}
            )
            self.assertEqual(timing["tool_duration_ms"], {"pcb_add_component": 7.0})
            self.assertIn("overlapping", timing["aggregation_note"])

    def test_run_does_one_worker_attempt_and_records_result_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "src" / "pcbdraft").mkdir(parents=True)
            (source / "src" / "pcbdraft" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            args = self._args(root, "run")
            with (
                mock.patch.object(
                    RUNNER,
                    "_source_identity",
                    return_value={"commit": "a" * 40, "dirty": False, "tree": "b" * 40},
                ),
                mock.patch.object(
                    RUNNER,
                    "_import_probe",
                    return_value={
                        "package_file": str(source / "src" / "pcbdraft" / "__init__.py")
                    },
                ),
                mock.patch.object(
                    RUNNER,
                    "_resolver_probe",
                    return_value={
                        "symbols": ["Device:R", "LQEDA:BCON"],
                        "footprints": [
                            "Resistor_SMD:R_0805_2012Metric",
                            "LQEDA:SOP_BCON",
                        ],
                    },
                ),
                mock.patch.object(
                    RUNNER,
                    "_runtime_probe",
                    return_value={
                        "python_version": "3.13.0",
                        "configured": True,
                        "usable": True,
                    },
                ),
                mock.patch.object(
                    RUNNER,
                    "_run_worker",
                    return_value=(0, False, b"out", b"err", 0.25, False),
                ) as worker,
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
            self.assertEqual(status, 0)
            worker.assert_called_once()
            run_root = root / "private-run"
            manifest = json.loads(
                (run_root / "manifest.json").read_text(encoding="utf-8")
            )
            result = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["limits"]["attempts"], 1)
            self.assertFalse(result["answer_key_supplied_to_worker"])
            self.assertEqual(
                manifest["result"]["sha256"], RUNNER._sha256(run_root / "result.json")
            )
            self.assertEqual(worker.call_args.kwargs["timeout"], 900)
            self.assertNotIn("answer", worker.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
