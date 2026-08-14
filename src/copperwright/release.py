"""Validated manufacturing-candidate export and evidence bundling."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CopperWrightError, ValidationError
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
    make_directory,
    portable_record_path,
    read_bytes_limited,
)
from .locking import ResourceLock
from .managed import ManagedProject, open_managed_project
from .parts import PartGraph
from .process import redact_argv, run_command
from .project import canonical_project, sha256_file, validate_agent_tree
from .runs import utc_timestamp
from .validation import validate_managed_project

MAX_EXPORT_OUTPUT = 4 * 1024 * 1024
MAX_RELEASE_FILES = 256
MAX_RELEASE_BYTES = 512 * 1024 * 1024
REPRODUCIBLE_TIMESTAMP = "1980-01-01T00:00:00"
AUDIT_ARTIFACT_PATHS = frozenset(
    {
        "validation/receipt.json",
        "validation/erc.raw.json",
        "validation/drc.raw.json",
    }
)
RELEASE_CONTROL_PATHS = frozenset(
    {"receipt.json", "release-manifest.json", "release.zip"}
)


@dataclass(frozen=True)
class ManufacturingRelease:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    archive_path: Path
    archive_sha256: str
    candidate_ready: bool
    production_ready: bool


@dataclass(frozen=True)
class ReleaseVerification:
    root: Path
    manifest_sha256: str
    archive_sha256: str
    artifact_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "manifest_sha256": self.manifest_sha256,
            "archive_sha256": self.archive_sha256,
            "artifact_count": self.artifact_count,
            "verified": True,
        }


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
    resolved_graph = graph or project.graph
    root = _new_release_root(output)
    receipt_path = root / "receipt.json"
    receipt: dict[str, Any] = {
        "schema": "copperwright-release-receipt",
        "version": 1,
        "status": "running",
        "created_at": utc_timestamp(),
        "project": portable_record_path(project.root),
        "design_content_hash": project.design.content_hash(),
    }
    atomic_write_json(receipt_path, receipt)
    deadline = time.monotonic() + timeout
    try:
        with ResourceLock(
            project.root,
            project.root.parent / ".copperwright-locks",
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
            normalization = _normalize_export_metadata(manufacturing)
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

            audit_artifacts = _inventory_exact(root, AUDIT_ARTIFACT_PATHS)
            artifacts = _inventory(
                root,
                exclude={"receipt.json", "release-manifest.json", "release.zip"},
            )
            manifest = {
                "schema": "copperwright-manufacturing-release",
                "version": 1,
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
                "tool_runs": [
                    {
                        "name": run["name"],
                        "status": run["status"],
                        "exit_code": run["exit_code"],
                        "argv": run["argv"],
                    }
                    for run in tool_runs
                ],
                "artifacts": artifacts,
                "reproducibility": {
                    "content_archive": "byte_reproducible_for_identical_managed_input_and_tool_version",
                    "normalized_fields": "creation-time metadata and SVG trailing whitespace",
                    "normalized_timestamp": REPRODUCIBLE_TIMESTAMP + "Z",
                    "excluded_from_content_archive": [
                        "receipt.json",
                        "validation/receipt.json",
                        "validation/erc.raw.json",
                        "validation/drc.raw.json",
                    ],
                    "execution_audit_location": "receipt.json and validation/receipt.json",
                },
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
                    "tool_runs": tool_runs,
                    "normalization": normalization,
                    "audit_artifacts": audit_artifacts,
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


def verify_manufacturing_release(value: str | Path) -> ReleaseVerification:
    """Verify a release directory and its deterministic archive without trust in names."""
    root = canonical_project(value)
    validate_agent_tree(root)
    manifest_path = _single_link_file(root / "release-manifest.json")
    receipt_path = _single_link_file(root / "receipt.json")
    archive_path = _single_link_file(root / "release.zip")
    manifest = load_json_limited(manifest_path, 16 * 1024 * 1024)
    receipt = load_json_limited(receipt_path, 4 * 1024 * 1024)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "copperwright-manufacturing-release"
        or manifest.get("version") != 1
        or not isinstance(manifest.get("artifacts"), list)
    ):
        raise ValidationError("release manifest is malformed")
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "copperwright-release-receipt"
        or receipt.get("version") != 1
        or receipt.get("status") != "complete"
    ):
        raise ValidationError("release receipt is malformed or incomplete")
    artifacts = manifest["artifacts"]
    if len(artifacts) > MAX_RELEASE_FILES:
        raise ValidationError("release manifest exceeds the artifact count limit")
    expected: dict[str, dict[str, Any]] = {}
    total = 0
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValidationError("release artifact entry is malformed")
        relative = entry["path"]
        if not _safe_release_relative(relative) or relative in expected:
            raise ValidationError("release artifact path is unsafe or duplicated")
        size = entry["size"]
        digest = entry["sha256"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > 128 * 1024 * 1024
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValidationError("release artifact size/hash is invalid")
        path = _single_link_file(root / relative)
        if (
            path.stat().st_size != size
            or sha256_file(path, max_bytes=128 * 1024 * 1024) != digest
        ):
            raise ValidationError(f"release artifact hash mismatch: {relative}")
        total += size
        expected[relative] = entry
    if total > MAX_RELEASE_BYTES:
        raise ValidationError("release manifest exceeds the total byte limit")

    actual_nonvolatile = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix() not in RELEASE_CONTROL_PATHS
        and path.relative_to(root).as_posix() not in AUDIT_ARTIFACT_PATHS
    }
    if actual_nonvolatile != set(expected):
        raise ValidationError("release directory inventory differs from its manifest")
    manifest_hash = sha256_file(manifest_path, max_bytes=16 * 1024 * 1024)
    archive_hash = sha256_file(archive_path, max_bytes=MAX_RELEASE_BYTES)
    if receipt.get("manifest_sha256") != manifest_hash:
        raise ValidationError("release manifest hash differs from its receipt")
    if receipt.get("archive_sha256") != archive_hash:
        raise ValidationError("release archive hash differs from its receipt")
    audit = _verify_audit_artifacts(root, receipt.get("audit_artifacts"))
    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    if actual_files != set(expected) | set(audit) | set(RELEASE_CONTROL_PATHS):
        raise ValidationError("release directory contains untracked files")
    _verify_archive(archive_path, manifest_path, expected)
    return ReleaseVerification(root, manifest_hash, archive_hash, len(expected))


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
        raise CopperWrightError("required executable not found: kicad-cli")
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
                "--board-only",
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
            raise CopperWrightError(
                f"manufacturing export deadline expired before {name}"
            )
        result = run_command(
            argv,
            cwd=project.root,
            timeout=remaining,
            max_output_bytes=MAX_EXPORT_OUTPUT,
        )
        if result.timed_out:
            raise CopperWrightError(f"manufacturing export timed out: {name}")
        if result.output_limited:
            raise CopperWrightError(
                f"manufacturing export output limit exceeded: {name}"
            )
        if result.returncode != 0:
            raise CopperWrightError(
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


def _normalize_export_metadata(output: Path) -> list[dict[str, Any]]:
    """Normalize reproducibility-variant metadata and SVG whitespace."""
    paths = sorted(
        [
            *(output / "gerber").glob("*"),
            *(output / "drill").glob("*"),
            *(output / "svg").glob("*"),
            output / "board_stats.json",
            output / "schematic.pdf",
            output / "board.step",
        ]
    )
    records = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        before = read_bytes_limited(path, 128 * 1024 * 1024)
        normalized, creation_time_replacements = _normalize_creation_times(before)
        svg_trailing_whitespace_replacements = 0
        if path.suffix.lower() == ".svg":
            normalized, svg_trailing_whitespace_replacements = (
                _normalize_svg_trailing_whitespace(normalized)
            )
        if creation_time_replacements or svg_trailing_whitespace_replacements:
            atomic_write_bytes(path, normalized, mode=0o644)
        records.append(
            {
                "path": path.relative_to(output).as_posix(),
                "original_sha256": hashlib.sha256(before).hexdigest(),
                "content_sha256": hashlib.sha256(normalized).hexdigest(),
                "creation_time_fields_normalized": creation_time_replacements,
                "svg_trailing_whitespace_fields_normalized": svg_trailing_whitespace_replacements,
            }
        )
    return records


def _normalize_creation_times(data: bytes) -> tuple[bytes, int]:
    replacements = 0
    patterns = (
        (
            rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
            b"1980-01-01T00:00:00+00:00",
        ),
        (
            rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            b"1980-01-01T00:00:00",
        ),
        (
            rb"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
            b"1980-01-01 00:00:00",
        ),
        (
            rb"D:\d{4}:\d{2}:\d{2}:\d{2}:\d{2}:\d{2}",
            b"D:1980:01:01:00:00:00",
        ),
    )
    result = data
    for pattern, replacement in patterns:
        result, count = re.subn(pattern, replacement, result)
        replacements += count
    return result, replacements


def _normalize_svg_trailing_whitespace(data: bytes) -> tuple[bytes, int]:
    return re.subn(rb"[ \t]+(?=\r?\n)", b"", data)


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
        if path.is_file()
        and path.relative_to(root).as_posix() not in exclude
        and path.relative_to(root).as_posix() not in AUDIT_ARTIFACT_PATHS
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


def _inventory_exact(
    root: Path, relative_paths: frozenset[str]
) -> list[dict[str, Any]]:
    records = []
    for relative in sorted(relative_paths):
        path = _single_link_file(root / relative)
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path, max_bytes=16 * 1024 * 1024),
            }
        )
    return records


def _verify_audit_artifacts(root: Path, value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(AUDIT_ARTIFACT_PATHS):
        raise ValidationError("release audit artifact list is malformed")
    records: dict[str, dict[str, Any]] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValidationError("release audit artifact entry is malformed")
        relative = entry["path"]
        size = entry["size"]
        digest = entry["sha256"]
        if (
            relative not in AUDIT_ARTIFACT_PATHS
            or relative in records
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= 16 * 1024 * 1024
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValidationError("release audit artifact identity is invalid")
        path = _single_link_file(root / relative)
        if (
            path.stat().st_size != size
            or sha256_file(path, max_bytes=16 * 1024 * 1024) != digest
        ):
            raise ValidationError(f"release audit artifact hash mismatch: {relative}")
        records[relative] = entry
    if set(records) != set(AUDIT_ARTIFACT_PATHS):
        raise ValidationError("release audit artifact set is incomplete")
    return records


def _write_archive(root: Path, output: Path) -> None:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path != output
        and path.relative_to(root).as_posix() != "receipt.json"
        and path.relative_to(root).as_posix() not in AUDIT_ARTIFACT_PATHS
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, read_bytes_limited(path, 128 * 1024 * 1024))
    output.chmod(0o644)


def _verify_archive(
    archive_path: Path,
    manifest_path: Path,
    expected: dict[str, dict[str, Any]],
) -> None:
    expected_names = set(expected) | {"release-manifest.json"}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)) or set(names) != expected_names:
                raise ValidationError("release archive membership is invalid")
            total = 0
            for member in members:
                if not _safe_release_relative(member.filename) or member.is_dir():
                    raise ValidationError("release archive contains an unsafe member")
                if member.flag_bits & 0x1:
                    raise ValidationError("release archive must not contain encryption")
                if member.date_time != (1980, 1, 1, 0, 0, 0):
                    raise ValidationError(
                        "release archive contains a non-reproducible timestamp"
                    )
                expected_size = (
                    manifest_path.stat().st_size
                    if member.filename == "release-manifest.json"
                    else expected[member.filename]["size"]
                )
                if member.file_size != expected_size:
                    raise ValidationError("release archive member size is invalid")
                total += member.file_size
                if total > MAX_RELEASE_BYTES:
                    raise ValidationError("release archive expands beyond its limit")
                digest = hashlib.sha256()
                consumed = 0
                with archive.open(member) as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        consumed += len(chunk)
                        if consumed > 128 * 1024 * 1024:
                            raise ValidationError(
                                "release archive member expands beyond its limit"
                            )
                        digest.update(chunk)
                expected_digest = (
                    sha256_file(manifest_path, max_bytes=16 * 1024 * 1024)
                    if member.filename == "release-manifest.json"
                    else expected[member.filename]["sha256"]
                )
                if digest.hexdigest() != expected_digest:
                    raise ValidationError("release archive member hash mismatch")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValidationError("release archive is unreadable or corrupt") from exc


def _single_link_file(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError(f"release file is missing: {path.name}") from exc
    if path.is_symlink() or not path.is_file() or info.st_nlink != 1:
        raise ValidationError(f"release file is unsafe: {path.name}")
    return path


def _safe_release_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and path.as_posix() == value
    )
