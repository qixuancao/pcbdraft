"""Validated manufacturing-candidate export and evidence bundling."""

from __future__ import annotations

import csv
import json
import math
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PcbAgentError, ValidationError
from .io import atomic_write_json, make_directory
from .locking import ResourceLock
from .managed import ManagedProject, open_managed_project
from .parts import PartGraph
from .process import redact_argv, run_command
from .project import sha256_file
from .runs import utc_timestamp
from .validation import validate_managed_project

MAX_EXPORT_OUTPUT = 4 * 1024 * 1024
MAX_RELEASE_FILES = 256
MAX_RELEASE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ManufacturingRelease:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    archive_path: Path
    archive_sha256: str
    candidate_ready: bool
    production_ready: bool


def build_manufacturing_release(
    project_value: ManagedProject | str | Path,
    output: str | Path,
    *,
    graph: PartGraph | None = None,
    timeout: float = 180.0,
) -> ManufacturingRelease:
    """Generate real KiCad manufacturing outputs after all candidate gates pass."""
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ValidationError("release timeout must be in (0, 3600] seconds")
    project = (
        project_value
        if isinstance(project_value, ManagedProject)
        else open_managed_project(project_value)
    )
    project.assert_synchronized()
    resolved_graph = graph or PartGraph.bundled()
    root = _new_release_root(output)
    receipt_path = root / "receipt.json"
    receipt: dict[str, Any] = {
        "schema": "pcb-agent-release-receipt",
        "version": 1,
        "status": "running",
        "created_at": utc_timestamp(),
        "project": str(project.root),
        "design_content_hash": project.design.content_hash(),
    }
    atomic_write_json(receipt_path, receipt)
    deadline = time.monotonic() + timeout
    try:
        with ResourceLock(
            project.root,
            project.root.parent / ".pcb-agent-locks",
            timeout=min(10.0, timeout),
        ):
            project = open_managed_project(project.root)
            project.assert_synchronized()
            source_hashes = _source_hashes(project)
            validation = validate_managed_project(
                project,
                output=root / "validation",
                graph=resolved_graph,
                timeout=max(1.0, deadline - time.monotonic()),
                _already_locked=True,
            )
            if not validation.candidate_ready:
                raise ValidationError(
                    "manufacturing export requires all engineering-candidate gates to pass"
                )
            manufacturing = root / "manufacturing"
            source = root / "source"
            gerber = manufacturing / "gerber"
            drill = manufacturing / "drill"
            svg = manufacturing / "svg"
            for directory in (manufacturing, source, gerber, drill, svg):
                make_directory(directory)
            tool_runs = _run_exports(project, manufacturing, deadline)
            _copy_sources(project, source)
            bom_contract = _verify_bom(project, resolved_graph, manufacturing)
            position_contract = _verify_positions(
                project, resolved_graph, manufacturing
            )
            fabrication_contract = _verify_fabrication_files(manufacturing)
            if _source_hashes(project) != source_hashes:
                raise ValidationError(
                    "managed project changed during manufacturing export"
                )

            artifacts = _inventory(
                root,
                exclude={"receipt.json", "release-manifest.json", "release.zip"},
            )
            manifest = {
                "schema": "pcb-agent-manufacturing-release",
                "version": 1,
                "created_at": utc_timestamp(),
                "classification": "engineering_candidate",
                "readiness": {
                    "engineering_candidate": True,
                    "production": validation.production_ready,
                    "production_claimed": False,
                    "human_engineering_review": "external_gate",
                    "physical_build_and_test": "external_gate",
                },
                "design": {
                    "id": project.design.design_id,
                    "revision": project.design.revision,
                    "content_hash": project.design.content_hash(),
                },
                "validation": {
                    "report": validation.report_path.relative_to(root).as_posix(),
                    "report_sha256": validation.report_sha256,
                },
                "contracts": {
                    "bom": bom_contract,
                    "positions": position_contract,
                    "fabrication": fabrication_contract,
                },
                "tool_runs": tool_runs,
                "artifacts": artifacts,
                "limitations": [
                    "This bundle is not a human engineering sign-off.",
                    "Live distributor stock and price were not verified.",
                    "No fabricated-board, bring-up, environmental, EMC, or measured L7 evidence is included.",
                ],
            }
            manifest_path = root / "release-manifest.json"
            atomic_write_json(manifest_path, manifest, mode=0o644)
            manifest_hash = sha256_file(manifest_path, max_bytes=16 * 1024 * 1024)
            archive_path = root / "release.zip"
            _write_archive(root, archive_path)
            archive_hash = sha256_file(archive_path, max_bytes=MAX_RELEASE_BYTES)
            receipt.update(
                {
                    "status": "complete",
                    "completed_at": utc_timestamp(),
                    "candidate_ready": True,
                    "production_ready": validation.production_ready,
                    "manifest_sha256": manifest_hash,
                    "archive_sha256": archive_hash,
                }
            )
            atomic_write_json(receipt_path, receipt)
            return ManufacturingRelease(
                root,
                manifest_path,
                manifest_hash,
                archive_path,
                archive_hash,
                True,
                validation.production_ready,
            )
    except BaseException as exc:
        receipt["status"] = "failed"
        receipt["completed_at"] = utc_timestamp()
        receipt["failure"] = str(exc)[:2048]
        atomic_write_json(receipt_path, receipt)
        raise


