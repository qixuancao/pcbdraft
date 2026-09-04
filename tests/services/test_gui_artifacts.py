from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services.gui_artifacts import GUIArtifactService
from pcbdraft.verification.rule_evidence import capture_rule_evidence


class _Service:
    def __init__(self, root: Path, *, locks_root: Path | None = None) -> None:
        self.root = root
        if locks_root is not None:
            self.locks_root = locks_root

    def project_root(self, project_id: str) -> Path:
        if project_id != "demo-board":
            raise ValidationError("project id is invalid")
        return self.root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GUIArtifactServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "demo-board"
        self.root.mkdir()
        self.locks_root = Path(self.temporary.name) / "locks"
        self.locks_root.mkdir()
        self.release = self.root / "releases" / "release-1"
        self.release.mkdir(parents=True)
        self._write_release()
        self._write_project_state()
        self.artifacts = GUIArtifactService(
            _Service(self.root, locks_root=self.locks_root),
            cache_root=Path(self.temporary.name) / "cache",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_project_state(self, **overrides: Any) -> None:
        state: dict[str, Any] = {
            "design_revision": 4,
            "last_validation": {
                "report": "validation/run-1/validation.json",
                "report_sha256": "",
                "source_design_revision": 4,
                "candidate_ready": True,
            },
            "last_preview": {
                "root": "previews/preview-1",
                "receipt": "previews/preview-1/receipt.json",
                "design_content_hash": "a" * 64,
                "source_design_revision": 4,
                "files": {
                    "schematic_svg": "previews/preview-1/schematic.svg",
                    "schematic_pdf": "previews/preview-1/schematic.pdf",
                },
            },
            "last_release": {
                "id": "release-1",
                "root": "releases/release-1",
                "source_design_revision": 4,
                "design_content_hash": "a" * 64,
            },
        }
        state.update(overrides)
        validation = self.root / "validation" / "run-1"
        validation.mkdir(parents=True, exist_ok=True)
        report = validation / "validation.json"
        report.write_text(
            json.dumps(
                {
                    "schema": "pcbdraft-validation",
                    "version": 2,
                    "readiness": {"engineering_candidate": True},
                    "levels": [
                        {
                            "level": "L2",
                            "name": "KiCad",
                            "state": "completed",
                            "outcome": "pass",
                            "checks": [],
                        }
                    ],
                    "tool_runs": {
                        "erc": {"status": "completed", "violation_count": 0},
                        "drc": {"status": "completed", "violation_count": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        if isinstance(state.get("last_validation"), dict):
            state["last_validation"]["report_sha256"] = _sha256(report)
        preview = self.root / "previews" / "preview-1"
        preview.mkdir(parents=True, exist_ok=True)
        schematic_svg = preview / "schematic.svg"
        schematic_svg.write_text("<svg/>", encoding="utf-8")
        schematic_pdf = preview / "schematic.pdf"
        schematic_pdf.write_bytes(b"%PDF-1.4\npreview")
        preview_receipt = {
            "schema": "pcbdraft-preview-bundle",
            "version": 1,
            "design_content_hash": "a" * 64,
            "files": {
                "schematic_svg": {
                    "path": "schematic.svg",
                    "bytes": schematic_svg.stat().st_size,
                    "sha256": _sha256(schematic_svg),
                },
                "schematic_pdf": {
                    "path": "schematic.pdf",
                    "bytes": schematic_pdf.stat().st_size,
                    "sha256": _sha256(schematic_pdf),
                },
            },
        }
        (preview / "receipt.json").write_text(
            json.dumps(preview_receipt), encoding="utf-8"
        )
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")

    def _write_release(self) -> None:
        files = {
            "manufacturing/bom.csv": b"Reference,Value\nU1,AP2112\n",
            "manufacturing/gerber/demo-F_Cu.gtl": b"G04 gerber front*\n",
            "manufacturing/gerber/demo-B_Cu.gbl": b"G04 gerber back*\n",
            "manufacturing/drill/demo.drl": b"M48\n",
            "manufacturing/positions.csv": b"Ref,PosX\nU1,1\n",
            "manufacturing/board.step": b"ISO-10303-21;\n",
            "manufacturing/schematic.pdf": b"%PDF-1.4\nrelease",
        }
        entries = []
        for relative, content in files.items():
            path = self.release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            entries.append(
                {"path": relative, "size": len(content), "sha256": _sha256(path)}
            )
        manifest = {
            "schema": "pcbdraft-manufacturing-release",
            "version": 2,
            "design": {"revision": 4, "content_hash": "a" * 64},
            "artifacts": entries,
        }
        manifest_path = self.release / "release-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        archive = self.release / "release.zip"
        archive.write_bytes(b"archive")
        receipt = {
            "schema": "pcbdraft-release-receipt",
            "version": 1,
            "status": "complete",
            "completed_at": "2026-08-30T00:00:00Z",
            "manifest_sha256": _sha256(manifest_path),
            "archive_sha256": _sha256(archive),
        }
        (self.release / "receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )

    def _replace_validation_report(self, report: dict[str, Any]) -> None:
        path = self.root / "validation" / "run-1" / "validation.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        state = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        state["last_validation"]["report_sha256"] = _sha256(path)
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")

    def _write_individual_check(
        self, run_id: str, check: str, completed_at: str
    ) -> tuple[Path, dict[str, Any]]:
        run = self.root / "validation" / run_id
        run.mkdir(parents=True)
        details: dict[str, Any]
        tool_run: dict[str, Any] | None
        if check in {"run_erc", "run_drc"}:
            details = {"violations": []}
            tool_run = {"status": "completed", "violation_count": 0}
        else:
            details = {"issues": []}
            tool_run = None
        report = {
            "schema": "pcbdraft-individual-check",
            "version": 1,
            "check": check,
            "created_at": completed_at,
            "design_content_hash": "a" * 64,
            "state": "completed",
            "outcome": "pass",
            "details": details,
            "tool_run": tool_run,
        }
        report_path = run / "check.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        receipt = {
            "schema": "pcbdraft-individual-check-receipt",
            "version": 1,
            "status": "complete",
            "check": check,
            "completed_at": completed_at,
            "design_content_hash": "a" * 64,
            "source_design_revision": 4,
            "state": "completed",
            "outcome": "pass",
            "report": "check.json",
            "report_sha256": _sha256(report_path),
        }
        (run / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        return report_path, receipt

    def _write_individual_export(
        self,
        run_id: str,
        export: str,
        files: dict[str, bytes],
        completed_at: str,
    ) -> tuple[Path, list[dict[str, Any]]]:
        release = self.root / "releases" / run_id
        release.mkdir(parents=True)
        artifacts = []
        for relative, content in files.items():
            path = release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            artifacts.append(
                {"path": relative, "size": len(content), "sha256": _sha256(path)}
            )
        receipt = {
            "schema": "pcbdraft-individual-manufacturing-export",
            "version": 1,
            "status": "complete",
            "export": export,
            "completed_at": completed_at,
            "design_content_hash": "a" * 64,
            "artifacts": artifacts,
        }
        (release / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        return release, artifacts

    def _write_individual_release_state(
        self, run_id: str, artifacts: list[dict[str, Any]]
    ) -> None:
        state = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        state["last_release"] = {
            "id": run_id,
            "export": "export_step",
            "root": f"releases/{run_id}",
            "receipt": f"releases/{run_id}/receipt.json",
            "design_content_hash": "a" * 64,
            "source_design_revision": 4,
            "artifacts": artifacts,
        }
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")

    def _hold_project_lock(
        self,
    ) -> tuple[threading.Event, threading.Thread, list[BaseException]]:
        acquired = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def hold() -> None:
            lock: ResourceLock | None = None
            try:
                lock = ResourceLock(self.root, self.locks_root, timeout=0.0).acquire()
                acquired.set()
                if not release.wait(timeout=2.0):
                    raise RuntimeError("test lock holder was not released")
            except BaseException as exc:  # noqa: BLE001 - thread handoff
                errors.append(exc)
                acquired.set()
            finally:
                if lock is not None:
                    lock.release()

        holder = threading.Thread(target=hold, name="gui-artifact-lock-holder")
        holder.start()
        self.assertTrue(acquired.wait(timeout=1.0), "lock holder did not acquire")
        self.assertEqual(errors, [])
        return release, holder, errors

    def _release_project_lock(
        self,
        release: threading.Event,
        holder: threading.Thread,
        errors: list[BaseException],
    ) -> None:
        release.set()
        holder.join(timeout=1.0)
        self.assertFalse(holder.is_alive(), "lock holder did not stop")
        self.assertEqual(errors, [])

    def test_read_projections_wait_for_a_brief_external_project_lock(self) -> None:
        for projection, required_field in (
            ("manifest", "artifacts"),
            ("validation", "checks"),
        ):
            with self.subTest(projection=projection):
                release, holder, errors = self._hold_project_lock()
                release_timer = threading.Timer(0.1, release.set)
                release_timer.start()
                try:
                    started = time.monotonic()
                    value = getattr(self.artifacts, projection)("demo-board")
                    self.assertIn(required_field, value)
                    self.assertLess(time.monotonic() - started, 1.0)
                finally:
                    release_timer.cancel()
                    self._release_project_lock(release, holder, errors)

    def test_project_lock_timeout_is_generic_and_hides_the_project_path(self) -> None:
        release, holder, errors = self._hold_project_lock()
        try:
            for projection in ("manifest", "validation"):
                with self.subTest(projection=projection):
                    started = time.monotonic()
                    with self.assertRaises(ValidationError) as raised:
                        getattr(self.artifacts, projection)("demo-board")
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertEqual(
                        str(raised.exception), "artifact receipt is invalid"
                    )
                    self.assertNotIn(str(self.root), str(raised.exception))
        finally:
            self._release_project_lock(release, holder, errors)

    def test_concurrent_manifest_and_validation_reads_serialize(self) -> None:
        first_read_entered = threading.Event()
        allow_first_read = threading.Event()
        second_lock_attempted = threading.Event()
        state_calls = 0
        lock_attempts = 0
        counter_lock = threading.Lock()
        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, BaseException] = {}
        original_state = self.artifacts._state
        original_acquire = ResourceLock.acquire

        def delayed_state(root: Path) -> Any:
            nonlocal state_calls
            with counter_lock:
                state_calls += 1
                call_number = state_calls
            if call_number == 1:
                first_read_entered.set()
                if not allow_first_read.wait(timeout=1.0):
                    raise RuntimeError("first read was not released")
            return original_state(root)

        def tracked_acquire(lock: ResourceLock) -> ResourceLock:
            nonlocal lock_attempts
            with counter_lock:
                lock_attempts += 1
                if lock_attempts == 2:
                    second_lock_attempted.set()
            return original_acquire(lock)

        def read(name: str) -> None:
            try:
                results[name] = getattr(self.artifacts, name)("demo-board")
            except BaseException as exc:  # noqa: BLE001 - thread handoff
                errors[name] = exc

        with (
            mock.patch.object(self.artifacts, "_state", side_effect=delayed_state),
            mock.patch.object(ResourceLock, "acquire", new=tracked_acquire),
        ):
            manifest_thread = threading.Thread(target=read, args=("manifest",))
            manifest_thread.start()
            self.assertTrue(first_read_entered.wait(timeout=1.0))

            validation_thread = threading.Thread(target=read, args=("validation",))
            validation_thread.start()
            self.assertTrue(second_lock_attempted.wait(timeout=1.0))
            time.sleep(0.1)
            allow_first_read.set()
            manifest_thread.join(timeout=1.0)
            validation_thread.join(timeout=1.0)

        self.assertFalse(manifest_thread.is_alive())
        self.assertFalse(validation_thread.is_alive())
        self.assertEqual(errors, {})
        self.assertIn("artifacts", results["manifest"])
        self.assertIn("checks", results["validation"])

    def test_manifest_is_bounded_public_and_uses_only_fixed_artifact_keys(self) -> None:
        manifest = self.artifacts.manifest("demo-board")

        self.assertEqual(manifest["schema"], "pcbdraft-gui-artifacts")
        self.assertNotIn(str(self.root), json.dumps(manifest))
        by_key = {item["key"]: item for item in manifest["artifacts"]}
        self.assertEqual(by_key["bom.csv"]["state"], "ready")
        self.assertEqual(by_key["gerbers.zip"]["file_count"], 2)
        self.assertEqual(by_key["schematic.svg"]["state"], "ready")
        self.assertEqual(
            set(by_key["bom.csv"]),
            {"key", "label", "state", "file_count", "bytes", "created_at"},
        )

    def test_fixed_zip_downloads_are_deterministic_and_never_trust_client_paths(
        self,
    ) -> None:
        first = self.artifacts.download("demo-board", "gerbers.zip")
        second = self.artifacts.download("demo-board", "gerbers.zip")

        self.assertEqual(first.path, second.path)
        self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
        with zipfile.ZipFile(first.path) as archive:
            self.assertEqual(archive.namelist(), ["demo-B_Cu.gbl", "demo-F_Cu.gtl"])
            self.assertTrue(
                all(
                    item.date_time == (1980, 1, 1, 0, 0, 0)
                    for item in archive.infolist()
                )
            )
        with zipfile.ZipFile(first.path, "w") as archive:
            archive.writestr("demo-B_Cu.gbl", b"tampered")
        repaired = self.artifacts.download("demo-board", "gerbers.zip")
        with zipfile.ZipFile(repaired.path) as archive:
            self.assertEqual(archive.namelist(), ["demo-B_Cu.gbl", "demo-F_Cu.gtl"])
            self.assertEqual(archive.read("demo-B_Cu.gbl"), b"G04 gerber back*\n")
        with self.assertRaisesRegex(ValidationError, "unsupported GUI artifact"):
            self.artifacts.download("demo-board", "../../project.json")

    def test_validation_projects_pass_not_run_and_stale_without_paths(self) -> None:
        current = self.artifacts.validation("demo-board")
        self.assertEqual(current["state"], "pass")
        self.assertNotIn(str(self.root), json.dumps(current))
        self.assertEqual(
            [item["id"] for item in current["checks"][:3]],
            ["erc", "drc", "unrouted"],
        )

        self._write_project_state(last_validation=None)
        self.assertEqual(self.artifacts.validation("demo-board")["state"], "not_run")

        self._write_project_state(
            last_validation={
                "report": "validation/run-1/validation.json",
                "report_sha256": "placeholder",
                "source_design_revision": 3,
            }
        )
        report = self.root / "validation" / "run-1" / "validation.json"
        state = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        state["last_validation"]["report_sha256"] = _sha256(report)
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.artifacts.validation("demo-board")["state"], "stale")

    def test_validation_never_projects_missing_or_failed_tools_as_pass(self) -> None:
        path = self.root / "validation" / "run-1" / "validation.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["tool_runs"]["drc"]["violation_count"] = 1
        self._replace_validation_report(report)
        self.assertEqual(self.artifacts.validation("demo-board")["state"], "failed")

        report["tool_runs"] = {}
        self._replace_validation_report(report)
        self.assertEqual(self.artifacts.validation("demo-board")["state"], "warning")

    def test_validation_reverifies_complete_evidence_and_bounds_ui_findings(
        self,
    ) -> None:
        validation_root = self.root / "validation" / "run-1"
        source = validation_root / "design.kicad_pcb"
        source.write_text("(kicad_pcb)", encoding="utf-8")
        for kind in ("erc", "drc"):
            raw = validation_root / f"{kind}.json"
            raw.write_text(
                json.dumps(
                    {
                        "$schema": f"https://example.test/{kind}.v1.json",
                        "kicad_version": "10.0.5",
                        "violations": [
                            {
                                "severity": "warning",
                                "type": f"{kind}_warning",
                                "description": "bounded location",
                                "items": [
                                    {
                                        "uuid": "11111111-1111-1111-1111-111111111111",
                                        "pos": {"x": 9.0, "y": 8.0},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            capture_rule_evidence(
                kind=kind,
                raw_report=raw,
                output=validation_root / f"{kind}.evidence.json",
                source_file=source,
                canonical_revision=7,
                design_revision=4,
                design_content_hash="a" * 64,
            )
        state = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        state["last_validation"].update(
            {
                "design_content_hash": "a" * 64,
                "erc_evidence": "validation/run-1/erc.evidence.json",
                "drc_evidence": "validation/run-1/drc.evidence.json",
            }
        )
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")

        current = self.artifacts.validation("demo-board")
        self.assertTrue(current["machine_evidence_complete"])
        self.assertEqual(len(current["findings"]), 2)
        self.assertEqual(
            current["findings"][0]["items"][0]["uuid"],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertNotIn("identity", json.dumps(current))

        (validation_root / "drc.json").write_text("{}", encoding="utf-8")
        damaged = self.artifacts.validation("demo-board")
        self.assertFalse(damaged["machine_evidence_complete"])
        self.assertEqual(damaged["state"], "failed")

    def test_retained_individual_checks_project_the_real_complete_shape(self) -> None:
        self._write_project_state(last_validation=None)
        latest_report: Path | None = None
        latest_receipt: dict[str, Any] | None = None
        for ordinal, check in enumerate(
            ("check_semantics", "check_connectivity", "run_erc", "run_drc"),
            start=1,
        ):
            latest_report, latest_receipt = self._write_individual_check(
                f"20260830T00000{ordinal}Z-{ordinal:08d}",
                check,
                f"2026-08-30T00:00:0{ordinal}Z",
            )
        assert latest_report is not None
        assert latest_receipt is not None
        state = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        state["last_validation"] = {
            "run_id": "20260830T000004Z-00000004",
            "check": "run_drc",
            "report": "validation/20260830T000004Z-00000004/check.json",
            "report_sha256": _sha256(latest_report),
            "state": latest_receipt["state"],
            "outcome": latest_receipt["outcome"],
            "design_content_hash": "a" * 64,
            "source_design_revision": 4,
        }
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")

        validation = self.artifacts.validation("demo-board")

        self.assertEqual(validation["state"], "pass")
        self.assertEqual(validation["checked_at"], "2026-08-30T00:00:04Z")
        self.assertEqual(
            validation["counts"], {"error": 0, "warning": 0, "unconnected": 0}
        )
        self.assertEqual(
            [(item["id"], item["outcome"]) for item in validation["checks"]],
            [
                ("semantics", "pass"),
                ("connectivity", "pass"),
                ("erc", "pass"),
                ("drc", "pass"),
            ],
        )
        self.assertNotIn(str(self.root), json.dumps(validation))

    def test_retained_individual_exports_aggregate_all_fixed_downloads(self) -> None:
        self._write_project_state()
        self._write_individual_export(
            "20260830T000001Z-00000001",
            "export_bom",
            {"bom.csv": b"Reference,Value\\nU1,AP2112\\n"},
            "2026-08-30T00:00:01Z",
        )
        self._write_individual_export(
            "20260830T000002Z-00000002",
            "export_gerbers",
            {
                f"gerber/layer-{number}.gbr": f"G04 {number}*\\n".encode()
                for number in range(8)
            },
            "2026-08-30T00:00:02Z",
        )
        self._write_individual_export(
            "20260830T000003Z-00000003",
            "export_drill",
            {"drill/a.drl": b"M48\\n", "drill/b.drl": b"M48\\nT2\\n"},
            "2026-08-30T00:00:03Z",
        )
        self._write_individual_export(
            "20260830T000004Z-00000004",
            "export_pick_place",
            {"positions.csv": b"Ref,PosX\\nU1,1\\n"},
            "2026-08-30T00:00:04Z",
        )
        step_root, step_artifacts = self._write_individual_export(
            "20260830T000005Z-00000005",
            "export_step",
            {"board.step": b"ISO-10303-21;\\n"},
            "2026-08-30T00:00:05Z",
        )
        self._write_individual_release_state(step_root.name, step_artifacts)

        manifest = self.artifacts.manifest("demo-board")

        by_key = {item["key"]: item for item in manifest["artifacts"]}
        for key in (
            "bom.csv",
            "gerbers.zip",
            "drill.zip",
            "positions.csv",
            "board.step",
        ):
            self.assertEqual(by_key[key]["state"], "ready")
        self.assertEqual(by_key["gerbers.zip"]["file_count"], 8)
        self.assertEqual(by_key["drill.zip"]["file_count"], 2)
        self.assertEqual(by_key["schematic.svg"]["state"], "ready")
        self.assertEqual(
            self.artifacts.download("demo-board", "bom.csv").path.read_text(
                encoding="utf-8"
            ),
            "Reference,Value\\nU1,AP2112\\n",
        )
        with zipfile.ZipFile(
            self.artifacts.download("demo-board", "gerbers.zip").path
        ) as archive:
            self.assertEqual(len(archive.namelist()), 8)
        with zipfile.ZipFile(
            self.artifacts.download("demo-board", "drill.zip").path
        ) as archive:
            self.assertEqual(len(archive.namelist()), 2)
        self.assertEqual(
            self.artifacts.download("demo-board", "positions.csv").path.read_text(
                encoding="utf-8"
            ),
            "Ref,PosX\\nU1,1\\n",
        )
        self.assertEqual(
            self.artifacts.download("demo-board", "board.step").path.read_bytes(),
            b"ISO-10303-21;\\n",
        )
        self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_individual_export_without_revision_requires_a_trusted_current_hash(
        self,
    ) -> None:
        self._write_project_state(last_validation=None, last_preview=None)
        release, artifacts = self._write_individual_export(
            "a-step",
            "export_step",
            {"board.step": b"ISO-10303-21;\\n"},
            "2026-08-30T00:00:05Z",
        )
        state = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        state["last_release"] = {
            "id": release.name,
            "root": f"releases/{release.name}",
            "source_design_revision": 4,
            "artifacts": artifacts,
        }
        (self.root / "project.json").write_text(json.dumps(state), encoding="utf-8")
        for ordinal in range(100):
            (self.root / "releases" / f"z-{ordinal:03d}").mkdir()

        manifest = self.artifacts.manifest("demo-board")

        by_key = {item["key"]: item for item in manifest["artifacts"]}
        self.assertEqual(by_key["board.step"]["state"], "stale")

    def test_newest_recognized_individual_export_fails_closed_when_tampered(
        self,
    ) -> None:
        self._write_project_state()
        self._write_individual_export(
            "20260830T000001Z-00000001",
            "export_bom",
            {"bom.csv": b"Reference,Value\\nU1,old\\n"},
            "2026-08-30T00:00:01Z",
        )
        newest, _ = self._write_individual_export(
            "20260830T000002Z-00000002",
            "export_bom",
            {"bom.csv": b"Reference,Value\\nU1,new\\n"},
            "2026-08-30T00:00:02Z",
        )
        (newest / "bom.csv").write_bytes(b"tampered")

        with self.assertRaisesRegex(ValidationError, "artifact receipt is invalid"):
            self.artifacts.manifest("demo-board")

    def test_hash_size_symlink_and_path_traversal_fail_closed(self) -> None:
        bom = self.release / "manufacturing" / "bom.csv"
        bom.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "artifact receipt is invalid"):
            self.artifacts.manifest("demo-board")

        self._write_release()
        self._write_project_state(
            last_release={"root": "../outside", "source_design_revision": 4}
        )
        with self.assertRaisesRegex(ValidationError, "artifact receipt is invalid"):
            self.artifacts.manifest("demo-board")

        self._write_project_state()
        target = self.release / "manufacturing" / "bom.csv"
        link = self.release / "manufacturing" / "positions.csv"
        link.unlink()
        link.symlink_to(target)
        with self.assertRaisesRegex(ValidationError, "artifact receipt is invalid"):
            self.artifacts.manifest("demo-board")


if __name__ == "__main__":
    unittest.main()
