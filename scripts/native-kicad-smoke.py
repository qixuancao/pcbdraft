#!/usr/bin/env python3
"""Generate and validate the bundled deterministic board with real KiCad.

This is a CI entry point, not a user-facing first-board command and not a real
model example.  It retains the existing ATtiny/sensor/I2C/LED fixture coverage
while replacing CLI commands that are no longer part of the product surface.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import load_json_limited
from pcbdraft.core.project import sha256_file
from pcbdraft.services.managed import generate_managed_project
from pcbdraft.verification.benchmark import bundled_requirements_path
from pcbdraft.verification.validation import ValidationRun, validate_managed_project

RECEIPT_LIMIT = 16 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the bundled deterministic ATtiny fixture, run real KiCad "
            "ERC/DRC and parity validation, and print the canonical receipt."
        )
    )
    parser.add_argument(
        "output",
        type=Path,
        help="fresh directory below which the immutable smoke evidence is written",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        metavar="SEC",
        help="bounded validation timeout in seconds (default: 180)",
    )
    return parser


def _validated_receipt(validation: ValidationRun) -> dict[str, Any]:
    receipt = load_json_limited(validation.output_dir / "receipt.json", RECEIPT_LIMIT)
    report = load_json_limited(validation.report_path, RECEIPT_LIMIT)
    tool_runs = receipt.get("tool_runs") if isinstance(receipt, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "pcbdraft-validation-receipt"
        or receipt.get("version") != 1
        or receipt.get("status") != "complete"
        or receipt.get("candidate_ready") is not True
        or receipt.get("report") != validation.report_path.name
        or receipt.get("report_sha256") != validation.report_sha256
        or validation.report_sha256
        != sha256_file(validation.report_path, max_bytes=RECEIPT_LIMIT)
        or not isinstance(tool_runs, dict)
        or set(tool_runs) != {"erc", "drc"}
        or any(
            not isinstance(tool_runs[name], dict)
            or tool_runs[name].get("status") != "completed"
            or tool_runs[name].get("failure") is not None
            for name in ("erc", "drc")
        )
        or not isinstance(report, dict)
        or report.get("schema") != "pcbdraft-validation"
        or report.get("version") != 2
        or not isinstance(report.get("readiness"), dict)
        or report["readiness"].get("engineering_candidate") is not True
    ):
        raise ValidationError("native KiCad smoke validation receipt is inconsistent")
    return receipt


def run_smoke(output: Path, *, timeout: float) -> dict[str, Any]:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 3600
    ):
        raise ValidationError("native KiCad smoke timeout must be in (0, 3600]")
    root = output.expanduser()
    if root.name in {"", ".", ".."} or root.is_symlink() or root.exists():
        raise ValidationError(
            "native KiCad smoke output path must be safe, fresh, and absent"
        )
    generated = generate_managed_project(bundled_requirements_path(), root / "project")
    generated.project.assert_synchronized()
    validation = validate_managed_project(
        generated.project,
        output=root / "validation",
        timeout=timeout,
    )
    if not validation.candidate_ready:
        raise ValidationError(
            "native KiCad smoke did not produce a candidate-ready board"
        )
    return _validated_receipt(validation)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_smoke(args.output, timeout=args.timeout)
    except PCBDraftError as exc:
        print(f"native KiCad smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
