from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = _load_script("pcbdraft_public_example_runner", "run-public-examples.py")
EXPORTER = _load_script("pcbdraft_public_example_exporter", "export-public-examples.py")


class PublicExampleRunnerTests(unittest.TestCase):
    """Offline contract tests; no provider or KiCad command is invoked."""

    def test_exact_three_prompts_are_natural_language_only(self) -> None:
        self.assertEqual(
            tuple(case_id for case_id, _filename in RUNNER.CASES),
            ("led-3v3-330r", "rc-1k-100nf", "i2c-3v3-pullups"),
        )
        for _case_id, filename in RUNNER.CASES:
            value = (ROOT / "examples" / "prompts" / filename).read_text(
                encoding="utf-8"
            )
            self.assertTrue(value.strip())
            self.assertNotIn("CircuitPlan", value)
            self.assertIn("stock KiCad", value)
            self.assertIn("non-safety-critical", value)
            self.assertIn("board.svg", value)

    def test_defaults_are_three_single_attempts_with_bounded_limits(self) -> None:
        args = RUNNER._parser().parse_args(["--runtime-template", "/private/runtime"])
        self.assertEqual(args.wall_timeout_seconds, 600)
        self.assertEqual(args.pcb_tool_call_limit, 80)
        self.assertEqual(len(RUNNER.CASES), 3)

    def test_worker_environment_isolates_app_and_runtime_and_drops_pythonpath(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {"PYTHONPATH": "/dirty/src", "PYTHONHOME": "/dirty/python"},
            clear=False,
        ):
            env = RUNNER._worker_environment(
                Path("/private/runtime"), Path("/private/app/config.json")
            )
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["PCBDRAFT_RUNTIME_HOME"], "/private/runtime")
        self.assertEqual(env["PCBDRAFT_CONFIG"], "/private/app/config.json")
        self.assertNotIn("PCBDRAFT_HOME", env)
        self.assertNotIn("PCBDRAFT_REPOSITORY_CONFIG", env)

    def test_inventory_reports_native_files_and_preview_without_claiming_pass(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            project = repository / "projects" / "demo"
            (project / "design").mkdir(parents=True)
            (project / "previews" / "one").mkdir(parents=True)
            (project / "project.json").write_text("{}", encoding="utf-8")
            (project / "design" / "demo.kicad_pcb").write_text(
                "(kicad_pcb)", encoding="utf-8"
            )
            (project / "previews" / "one" / "board.svg").write_text(
                "<svg/>", encoding="utf-8"
            )
            inventory = RUNNER._inventory(repository)
        self.assertEqual(inventory["project_count"], 1)
        self.assertEqual(len(inventory["native_artifacts"]), 1)
        self.assertEqual(len(inventory["board_svgs"]), 1)
        self.assertNotIn("passed", inventory)


class PublicExampleExporterTests(unittest.TestCase):
    _PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_root = self.root / "private-run"
        self.output = self.root / "public-bundle"
        self.run_root.mkdir()
        cases = []
        for case_id in EXPORTER.CASE_IDS:
            cases.append(self._make_case(case_id))
        (self.run_root / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": EXPORTER.RUN_SCHEMA,
                    "version": 1,
                    "started_at": "2026-09-09T00:00:00+00:00",
                    "completed_at": "2026-09-09T00:00:03+00:00",
                    "source": {"commit": "a" * 40, "dirty": False},
                    "environment": {
                        "os": "Linux-test",
                        "python": "3.13.0",
                        "kicad": "10.0.6",
                        "private_extra": "excluded",
                    },
                    "limits": {
                        "attempts_per_case": 1,
                        "pcb_tool_calls_per_case": 80,
                        "wall_timeout_seconds_per_case": 600,
                    },
                    "cases": cases,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _insert_png_chunk(raw: bytes, kind: bytes, payload: bytes) -> bytes:
        position = 8
        while raw[position + 4 : position + 8] != b"IDAT":
            length = int.from_bytes(raw[position : position + 4], "big")
            position += 12 + length
        chunk = (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + EXPORTER.zlib.crc32(kind + payload).to_bytes(4, "big")
        )
        return raw[:position] + chunk + raw[position:]

    def _make_case(self, case_id: str) -> dict[str, object]:
        project_id = f"{case_id}-abc12345"
        case_root = self.run_root / case_id
        project_root = case_root / "application" / "projects" / project_id
        design = project_root / "design"
        design.mkdir(parents=True)
        (case_root / "runtime").mkdir()
        (case_root / "runtime" / "auth.json").write_text(
            '{"access_token":"must-not-publish"}\n', encoding="utf-8"
        )
        (case_root / "agent-trace.jsonl").write_text(
            '{"endpoint":"https://secret.invalid/api?token=x"}\n',
            encoding="utf-8",
        )
        native = {
            "kicad_project": f"{project_id}.kicad_pro",
            "schematic": f"{project_id}.kicad_sch",
            "board": f"{project_id}.kicad_pcb",
        }
        aux = {
            "ir": "design.pcbir.json",
            "part_catalog": "parts.pcbdraft.json",
            "requirements": "requirements.pcbreq.json",
            "worker_receipt": f"{project_id}.worker-result.json",
        }
        contents = {
            native["kicad_project"]: '{"meta":{"filename":"demo.kicad_pro"}}\n',
            native["schematic"]: "(kicad_sch (version 20250114))\n",
            native["board"]: "(kicad_pcb (version 20250114))\n",
            aux["ir"]: '{"schema":"ir"}\n',
            aux["part_catalog"]: '{"schema":"parts"}\n',
            aux["requirements"]: '{"schema":"requirements"}\n',
            aux["worker_receipt"]: '{"schema":"worker"}\n',
        }
        for name, content in contents.items():
            (design / name).write_text(content, encoding="utf-8")
        design_hash = EXPORTER._sha256(design / aux["ir"])
        manifest_files = {
            **native,
            **aux,
            "manifest": "project.pcbdraft.json",
        }
        hashes = {
            key: EXPORTER._sha256(design / name)
            for key, name in manifest_files.items()
            if key != "manifest"
        }
        managed = {
            "schema": "pcbdraft-managed-project",
            "version": 1,
            "design": {
                "id": project_id,
                "name": case_id,
                "content_hash": design_hash,
                "revision": "1",
            },
            "files": manifest_files,
            "hashes": hashes,
            "sync": {"state": "synchronized"},
        }
        (design / "project.pcbdraft.json").write_text(
            json.dumps(managed), encoding="utf-8"
        )
        current_revision = 7
        validation = project_root / "validation"
        validation.mkdir()
        self._write_check(
            validation / "erc-1",
            "run_erc",
            design_hash,
            current_revision,
            native["schematic"],
            native["board"],
        )
        timed_out = case_id == "i2c-3v3-pullups"
        if timed_out:
            running = validation / "drc-running"
            running.mkdir()
            (running / "receipt.json").write_text(
                json.dumps(
                    {
                        "schema": "pcbdraft-individual-check-receipt",
                        "version": 1,
                        "check": "run_drc",
                        "status": "running",
                        "design_content_hash": design_hash,
                    }
                ),
                encoding="utf-8",
            )
        else:
            self._write_check(
                validation / "drc-1",
                "run_drc",
                design_hash,
                current_revision,
                native["schematic"],
                native["board"],
            )
        last_preview: dict[str, object] | None = None
        if not timed_out:
            preview_root = project_root / "previews" / "preview-1"
            preview_root.mkdir(parents=True)
            svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>\n'
            (preview_root / "board.svg").write_text(svg, encoding="utf-8")
            (preview_root / "board-top.png").write_bytes(self._PNG_1X1)
            preview_receipt = {
                "schema": "pcbdraft-preview-bundle",
                "version": 1,
                "design_content_hash": design_hash,
                "files": {
                    "board_svg": {
                        "path": "board.svg",
                        "bytes": (preview_root / "board.svg").stat().st_size,
                        "sha256": EXPORTER._sha256(preview_root / "board.svg"),
                    },
                    "board_render": {
                        "path": "board-top.png",
                        "bytes": (preview_root / "board-top.png").stat().st_size,
                        "sha256": EXPORTER._sha256(preview_root / "board-top.png"),
                    },
                },
                "renders": ["render_board"],
                "tool_runs": [
                    {
                        "name": "board_svg",
                        "exit_code": 0,
                        "timed_out": False,
                        "output_limited": False,
                        "failure": None,
                    },
                    {
                        "name": "board_render",
                        "exit_code": 0,
                        "timed_out": False,
                        "output_limited": False,
                        "failure": None,
                    },
                ],
            }
            (preview_root / "receipt.json").write_text(
                json.dumps(preview_receipt), encoding="utf-8"
            )
            last_preview = {
                "root": "previews/preview-1",
                "receipt": "previews/preview-1/receipt.json",
                "design_content_hash": design_hash,
                "source_design_revision": current_revision,
                "source_revision": 10,
                "files": {
                    "board_svg": "previews/preview-1/board.svg",
                    "board_render": "previews/preview-1/board-top.png",
                },
            }
        (project_root / "project.json").write_text(
            json.dumps(
                {
                    "schema": "pcbdraft-application-project",
                    "version": 1,
                    "id": project_id,
                    "name": case_id,
                    "design_revision": current_revision,
                    "revision": 11,
                    "status": "generated",
                    "last_preview": last_preview,
                }
            ),
            encoding="utf-8",
        )
        prompt = f"Build a small stock KiCad {case_id} board."
        entry = {
            "case_id": case_id,
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "started_at": "2026-09-09T00:00:00+00:00",
            "completed_at": "2026-09-09T00:00:01+00:00",
            "elapsed_seconds": 1.0,
            "returncode": -15 if timed_out else 0,
            "timed_out": timed_out,
            "provider": "example-provider",
            "model": "example-model",
            "package_version": "0.1.0",
            "usage": {"present": True, "total_tokens": 12, "cost_status": "unknown"},
            "artifacts": {
                "project_count": 0,
                "native_artifacts": [],
                "board_svgs": [],
                "receipts": [],
            },
        }
        (case_root / "result.json").write_text(json.dumps(entry), encoding="utf-8")
        return entry

    def _write_check(
        self,
        directory: Path,
        check: str,
        design_hash: str,
        revision: int,
        schematic_name: str,
        board_name: str,
    ) -> None:
        directory.mkdir(parents=True)
        report_name = "erc.json" if check == "run_erc" else "drc.json"
        raw_name = "erc.raw.json" if check == "run_erc" else "drc.raw.json"
        raw = {
            "$schema": f"https://schemas.kicad.org/{check[4:]}.v1.json",
            "included_severities": ["error", "warning"],
            "ignored_checks": [
                {"key": "simulation_model_issue", "description": "not published"}
            ],
            "source": schematic_name if check == "run_erc" else board_name,
        }
        if check == "run_erc":
            raw["sheets"] = [{"violations": []}]
        else:
            raw.update(
                {"violations": [], "unconnected_items": [], "schematic_parity": []}
            )
        (directory / raw_name).write_text(json.dumps(raw), encoding="utf-8")
        (directory / report_name).write_text(json.dumps(raw), encoding="utf-8")
        check_report = {
            "schema": "pcbdraft-individual-check",
            "version": 1,
            "check": check,
            "state": "completed",
            "outcome": "pass",
            "design_content_hash": design_hash,
            "details": {"failure": None, "violations": []},
            "tool_run": {
                "status": "completed",
                "failure": None,
                "raw_report": raw_name,
                "report": report_name,
            },
        }
        (directory / "check.json").write_text(
            json.dumps(check_report), encoding="utf-8"
        )
        receipt = {
            "schema": "pcbdraft-individual-check-receipt",
            "version": 1,
            "check": check,
            "status": "complete",
            "state": "completed",
            "outcome": "pass",
            "design_content_hash": design_hash,
            "source_design_revision": revision,
            "source_revision": 10,
            "report": "check.json",
            "report_sha256": EXPORTER._sha256(directory / "check.json"),
        }
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    def test_export_is_receipt_bound_and_keeps_evidence_boundary(self) -> None:
        result = EXPORTER.create_bundle(self.run_root, self.output)
        self.assertEqual(result, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["schema"], EXPORTER.BUNDLE_SCHEMA)
        self.assertEqual(len(manifest["cases"]), 3)
        self.assertEqual(
            manifest["cases"][0]["checks"]["erc"]["reported_item_count"], 0
        )
        erc_summary = manifest["cases"][0]["checks"]["erc"]
        erc_dir = (
            next(
                (self.run_root / "led-3v3-330r" / "application" / "projects").iterdir()
            )
            / "validation"
            / "erc-1"
        )
        self.assertEqual(
            erc_summary["normalized_report_sha256"],
            EXPORTER._sha256(erc_dir / "erc.json"),
        )
        self.assertEqual(
            erc_summary["raw_report_sha256"],
            EXPORTER._sha256(erc_dir / "erc.raw.json"),
        )
        self.assertEqual(
            erc_summary["check_report_sha256"],
            EXPORTER._sha256(erc_dir / "check.json"),
        )
        self.assertEqual(
            erc_summary["receipt_sha256"],
            EXPORTER._sha256(erc_dir / "receipt.json"),
        )
        self.assertEqual(erc_summary["ignored_check_keys"], ["simulation_model_issue"])
        self.assertEqual(
            manifest["cases"][0]["design_assessment"],
            "not_asserted_by_exporter",
        )
        self.assertFalse(manifest["evidence_boundary"]["raw_trace_published"])
        self.assertEqual(manifest["review"]["reviewer_kind"], "automated_assistant")
        self.assertFalse(manifest["review"]["human_engineering_review"])
        self.assertEqual(
            manifest["cases"][2]["worker"]["status"],
            "timed_out",
        )
        self.assertIsNone(manifest["cases"][2]["checks"]["drc"]["reported_item_count"])
        collection = json.loads((self.output / "collection.json").read_text())
        self.assertEqual(collection["cases"][0]["corrected_project_count"], 1)
        self.assertTrue(collection["cases"][0]["correction_applied"])
        self.assertTrue(collection["discrepancy"]["correction_applied"])
        self.assertFalse(
            (
                self.output / "cases" / "i2c-3v3-pullups" / "previews" / "board.svg"
            ).exists()
        )
        native = list((self.output / "cases" / "led-3v3-330r" / "native").iterdir())
        self.assertEqual(
            {path.suffix for path in native}, {".kicad_pro", ".kicad_sch", ".kicad_pcb"}
        )
        all_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.output.rglob("*")
            if path.is_file() and path.suffix != ".png"
        )
        self.assertNotIn("must-not-publish", all_text)
        self.assertNotIn("secret.invalid", all_text)
        self.assertFalse(
            any(path.name == "auth.json" for path in self.output.rglob("*"))
        )
        self.assertFalse(any("trace" in path.name for path in self.output.rglob("*")))

    def test_export_refuses_incomplete_run_and_changed_canonical_artifact(self) -> None:
        manifest_path = self.run_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["completed_at"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not complete"):
            EXPORTER.create_bundle(self.run_root, self.output)
        manifest["completed_at"] = "2026-09-09T00:00:03+00:00"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        board = next(
            (self.run_root / "led-3v3-330r" / "application" / "projects").glob(
                "*/design/*.kicad_pcb"
            )
        )
        board.write_text(board.read_text() + "tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed after collection"):
            EXPORTER.create_bundle(self.run_root, self.output)

    def test_stale_receipt_and_missing_fields_never_become_zero(self) -> None:
        project_root = next(
            (self.run_root / "i2c-3v3-pullups" / "application" / "projects").iterdir()
        )
        validation = project_root / "validation"
        broken = validation / "drc-current"
        broken.mkdir()
        design_hash = json.loads(
            (project_root / "design" / "project.pcbdraft.json").read_text()
        )["design"]["content_hash"]
        (broken / "receipt.json").write_text(
            json.dumps(
                {
                    "schema": "pcbdraft-individual-check-receipt",
                    "version": 1,
                    "check": "run_drc",
                    "status": "complete",
                    "state": "completed",
                    "outcome": "pass",
                    "design_content_hash": design_hash,
                    "source_design_revision": 7,
                    "source_revision": 20,
                    "report": "check.json",
                    "report_sha256": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        result = EXPORTER.create_bundle(self.run_root, self.output)
        drc = json.loads(
            (result / "cases" / "i2c-3v3-pullups" / "checks" / "drc.json").read_text()
        )
        self.assertEqual(drc["status"], "incomplete")
        self.assertIsNone(drc["reported_item_count"])
        self.assertNotEqual(drc["outcome"], "pass")

    def test_public_text_scan_rejects_secret_without_creating_output(self) -> None:
        manifest_path = self.run_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        prompt_value = "Build this board with " + "access_token=do-not-publish."
        manifest["cases"][0]["prompt"] = prompt_value
        manifest["cases"][0]["prompt_sha256"] = hashlib.sha256(
            prompt_value.encode()
        ).hexdigest()
        (self.run_root / "led-3v3-330r" / "result.json").write_text(
            json.dumps(manifest["cases"][0]), encoding="utf-8"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "credential material"):
            EXPORTER.create_bundle(self.run_root, self.output)
        self.assertFalse(self.output.exists())

    def test_pass_requires_explicit_failure_fields(self) -> None:
        project_root = next(
            (self.run_root / "led-3v3-330r" / "application" / "projects").iterdir()
        )
        check_dir = project_root / "validation" / "erc-1"
        check_path = check_dir / "check.json"
        check = json.loads(check_path.read_text())
        check["details"]["failure"] = "tool_failed"
        check_path.write_text(json.dumps(check), encoding="utf-8")
        receipt_path = check_dir / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["report_sha256"] = EXPORTER._sha256(check_path)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        result = EXPORTER.create_bundle(self.run_root, self.output)
        summary = json.loads(
            (result / "cases" / "led-3v3-330r" / "checks" / "erc.json").read_text()
        )
        self.assertEqual(summary["status"], "incomplete")
        self.assertIsNone(summary["reported_item_count"])
        self.assertNotEqual(summary["outcome"], "pass")

    def test_tool_run_without_explicit_failure_field_is_not_valid(self) -> None:
        valid = {
            "name": "board_svg",
            "exit_code": 0,
            "timed_out": False,
            "output_limited": False,
            "failure": None,
        }
        self.assertTrue(EXPORTER._valid_tool_run(valid, "board_svg"))
        del valid["failure"]
        self.assertFalse(EXPORTER._valid_tool_run(valid, "board_svg"))

    def test_png_allowlist_accepts_sbit_and_rejects_unknown_or_duplicate_chunks(
        self,
    ) -> None:
        png = self._PNG_1X1
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            path = Path(directory) / "candidate.png"
            path.write_bytes(self._insert_png_chunk(png, b"sBIT", b"\x08\x08"))
            self.assertEqual(EXPORTER._png_metadata(path)["width"], 1)

            path.write_bytes(self._insert_png_chunk(png, b"eXIf", b"private"))
            with self.assertRaisesRegex(RuntimeError, "unallowlisted"):
                EXPORTER._png_metadata(path)

            ihdr = png[16:29]
            path.write_bytes(self._insert_png_chunk(png, b"IHDR", ihdr))
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                EXPORTER._png_metadata(path)

    def test_svg_xml_and_private_path_filters_are_conservative(self) -> None:
        safe = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            path = Path(directory) / "board.svg"
            path.write_text(safe, encoding="utf-8")
            EXPORTER._scan_public_text(path)
            unsafe_svg = '<svg xmlns="http://www.w3.org/2000/svg" onClick="go()"/>'
            for index, value in enumerate(
                (
                    unsafe_svg,
                    '<svg xmlns="http://www.w3.org/2000/svg"><use href="https://example.invalid/x"/></svg>',
                    '<!DOCTYPE svg [<!ENTITY x "bad">]><svg>&x;</svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
                    "<svg>",
                )
            ):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(RuntimeError, msg=f"unsafe SVG {index}"):
                    EXPORTER._scan_public_text(path)

        for value in (
            "/Users/alice/private/board.kicad_pcb",
            r"D:\\work\\private\\board.kicad_pcb",
            "file:///tmp/private/board.kicad_pcb",
            "http://192.168.1.20/private",
            "http://[::1]/private",
        ):
            with self.assertRaisesRegex(
                RuntimeError, "private|file URL|local endpoint"
            ):
                EXPORTER._validate_safe_text(value, "test text")

    def test_repository_layout_and_correction_provenance_are_truthful(self) -> None:
        manifest_path = self.run_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for case in manifest["cases"]:
            case["artifacts"]["project_count"] = 1
            case["artifacts"]["native_artifacts"] = [{}, {}, {}]
            result_path = self.run_root / case["case_id"] / "result.json"
            result_path.write_text(json.dumps(case), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        case_root = self.run_root / "led-3v3-330r"
        application_project = next((case_root / "application" / "projects").iterdir())
        repository_projects = case_root / "repository" / "projects"
        repository_projects.mkdir(parents=True)
        application_project.rename(repository_projects / application_project.name)

        EXPORTER.create_bundle(self.run_root, self.output)
        collection = json.loads((self.output / "collection.json").read_text())
        led = next(
            item for item in collection["cases"] if item["case_id"] == "led-3v3-330r"
        )
        self.assertEqual(led["collection_root"], "repository/projects")
        self.assertFalse(led["correction_applied"])
        self.assertIsNone(led["correction_reason"])
        self.assertFalse(collection["discrepancy"]["correction_applied"])
        self.assertEqual(
            led["source_result_sha256"],
            EXPORTER._sha256(self.run_root / "led-3v3-330r" / "result.json"),
        )

    def test_both_known_project_layouts_are_rejected(self) -> None:
        case_root = self.run_root / "led-3v3-330r"
        repository_projects = case_root / "repository" / "projects" / "other"
        repository_projects.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "both known project layouts"):
            EXPORTER.create_bundle(self.run_root, self.output)

    def test_result_metadata_must_match_manifest_entry(self) -> None:
        manifest_path = self.run_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["cases"][0]["provider"] = "different-provider"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "case result metadata"):
            EXPORTER.create_bundle(self.run_root, self.output)

    def test_url_queries_are_rejected_even_for_public_display_links(self) -> None:
        for query in ("usp=sharing", "usp=private-value", ""):
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(RuntimeError, "URL query"),
            ):
                EXPORTER._validate_safe_text(
                    EXPORTER._STOCK_DESCRIPTION_URL + "?" + query, "test"
                )

    def test_native_description_sanitization_records_both_hashes(self) -> None:
        original = (
            '(kicad_pcb\n  (descr "Stock capacitor ('
            + EXPORTER._STOCK_DESCRIPTION_URL
            + '?usp=sharing)")\n)\n'
        ).encode()
        source = self.root / "source.kicad_pcb"
        source.write_bytes(original)
        source_hash = EXPORTER._sha256(source)
        output = self.root / "copied"
        record = EXPORTER._copy_file(
            EXPORTER._FileToCopy(
                source, "native/board.kicad_pcb", source_hash, len(original)
            ),
            output,
        )
        published = output / "native/board.kicad_pcb"
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(record["source_sha256"], source_hash)
        self.assertEqual(record["sha256"], EXPORTER._sha256(published))
        self.assertNotEqual(record["sha256"], source_hash)
        self.assertEqual(published.read_bytes(), original.replace(b"?usp=sharing", b""))
        EXPORTER._scan_public_text(published)
        self.assertIn("not_rechecked", record["check_evidence_applies_to"])
        arbitrary_field = original.replace(b"(descr", b"(property")
        self.assertEqual(
            EXPORTER._sanitize_native_description(arbitrary_field), arbitrary_field
        )
        private_query = original.replace(b"usp=sharing", b"usp=private-value")
        self.assertEqual(
            EXPORTER._sanitize_native_description(private_query), private_query
        )

    def test_stock_kicad_svg_declaration_is_parsed_without_resolving_dtd(self) -> None:
        svg = (
            '<?xml version="1.0" standalone="no"?>\n'
            '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"\n'
            '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:svg="http://www.w3.org/2000/svg" '
            'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">'
            '<path style="fill:#000000; stroke:none" d="M0 0"/></svg>'
        )
        EXPORTER._validate_svg_xml(svg)
        with self.assertRaisesRegex(RuntimeError, "unsafe XML"):
            EXPORTER._validate_svg_xml(svg.replace("svg11.dtd", "evil.dtd"))

    def test_svg_paint_server_cannot_reference_external_url(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "external reference"):
            EXPORTER._validate_svg_xml(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<path fill="url(https://example.invalid/pattern)"/></svg>'
            )

    def test_cli_requires_explicit_review_acknowledgement(self) -> None:
        code = EXPORTER.main(
            [
                "--run-root",
                str(self.run_root),
                "--output",
                str(self.output),
                "--reviewer-kind",
                "automated_assistant",
            ]
        )
        self.assertEqual(code, 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