def _new_release_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if raw.name in {"", ".", ".."} or raw.is_symlink():
        raise ValidationError("release output path is unsafe")
    raw.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = raw.resolve(strict=False)
    if root.exists() or root.is_symlink():
        raise ValidationError("release output already exists")
    make_directory(root)
    return root


def _run_exports(
    project: ManagedProject, output: Path, deadline: float
) -> list[dict[str, Any]]:
    executable = shutil.which("kicad-cli")
    if executable is None:
        raise PcbAgentError("required executable not found: kicad-cli")
    board = str(project.board_path)
    schematic = str(project.schematic_path)
    commands = [
        (
            "gerbers",
            [
                executable,
                "pcb",
                "export",
                "gerbers",
                "-o",
                str(output / "gerber"),
                "-l",
                "F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,B.SilkS,Edge.Cuts",
                board,
            ],
        ),
        (
            "drill",
            [
                executable,
                "pcb",
                "export",
                "drill",
                "-o",
                str(output / "drill"),
                "--generate-report",
                "--report-path",
                str(output / "drill" / "drill_report.rpt"),
                board,
            ],
        ),
        (
            "positions",
            [
                executable,
                "pcb",
                "export",
                "pos",
                "-o",
                str(output / "positions.csv"),
                "--format",
                "csv",
                "--units",
                "mm",
                "--side",
                "both",
                "--exclude-dnp",
                board,
            ],
        ),
        (
            "connectivity",
            [
                executable,
                "pcb",
                "export",
                "ipcd356",
                "-o",
                str(output / "connectivity.d356"),
                board,
            ],
        ),
        (
            "board_stats",
            [
                executable,
                "pcb",
                "export",
                "stats",
                "-o",
                str(output / "board_stats.json"),
                "--format",
                "json",
                "--units",
                "mm",
                board,
            ],
        ),
        (
            "bom",
            [
                executable,
                "sch",
                "export",
                "bom",
                "-o",
                str(output / "bom.csv"),
                "--fields",
                "Reference,Value,Footprint,Manufacturer,MPN,Part_ID,Lifecycle,Trust,QUANTITY",
                "--labels",
                "Reference,Value,Footprint,Manufacturer,MPN,Part_ID,Lifecycle,Trust,Quantity",
                schematic,
            ],
        ),
        (
            "schematic_pdf",
            [
                executable,
                "sch",
                "export",
                "pdf",
                "-o",
                str(output / "schematic.pdf"),
                schematic,
            ],
        ),
        (
            "board_svg",
            [
                executable,
                "pcb",
                "export",
                "svg",
                "-o",
                str(output / "svg"),
                "-l",
                "F.Cu,F.Mask,F.SilkS,Edge.Cuts",
                "--mode-multi",
                "--fit-page-to-board",
                board,
            ],
        ),
        (
            "board_render",
            [
                executable,
                "pcb",
                "render",
                "-o",
                str(output / "board-top.png"),
                "--width",
                "1200",
                "--height",
                "800",
                "--side",
                "top",
                "--quality",
                "basic",
                board,
            ],
        ),
        (
            "step",
            [
                executable,
                "pcb",
                "export",
                "step",
                "--force",
                "-o",
                str(output / "board.step"),
                board,
            ],
        ),
    ]
    results = []
    redactions = {str(project.root): "<PROJECT>", str(output.parent): "<RELEASE>"}
    for name, argv in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PcbAgentError(f"manufacturing export deadline expired before {name}")
        result = run_command(
            argv,
            cwd=project.root,
            timeout=remaining,
            max_output_bytes=MAX_EXPORT_OUTPUT,
        )
        if result.timed_out:
            raise PcbAgentError(f"manufacturing export timed out: {name}")
        if result.output_limited:
            raise PcbAgentError(f"manufacturing export output limit exceeded: {name}")
        if result.returncode != 0:
            raise PcbAgentError(
                f"manufacturing export failed ({name}, exit {result.returncode})"
            )
        results.append(
            {
                "name": name,
                "status": "completed",
                "exit_code": result.returncode,
                "duration_seconds": result.duration_seconds,
                "argv": redact_argv(argv, redactions),
            }
        )
    return results


