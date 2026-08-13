"""Deterministic, bounded KiCad preview generation for local product UIs."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PcbAgentError, ValidationError
from .io import atomic_write_json, make_directory
from .managed import ManagedProject, open_managed_project
from .process import printable_first_line, run_command
from .project import sha256_file
from .runs import utc_timestamp

PREVIEW_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class PreviewBundle:
    root: Path
    receipt_path: Path
    files: dict[str, Path]
    design_content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "receipt": str(self.receipt_path),
            "design_content_hash": self.design_content_hash,
            "files": {key: str(value) for key, value in sorted(self.files.items())},
        }


def generate_previews(
    project_value: ManagedProject | str | Path,
    output: str | Path,
    *,
    timeout: float = 90.0,
) -> PreviewBundle:
    """Export schematic/PCB SVGs, a PDF, and a real 3D board render."""

    if timeout <= 0 or timeout > 600:
        raise ValidationError("preview timeout must be in (0, 600] seconds")
    project = (
        project_value
        if isinstance(project_value, ManagedProject)
        else open_managed_project(project_value)
    )
    project.assert_synchronized()
    root = Path(output).expanduser().resolve(strict=False)
    if root.name in {"", ".", ".."} or root.is_symlink():
        raise ValidationError("preview output path is unsafe")
    if root.exists():
        raise ValidationError("preview output already exists")
    make_directory(root)
    schematic_dir = make_directory(root / "schematic-svg")
    executable = shutil.which("kicad-cli")
    if executable is None:
        raise PcbAgentError("required executable not found: kicad-cli")
    board_svg = root / "board.svg"
    board_png = root / "board-top.png"
    schematic_pdf = root / "schematic.pdf"
    commands = [
        (
            "schematic_svg",
            [
                executable,
                "sch",
                "export",
                "svg",
                "--output",
                str(schematic_dir),
                "--exclude-drawing-sheet",
                str(project.schematic_path),
            ],
        ),
        (
            "schematic_pdf",
            [
                executable,
                "sch",
                "export",
                "pdf",
                "--output",
                str(schematic_pdf),
                str(project.schematic_path),
            ],
        ),
        (
            "board_svg",
            [
                executable,
                "pcb",
                "export",
                "svg",
                "--output",
                str(board_svg),
                "--layers",
                "F.Cu,F.Mask,F.SilkS,Edge.Cuts",
                "--mode-single",
                "--fit-page-to-board",
                "--exclude-drawing-sheet",
                str(project.board_path),
            ],
        ),
        (
            "board_render",
            [
                executable,
                "pcb",
                "render",
                "--output",
                str(board_png),
                "--width",
                "1200",
                "--height",
                "800",
                "--side",
                "top",
                "--quality",
                "basic",
                str(project.board_path),
            ],
        ),
    ]
    deadline = time.monotonic() + timeout
    runs: list[dict[str, Any]] = []
    for name, argv in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PcbAgentError("preview workflow timeout expired")
        result = run_command(
            argv,
            cwd=project.root,
            timeout=remaining,
            max_output_bytes=2 * 1024 * 1024,
        )
        runs.append(
            {
                "name": name,
                "exit_code": result.returncode,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
                "output_limited": result.output_limited,
                "failure": (
                    printable_first_line(result.stderr or result.stdout)
                    if result.returncode != 0
                    else None
                ),
            }
        )
        if result.returncode != 0 or result.timed_out or result.output_limited:
            raise PcbAgentError(f"KiCad preview export failed: {name}")
    schematic_svgs = sorted(
        path
        for path in schematic_dir.glob("*.svg")
        if path.is_file() and not path.is_symlink()
    )
    if len(schematic_svgs) != 1:
        raise ValidationError("expected exactly one schematic SVG preview")
    files = {
        "schematic_svg": schematic_svgs[0],
        "schematic_pdf": schematic_pdf,
        "board_svg": board_svg,
        "board_render": board_png,
    }
    inventory: dict[str, Any] = {}
    for name, path in files.items():
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"preview output is missing or unsafe: {name}")
        size = path.stat().st_size
        if size <= 0 or size > PREVIEW_MAX_BYTES:
            raise ValidationError(f"preview output size is invalid: {name}")
        inventory[name] = {
            "path": path.relative_to(root).as_posix(),
            "bytes": size,
            "sha256": sha256_file(path, max_bytes=PREVIEW_MAX_BYTES),
        }
    receipt_path = root / "receipt.json"
    atomic_write_json(
        receipt_path,
        {
            "schema": "copperwright-preview-bundle",
            "version": 1,
            "created_at": utc_timestamp(),
            "design_content_hash": project.design.content_hash(),
            "files": inventory,
            "tool_runs": runs,
        },
    )
    return PreviewBundle(
        root=root,
        receipt_path=receipt_path,
        files=files,
        design_content_hash=project.design.content_hash(),
    )
