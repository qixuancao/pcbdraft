"""Deterministic KiCad ERC/DRC execution and tolerant JSON counting."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.io import load_json_limited, make_directory
from pcbdraft.core.process import redact_argv, remaining_timeout, run_command
from pcbdraft.core.project import ProjectFiles, sha256_file

GATE_JSON_LIMIT = 16 * 1024 * 1024
GATE_PROCESS_OUTPUT_LIMIT = 1024 * 1024
VIOLATION_EXIT_CODES = {5}


@dataclass(frozen=True)
class GateResult:
    name: str
    tool_status: str
    exit_code: int | None
    error_count: int | None
    warning_count: int | None
    raw_report: str
    raw_report_sha256: str | None
    argv: list[str]
    duration_seconds: float
    failure_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_status": self.tool_status,
            "exit_code": self.exit_code,
            "counts": {"error": self.error_count, "warning": self.warning_count},
            "raw_report": self.raw_report,
            "raw_report_sha256": self.raw_report_sha256,
            "argv": self.argv,
            "duration_seconds": self.duration_seconds,
            "failure_kind": self.failure_kind,
        }


def count_severities(document: Any) -> tuple[int, int]:
    """Count violation objects across known and future KiCad JSON nesting."""
    counts = {"error": 0, "warning": 0}
    saw_explicit_severity = False

    def visit(value: Any) -> None:
        nonlocal saw_explicit_severity
        if isinstance(value, dict):
            severity = value.get("severity")
            if isinstance(severity, str) and severity.lower() in counts:
                counts[severity.lower()] += 1
                saw_explicit_severity = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    if saw_explicit_severity:
        return counts["error"], counts["warning"]

    # Some KiCad variants expose aggregate buckets only.
    def aggregate(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower()
                if normalized in ("error", "errors"):
                    if isinstance(child, int) and not isinstance(child, bool):
                        counts["error"] += child
                    elif isinstance(child, list):
                        counts["error"] += len(child)
                elif normalized in ("warning", "warnings"):
                    if isinstance(child, int) and not isinstance(child, bool):
                        counts["warning"] += child
                    elif isinstance(child, list):
                        counts["warning"] += len(child)
                aggregate(child)
        elif isinstance(value, list):
            for child in value:
                aggregate(child)

    aggregate(document)
    return counts["error"], counts["warning"]


def _bounded_string(value: Any, limit: int = 1000) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def structured_violations(
    document: Any, *, max_violations: int = 100
) -> dict[str, Any]:
    """Extract bounded violation records without depending on one KiCad JSON layout."""
    records: list[dict[str, Any]] = []
    total_seen = 0

    def visit(value: Any, location: str) -> None:
        nonlocal total_seen
        if isinstance(value, dict):
            severity = value.get("severity")
            if isinstance(severity, str) and severity.lower() in {"error", "warning"}:
                total_seen += 1
                if len(records) < max_violations:
                    record: dict[str, Any] = {
                        "json_location": location,
                        "severity": severity.lower(),
                    }
                    for key in ("type", "description", "sheet", "path"):
                        bounded = _bounded_string(value.get(key), 512)
                        if bounded is not None:
                            record[key] = bounded
                    items = value.get("items")
                    if isinstance(items, list):
                        compact_items: list[dict[str, Any]] = []
                        for item in items[:5]:
                            if not isinstance(item, dict):
                                continue
                            compact: dict[str, Any] = {}
                            for key in ("description", "uuid"):
                                bounded = _bounded_string(item.get(key), 512)
                                if bounded is not None:
                                    compact[key] = bounded
                            position = item.get("pos")
                            if isinstance(position, dict):
                                compact["pos"] = {
                                    axis: coordinate
                                    for axis, coordinate in position.items()
                                    if axis in {"x", "y"}
                                    and isinstance(coordinate, (int, float))
                                    and not isinstance(coordinate, bool)
                                }
                            if compact:
                                compact_items.append(compact)
                        if compact_items:
                            record["items"] = compact_items
                    records.append(record)
            for key, child in value.items():
                visit(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]")

    visit(document, "$")
    ignored: list[dict[str, str]] = []
    if isinstance(document, dict) and isinstance(document.get("ignored_checks"), list):
        for check in document["ignored_checks"][:100]:
            if not isinstance(check, dict):
                continue
            compact = {
                key: bounded
                for key in ("key", "description")
                if (bounded := _bounded_string(check.get(key))) is not None
            }
            if compact:
                ignored.append(compact)
    return {
        "violations": records,
        "violation_count_seen": total_seen,
        "violations_truncated": total_seen > len(records),
        "ignored_checks": ignored,
    }


def collect_structured_evidence(
    *,
    output_dir: Path,
    results: Mapping[str, GateResult],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for name, result in results.items():
        if result.tool_status != "ok":
            evidence[name] = {"available": False, "failure_kind": result.failure_kind}
            continue
        try:
            document = load_json_limited(
                output_dir / result.raw_report, GATE_JSON_LIMIT
            )
            evidence[name] = {"available": True, **structured_violations(document)}
        except PCBDraftError:
            evidence[name] = {
                "available": False,
                "failure_kind": "evidence_parse_failed",
            }
    return evidence


def _failed_result(
    *,
    name: str,
    raw_report: str,
    argv: list[str],
    exit_code: int | None,
    duration: float,
    failure_kind: str,
) -> GateResult:
    return GateResult(
        name=name,
        tool_status="tool_failed",
        exit_code=exit_code,
        error_count=None,
        warning_count=None,
        raw_report=raw_report,
        raw_report_sha256=None,
        argv=argv,
        duration_seconds=duration,
        failure_kind=failure_kind,
    )


def run_gate(
    *,
    name: str,
    input_file: Path,
    raw_output: Path,
    executable: str,
    deadline: float,
    redactions: Mapping[str, str],
) -> GateResult:
    if name == "erc":
        argv = [
            executable,
            "sch",
            "erc",
            "--format",
            "json",
            "--severity-error",
            "--severity-warning",
            "--output",
            str(raw_output),
            str(input_file),
        ]
    elif name == "drc":
        argv = [
            executable,
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-error",
            "--severity-warning",
            "--output",
            str(raw_output),
            str(input_file),
        ]
    else:
        raise PCBDraftError(f"unknown deterministic gate: {name}")

    redacted = redact_argv(argv, redactions)
    try:
        command_timeout = remaining_timeout(deadline)
    except PCBDraftError:
        return _failed_result(
            name=name,
            raw_report=raw_output.name,
            argv=redacted,
            exit_code=None,
            duration=0.0,
            failure_kind="timeout",
        )
    try:
        result = run_command(
            argv,
            cwd=input_file.parent,
            timeout=command_timeout,
            max_output_bytes=GATE_PROCESS_OUTPUT_LIMIT,
        )
    except PCBDraftError:
        return _failed_result(
            name=name,
            raw_report=raw_output.name,
            argv=redacted,
            exit_code=None,
            duration=0.0,
            failure_kind="launch_failed",
        )

    if result.timed_out:
        return _failed_result(
            name=name,
            raw_report=raw_output.name,
            argv=redacted,
            exit_code=result.returncode,
            duration=result.duration_seconds,
            failure_kind="timeout",
        )
    if result.output_limited:
        return _failed_result(
            name=name,
            raw_report=raw_output.name,
            argv=redacted,
            exit_code=result.returncode,
            duration=result.duration_seconds,
            failure_kind="output_limit",
        )

    try:
        raw_output.chmod(0o600)
        document = load_json_limited(raw_output, GATE_JSON_LIMIT)
        errors, warnings = count_severities(document)
        report_hash = sha256_file(raw_output, max_bytes=GATE_JSON_LIMIT)
    except (OSError, PCBDraftError):
        return _failed_result(
            name=name,
            raw_report=raw_output.name,
            argv=redacted,
            exit_code=result.returncode,
            duration=result.duration_seconds,
            failure_kind="missing_or_invalid_json",
        )

    if result.returncode != 0 and result.returncode not in VIOLATION_EXIT_CODES:
        return _failed_result(
            name=name,
            raw_report=raw_output.name,
            argv=redacted,
            exit_code=result.returncode,
            duration=result.duration_seconds,
            failure_kind="exit_code",
        )

    return GateResult(
        name=name,
        tool_status="ok",
        exit_code=result.returncode,
        error_count=errors,
        warning_count=warnings,
        raw_report=raw_output.name,
        raw_report_sha256=report_hash,
        argv=redacted,
        duration_seconds=result.duration_seconds,
    )


def run_gates(
    *,
    files: ProjectFiles,
    output_dir: Path,
    deadline: float,
    redactions: Mapping[str, str],
    executable: str | None = None,
    fail_fast: bool = True,
) -> dict[str, GateResult]:
    make_directory(output_dir)
    resolved_executable = executable or shutil.which("kicad-cli")
    if not resolved_executable:
        raise PCBDraftError("required executable not found: kicad-cli")

    results: dict[str, GateResult] = {}
    erc = run_gate(
        name="erc",
        input_file=files.schematic,
        raw_output=output_dir / "erc.json",
        executable=resolved_executable,
        deadline=deadline,
        redactions=redactions,
    )
    results["erc"] = erc
    if fail_fast and erc.tool_status != "ok":
        return results

    drc = run_gate(
        name="drc",
        input_file=files.board,
        raw_output=output_dir / "drc.json",
        executable=resolved_executable,
        deadline=deadline,
        redactions=redactions,
    )
    results["drc"] = drc
    return results


def gates_are_runnable(results: Mapping[str, GateResult]) -> bool:
    return all(
        name in results and results[name].tool_status == "ok" for name in ("erc", "drc")
    )


def gate_dict(results: Mapping[str, GateResult], *, prefix: str) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for name, result in results.items():
        value = result.to_dict()
        value["raw_report"] = f"{prefix}/{result.raw_report}"
        rendered[name] = value
    return rendered