def _copy_sources(project: ManagedProject, output: Path) -> None:
    for name, relative in sorted(project.manifest["files"].items()):
        if name == "manifest":
            source = project.manifest_path
        else:
            source = project.root / relative
        target = output / source.name
        shutil.copyfile(source, target)
        target.chmod(0o644)


def _verify_bom(
    project: ManagedProject, graph: PartGraph, output: Path
) -> dict[str, Any]:
    path = output / "bom.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    expected = {
        component.reference: component
        for component in project.design.components
        if graph.get(component.part_id).bom
    }
    actual_refs = [row.get("Reference", "") for row in rows]
    if len(actual_refs) != len(set(actual_refs)) or set(actual_refs) != set(expected):
        raise ValidationError(
            "KiCad BOM references do not match semantic BOM membership"
        )
    mismatches = []
    for row in rows:
        component = expected[row["Reference"]]
        part = graph.get(component.part_id)
        expected_fields = {
            "Value": component.value,
            "Footprint": part.footprint or "",
            "Manufacturer": part.manufacturer,
            "MPN": part.mpn,
            "Part_ID": part.id,
            "Lifecycle": str(part.lifecycle.get("status", "unknown")),
            "Trust": part.trust,
            "Quantity": "1",
        }
        for field, value in expected_fields.items():
            if row.get(field) != value:
                mismatches.append(f"{component.reference}.{field}")
    if mismatches:
        raise ValidationError(
            "KiCad BOM differs from trusted part contracts: "
            + ", ".join(sorted(mismatches))
        )
    return {
        "state": "completed",
        "outcome": "pass",
        "line_count": len(rows),
        "references": sorted(expected),
        "live_stock_checked": False,
    }


def _verify_positions(
    project: ManagedProject, graph: PartGraph, output: Path
) -> dict[str, Any]:
    with (output / "positions.csv").open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    expected = {
        component.reference
        for component in project.design.components
        if graph.get(component.part_id).footprint is not None
        and not component.attributes.get("exclude_from_board", False)
    }
    actual = {row.get("Ref", "") for row in rows}
    if len(rows) != len(actual) or actual != expected:
        raise ValidationError("position file references do not match board components")
    return {
        "state": "completed",
        "outcome": "pass",
        "count": len(rows),
        "units": "mm",
        "sides": sorted({row.get("Side", "") for row in rows}),
    }


def _verify_fabrication_files(output: Path) -> dict[str, Any]:
    gerbers = list((output / "gerber").glob("*"))
    suffixes = {path.suffix.lower() for path in gerbers if path.is_file()}
    expected = {".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gm1", ".gbrjob"}
    missing = sorted(expected - suffixes)
    drill = list((output / "drill").glob("*.drl"))
    required = [
        output / "connectivity.d356",
        output / "board_stats.json",
        output / "board.step",
        output / "board-top.png",
        output / "schematic.pdf",
        *drill,
    ]
    if (
        missing
        or len(drill) != 1
        or any(
            path.is_symlink() or not path.is_file() or path.stat().st_size == 0
            for path in required
        )
    ):
        raise ValidationError(
            "manufacturing output set is incomplete"
            + (f"; missing Gerber types: {', '.join(missing)}" if missing else "")
        )
    stats = json.loads((output / "board_stats.json").read_text(encoding="utf-8"))
    if (
        not isinstance(stats, dict)
        or stats.get("board", {}).get("has_outline") is not True
    ):
        raise ValidationError("board statistics do not confirm a closed outline")
    return {
        "state": "completed",
        "outcome": "pass",
        "gerber_file_count": len(gerbers),
        "drill_file_count": len(drill),
        "closed_outline": True,
        "step_model": True,
        "render": True,
    }


def _source_hashes(project: ManagedProject) -> dict[str, str]:
    return {
        name: sha256_file(project.root / relative, max_bytes=128 * 1024 * 1024)
        for name, relative in project.manifest["files"].items()
        if name != "manifest"
    } | {"manifest": sha256_file(project.manifest_path, max_bytes=16 * 1024 * 1024)}


def _inventory(root: Path, *, exclude: set[str]) -> list[dict[str, Any]]:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in exclude
    )
    if len(files) > MAX_RELEASE_FILES:
        raise ValidationError("release exceeds the artifact count limit")
    total = sum(path.stat().st_size for path in files)
    if total > MAX_RELEASE_BYTES:
        raise ValidationError("release exceeds the artifact byte limit")
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path, max_bytes=128 * 1024 * 1024),
        }
        for path in files
    ]


def _write_archive(root: Path, output: Path) -> None:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path != output and path.name != "receipt.json"
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    output.chmod(0o644)
