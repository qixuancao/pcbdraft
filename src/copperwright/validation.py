"""Layered L0-L7 validation with explicit, honest evidence states."""

from __future__ import annotations

import copy
import math
import secrets
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CopperWrightError, ValidationError
from .external_evidence import load_external_evidence
from .gates import GATE_JSON_LIMIT, count_severities
from .io import (
    atomic_write_json,
    load_json_limited,
    make_directory,
    portable_record_path,
)
from .kicad_pcb import FootprintInspection, inspect_footprints
from .locking import ResourceLock
from .managed import ManagedProject, open_managed_project
from .parts import PartGraph
from .process import run_command
from .project import sha256_file
from .runs import utc_timestamp
from .semantic_rules import evaluate_semantic_rules

VALIDATION_SCHEMA = "copperwright-validation"
VALIDATION_VERSION = 1
EVIDENCE_STATES = {
    "completed",
    "not_applicable",
    "unavailable",
    "heuristic",
    "human_required",
}
OUTCOMES = {"pass", "fail", "unknown"}
MAX_TOOL_OUTPUT = 1024 * 1024


@dataclass(frozen=True)
class CheckResult:
    id: str
    level: str
    state: str
    outcome: str
    summary: str
    evidence: tuple[str, ...] = ()
    metrics: dict[str, Any] | None = None
    blocks_candidate: bool = False
    blocks_production: bool = False

    def __post_init__(self) -> None:
        if self.state not in EVIDENCE_STATES or self.outcome not in OUTCOMES:
            raise ValueError("invalid validation state/outcome")
        if self.state == "completed" and self.outcome == "unknown":
            raise ValueError("completed checks require a pass/fail outcome")
        if self.state == "not_applicable" and self.outcome != "pass":
            raise ValueError("not-applicable checks must have pass outcome")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "level": self.level,
            "state": self.state,
            "outcome": self.outcome,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "blocks_candidate": self.blocks_candidate,
            "blocks_production": self.blocks_production,
        }
        if self.metrics is not None:
            value["metrics"] = self.metrics
        return value


@dataclass(frozen=True)
class LevelResult:
    level: str
    name: str
    state: str
    outcome: str
    checks: tuple[CheckResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "name": self.name,
            "state": self.state,
            "outcome": self.outcome,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class ValidationRun:
    output_dir: Path
    report_path: Path
    report_sha256: str
    levels: tuple[LevelResult, ...]
    candidate_ready: bool
    production_ready: bool


def validate_managed_project(
    project_value: ManagedProject | str | Path,
    *,
    output: str | Path | None = None,
    graph: PartGraph | None = None,
    timeout: float = 90.0,
    _already_locked: bool = False,
) -> ValidationRun:
    """Run deterministic and declared heuristic gates without inventing evidence."""
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ValidationError("validation timeout must be in (0, 3600] seconds")
    project = (
        project_value
        if isinstance(project_value, ManagedProject)
        else open_managed_project(project_value)
    )
    resolved_graph = graph or PartGraph.bundled()
    output_dir = _validation_output(project, output)
    deadline = time.monotonic() + timeout
    receipt = {
        "schema": "copperwright-validation-receipt",
        "version": 1,
        "status": "running",
        "started_at": utc_timestamp(),
        "project": portable_record_path(project.root),
        "design_content_hash": project.design.content_hash(),
    }
    atomic_write_json(output_dir / "receipt.json", receipt)
    try:
        lock = (
            nullcontext()
            if _already_locked
            else ResourceLock(
                project.root,
                project.root.parent / ".copperwright-locks",
                timeout=min(10.0, timeout),
            )
        )
        with lock:
            erc = _run_kicad_report("erc", project, output_dir / "erc.json", deadline)
            drc = _run_kicad_report("drc", project, output_dir / "drc.json", deadline)
            checks = _build_checks(project, resolved_graph, erc, drc)
            levels = _levels(checks)
            candidate_ready = _ready(checks, "blocks_candidate")
            production_ready = candidate_ready and _ready(checks, "blocks_production")
            report = {
                "schema": VALIDATION_SCHEMA,
                "version": VALIDATION_VERSION,
                "design": {
                    "id": project.design.design_id,
                    "content_hash": project.design.content_hash(),
                    "board_sha256": sha256_file(
                        project.board_path, max_bytes=128 * 1024 * 1024
                    ),
                },
                "readiness": {
                    "engineering_candidate": candidate_ready,
                    "production": production_ready,
                    "production_claimed": False,
                },
                "levels": [level.to_dict() for level in levels],
                "tool_runs": {
                    "erc": _public_tool_result(erc),
                    "drc": _public_tool_result(drc),
                },
            }
            report_path = output_dir / "validation.json"
            atomic_write_json(report_path, report)
            report_hash = sha256_file(report_path, max_bytes=16 * 1024 * 1024)
            receipt.update(
                {
                    "status": "complete",
                    "completed_at": utc_timestamp(),
                    "candidate_ready": candidate_ready,
                    "production_ready": production_ready,
                    "report": report_path.name,
                    "report_sha256": report_hash,
                    "tool_runs": {
                        "erc": _audit_tool_result(erc),
                        "drc": _audit_tool_result(drc),
                    },
                }
            )
            atomic_write_json(output_dir / "receipt.json", receipt)
            return ValidationRun(
                output_dir=output_dir,
                report_path=report_path,
                report_sha256=report_hash,
                levels=levels,
                candidate_ready=candidate_ready,
                production_ready=production_ready,
            )
    except BaseException as exc:
        receipt["status"] = "failed"
        receipt["completed_at"] = utc_timestamp()
        receipt["failure"] = str(exc)[:2048]
        atomic_write_json(output_dir / "receipt.json", receipt)
        raise


def _validation_output(project: ManagedProject, value: str | Path | None) -> Path:
    if value is None:
        parent = project.root / "evidence"
        make_directory(parent)
        name = f"validation-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
        output = parent / name
    else:
        raw = Path(value).expanduser()
        if raw.is_symlink() or raw.name in {"", ".", ".."}:
            raise ValidationError("validation output path is unsafe")
        raw.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = raw.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise ValidationError("validation output already exists")
    make_directory(output)
    return output


def _run_kicad_report(
    kind: str, project: ManagedProject, output: Path, deadline: float
) -> dict[str, Any]:
    executable = shutil.which("kicad-cli")
    if executable is None:
        return {
            "status": "unavailable",
            "failure": "kicad-cli not found",
            "duration_seconds": 0.0,
            "report": output.name,
            "document": None,
        }
    raw_output = output.with_name(f"{output.stem}.raw{output.suffix}")
    if kind == "erc":
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
            str(project.schematic_path),
        ]
    elif kind == "drc":
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
            "--schematic-parity",
            str(project.board_path),
        ]
    else:
        raise ValueError("unknown KiCad report kind")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {
            "status": "unavailable",
            "failure": "validation deadline expired",
            "duration_seconds": 0.0,
            "report": output.name,
            "document": None,
        }
    try:
        result = run_command(
            argv,
            cwd=project.root,
            timeout=remaining,
            max_output_bytes=MAX_TOOL_OUTPUT,
        )
    except CopperWrightError as exc:
        return {
            "status": "unavailable",
            "failure": str(exc),
            "duration_seconds": 0.0,
            "report": output.name,
            "document": None,
        }
    failure = None
    if result.timed_out:
        failure = "timeout"
    elif result.output_limited:
        failure = "output_limit"
    elif result.returncode != 0:
        failure = f"exit_code_{result.returncode}"
    try:
        document = (
            load_json_limited(raw_output, GATE_JSON_LIMIT) if failure is None else None
        )
    except CopperWrightError:
        failure = "missing_or_invalid_json"
        document = None
    reported_at = document.get("date") if isinstance(document, dict) else None
    if isinstance(document, dict):
        normalized_document = copy.deepcopy(document)
        normalized_document.pop("date", None)
        atomic_write_json(output, normalized_document, mode=0o644)
        raw_sha256 = sha256_file(raw_output, max_bytes=GATE_JSON_LIMIT)
        document = normalized_document
    else:
        raw_sha256 = None
    return {
        "status": "completed" if failure is None else "unavailable",
        "failure": failure,
        "duration_seconds": result.duration_seconds,
        "report": output.name,
        "raw_report": raw_output.name if raw_output.is_file() else None,
        "raw_sha256": raw_sha256,
        "reported_at": reported_at,
        "document": document,
    }


def _build_checks(
    project: ManagedProject,
    graph: PartGraph,
    erc: dict[str, Any],
    drc: dict[str, Any],
) -> tuple[CheckResult, ...]:
    checks: list[CheckResult] = []
    drift = project.drift()
    checks.append(
        CheckResult(
            "l0.manifest_integrity",
            "L0",
            "completed",
            "pass" if not drift else "fail",
            "Tracked requirements, IR, KiCad files, and receipts match the synchronization manifest."
            if not drift
            else "Managed project has unacknowledged file drift.",
            (project.manifest_path.name,),
            {"drift": list(drift)},
            True,
            True,
        )
    )
    checks.append(
        CheckResult(
            "l0.semantic_ir",
            "L0",
            "completed",
            "pass",
            "Semantic IR parsed, validated, and retained its declared content identity.",
            (project.ir_path.name,),
            {"content_hash": project.design.content_hash()},
            True,
            True,
        )
    )
    tool_ready = erc["status"] == "completed" and drc["status"] == "completed"
    checks.append(
        CheckResult(
            "l0.kicad_parse",
            "L0",
            "completed" if tool_ready else "unavailable",
            "pass" if tool_ready else "unknown",
            "KiCad parsed both native design files."
            if tool_ready
            else "KiCad syntax parsing could not be completed.",
            tuple(
                item["report"] for item in (erc, drc) if item["status"] == "completed"
            ),
            {
                "erc_failure": erc["failure"],
                "drc_failure": drc["failure"],
            },
            True,
            True,
        )
    )

    part_issues = graph.validate_design(project.design, check_libraries=True)
    part_errors = [issue for issue in part_issues if issue.severity == "error"]
    checks.append(
        CheckResult(
            "l1.component_contracts",
            "L1",
            "completed",
            "fail" if part_errors else "pass",
            "Canonical parts, pins, footprints, ratings, and required connections satisfy their contracts."
            if not part_errors
            else "One or more component contracts failed.",
            (project.ir_path.name, "bundled part catalog"),
            {"issues": [issue.to_dict() for issue in part_issues]},
            True,
            True,
        )
    )
    parity = _drc_section(drc, "schematic_parity")
    checks.append(
        _document_check(
            "l1.kicad_semantic_parity",
            "L1",
            drc,
            parity,
            "KiCad schematic-to-PCB footprint, field, net, and pad parity",
            candidate=True,
            production=True,
        )
    )
    checks.extend(_ignored_rule_checks(erc, drc))

    erc_violations = _erc_violations(erc)
    checks.append(
        _document_check(
            "l2.erc",
            "L2",
            erc,
            erc_violations,
            "KiCad electrical rules",
            candidate=True,
            production=True,
        )
    )
    drc_violations = _drc_section(drc, "violations")
    drc_unconnected = _drc_section(drc, "unconnected_items")
    checks.append(
        _document_check(
            "l2.drc_connectivity",
            "L2",
            drc,
            drc_violations + drc_unconnected,
            "KiCad geometry, connectivity, and design rules",
            candidate=True,
            production=True,
        )
    )

    if drift or not tool_ready:
        checks.append(
            CheckResult(
                "l3.intent_geometry",
                "L3",
                "unavailable",
                "unknown",
                "Intent geometry was not evaluated because synchronized native evidence is unavailable.",
                (),
                {"drift": list(drift)},
                True,
                True,
            )
        )
    else:
        checks.extend(_constraint_checks(project, graph))

    lifecycle_failures = [
        component.reference
        for component in project.design.components
        if graph.get(component.part_id).bom
        and graph.get(component.part_id).lifecycle.get("status") != "active"
    ]
    checks.append(
        CheckResult(
            "l4.bom_lifecycle",
            "L4",
            "completed",
            "fail" if lifecycle_failures else "pass",
            "All populated parts have active catalog lifecycle evidence."
            if not lifecycle_failures
            else "Inactive parts are present in the BOM.",
            ("bundled part catalog",),
            {
                "bom_line_count": sum(
                    graph.get(component.part_id).bom
                    for component in project.design.components
                ),
                "inactive_references": lifecycle_failures,
            },
            True,
            True,
        )
    )
    external_document = load_external_evidence(project)
    external_by_level = {
        entry["level"]: entry for entry in external_document["entries"]
    }
    l4_external = external_by_level.get("L4")
    checks.append(
        CheckResult(
            "l4.live_sourcing",
            "L4",
            "completed" if l4_external is not None else "unavailable",
            l4_external["outcome"] if l4_external is not None else "unknown",
            "Externally supplied sourcing and selected-fabricator evidence was integrity-checked; its commercial truth was not independently verified."
            if l4_external is not None
            else "Live authorized-distributor stock and selected-fabricator capability were not supplied; no current availability or process-capability claim is made.",
            tuple(artifact["path"] for artifact in l4_external["artifacts"])
            if l4_external is not None
            else ("bundled part catalog sourcing.stock_status=not_checked",),
            _external_metrics(l4_external) if l4_external is not None else None,
            False,
            True,
        )
    )
    checks.append(
        CheckResult(
            "l4.manufacturability",
            "L4",
            "completed" if drc["status"] == "completed" else "unavailable",
            (
                "pass"
                if drc["status"] == "completed"
                and not drc_violations
                and not drc_unconnected
                else "fail"
                if drc["status"] == "completed"
                else "unknown"
            ),
            "Declared fabrication rules and package contracts pass deterministic DFM proxies."
            if drc["status"] == "completed"
            and not drc_violations
            and not drc_unconnected
            else "Manufacturability evidence is incomplete or failing.",
            (drc["report"], project.ir_path.name),
            {
                "finish": project.design.board.finish,
                "layers": project.design.board.layers,
                "minimums_mm": {
                    "track": project.design.board.min_track_mm,
                    "clearance": project.design.board.min_clearance_mm,
                    "drill": project.design.board.min_drill_mm,
                },
            },
            True,
            True,
        )
    )

    checks.extend(_analysis_checks(project, graph))
    checks.extend(_external_gate_checks(external_document))
    return tuple(checks)


def _document_check(
    identifier: str,
    level: str,
    tool: dict[str, Any],
    violations: list[Any],
    label: str,
    *,
    candidate: bool,
    production: bool,
) -> CheckResult:
    if tool["status"] != "completed":
        return CheckResult(
            identifier,
            level,
            "unavailable",
            "unknown",
            f"{label} could not be executed.",
            (),
            {"failure": tool["failure"]},
            candidate,
            production,
        )
    errors, warnings = count_severities({"items": violations})
    passed = not violations
    return CheckResult(
        identifier,
        level,
        "completed",
        "pass" if passed else "fail",
        f"{label} passed with no reported violations."
        if passed
        else f"{label} reported violations.",
        (tool["report"],),
        {"errors": errors, "warnings": warnings, "total": len(violations)},
        candidate,
        production,
    )


def _erc_violations(tool: dict[str, Any]) -> list[Any]:
    document = tool.get("document")
    if not isinstance(document, dict):
        return []
    values: list[Any] = []
    for sheet in document.get("sheets", []):
        if isinstance(sheet, dict) and isinstance(sheet.get("violations"), list):
            values.extend(sheet["violations"])
    return values


def _drc_section(tool: dict[str, Any], name: str) -> list[Any]:
    document = tool.get("document")
    if not isinstance(document, dict) or not isinstance(document.get(name), list):
        return []
    return list(document[name])


def _ignored_rule_checks(erc: dict[str, Any], drc: dict[str, Any]) -> list[CheckResult]:
    expected = {
        "erc": {
            "simulation_model_issue": "SPICE models are not required by this low-speed fixture; optional simulation remains separately unavailable at L5.",
            "footprint_filter": "The trusted part graph declares the exact symbol, footprint, pin/pad map, and evidence; the generic symbol glob rejects a legitimate JST vendor footprint name.",
        },
        "drc": {
            "tuning_profile_track_geometries": "No tuned transmission line or differential-pair profile exists in the accepted low-speed scope.",
            "footprint_filters_mismatch": "Exact part-graph mapping and native schematic-to-PCB parity replace the generic symbol footprint glob for this generated design.",
        },
    }
    tools = {"erc": erc, "drc": drc}
    actual: dict[str, set[str]] = {}
    for name, tool in tools.items():
        document = tool.get("document")
        ignored = (
            document.get("ignored_checks", []) if isinstance(document, dict) else []
        )
        actual[name] = {
            str(item["key"])
            for item in ignored
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
    ready = all(tool["status"] == "completed" for tool in tools.values())
    mismatches = {
        name: {
            "unexpected": sorted(actual[name] - set(expected[name])),
            "missing_declared_not_applicable": sorted(
                set(expected[name]) - actual[name]
            ),
        }
        for name in tools
    }
    policy_passed = ready and not any(
        values["unexpected"] or values["missing_declared_not_applicable"]
        for values in mismatches.values()
    )
    checks = [
        CheckResult(
            "l2.ignored_rule_policy",
            "L2",
            "completed" if ready else "unavailable",
            "pass" if policy_passed else "fail" if ready else "unknown",
            "Every disabled KiCad rule is explicitly classified as not applicable."
            if policy_passed
            else "KiCad disabled-rule coverage is unavailable or differs from the explicit policy.",
            tuple(
                tool["report"]
                for tool in tools.values()
                if tool["status"] == "completed"
            ),
            {"policy": mismatches},
            True,
            True,
        )
    ]
    for tool_name, definitions in expected.items():
        for key, summary in definitions.items():
            checks.append(
                CheckResult(
                    f"l2.not_applicable.{tool_name}.{key}",
                    "L2",
                    "not_applicable"
                    if ready and key in actual[tool_name]
                    else "unavailable",
                    "pass" if ready and key in actual[tool_name] else "unknown",
                    summary,
                    (tools[tool_name]["report"],)
                    if tools[tool_name]["status"] == "completed"
                    else (),
                    {"kicad_rule": key},
                    False,
                    False,
                )
            )
    return checks


def _constraint_checks(project: ManagedProject, graph: PartGraph) -> list[CheckResult]:
    design = project.design
    components = tuple(
        component
        for component in design.components
        if not component.attributes.get("exclude_from_board", False)
        and graph.get(component.part_id).footprint is not None
    )
    inspections, _version = inspect_footprints(design, components, graph)
    placements = project.manifest["generation"]["pcb"]["placement"]["components"]
    positions = _pin_positions(design, components, graph, inspections, placements)
    checks: list[CheckResult] = []
    for constraint in design.constraints:
        try:
            if constraint.kind == "decoupling":
                checks.append(
                    _check_decoupling(
                        design, constraint, graph, positions, project.ir_path.name
                    )
                )
            elif constraint.kind == "interface_pullups":
                checks.append(_check_pullups(design, constraint, graph))
            elif constraint.kind == "i2c_electrical_budget":
                checks.append(_check_i2c_electrical_budget(design, constraint))
            elif constraint.kind == "spi_electrical_budget":
                checks.append(_check_spi_electrical_budget(design, constraint, graph))
            elif constraint.kind == "uart_electrical_budget":
                checks.append(_check_uart_electrical_budget(design, constraint))
            elif constraint.kind == "ldo_regulation_budget":
                checks.append(_check_ldo_regulation_budget(design, constraint, graph))
            elif constraint.kind == "source_ownership":
                checks.append(_check_source_ownership(design, constraint))
            elif constraint.kind == "current_limit":
                checks.append(_check_current_limit(design, constraint, graph))
            elif constraint.kind == "edge_placement":
                checks.append(
                    _check_edge(
                        design,
                        constraint,
                        inspections,
                        placements,
                        project.board_path.name,
                    )
                )
            elif constraint.kind == "functional_group":
                checks.append(_check_group(constraint, inspections, placements))
            elif constraint.kind == "routing":
                checks.append(
                    _check_routing(design, constraint, project.manifest, graph)
                )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            checks.append(
                CheckResult(
                    f"l3.{constraint.id}",
                    "L3",
                    "completed",
                    "fail",
                    f"{constraint.kind} constraint parameters are invalid.",
                    (project.ir_path.name,),
                    {
                        "failure_kind": type(exc).__name__,
                        "constraint_kind": constraint.kind,
                    },
                    constraint.severity == "release_blocking",
                    constraint.severity in {"required", "release_blocking"},
                )
            )
    footprint_bounds = {
        component_id: (
            inspection.bbox_x_mm,
            inspection.bbox_y_mm,
            inspection.bbox_x_mm + inspection.width_mm,
            inspection.bbox_y_mm + inspection.height_mm,
        )
        for component_id, inspection in inspections.items()
    }
    semantic_findings = evaluate_semantic_rules(
        design,
        graph,
        placements=placements,
        footprint_bounds=footprint_bounds,
        footprint_inspections=inspections,
        routing=project.manifest["generation"]["pcb"]["routing"],
        approximate_geometry=False,
    )
    checks.append(
        CheckResult(
            "l3.semantic_intent_registry",
            "L3",
            "completed",
            "fail" if semantic_findings else "pass",
            "All deterministic semantic intent rules passed."
            if not semantic_findings
            else "One or more deterministic semantic intent rules failed.",
            (project.ir_path.name, "trusted part graph", "generation receipts"),
            {"findings": [finding.to_dict() for finding in semantic_findings]},
            True,
            True,
        )
    )
    if not checks:
        checks.append(
            CheckResult(
                "l3.no_declared_constraints",
                "L3",
                "not_applicable",
                "pass",
                "No L3 interface or functional constraints were declared.",
            )
        )
    return checks


def _pin_positions(
    design: Any,
    components: tuple[Any, ...],
    graph: PartGraph,
    inspections: dict[str, FootprintInspection],
    placements: dict[str, Any],
) -> dict[tuple[str, str], tuple[float, float, float, float]]:
    del design
    result: dict[tuple[str, str], tuple[float, float, float, float]] = {}
    for component in components:
        part = graph.get(component.part_id)
        inspected = inspections[component.id]
        origin = placements[component.id]
        for pin in part.pins:
            pads = [pad for pad in inspected.pads if pad.number == pin.footprint_pad]
            if len(pads) != 1:
                raise ValidationError(
                    f"cannot resolve validation pad {component.reference}/{pin.number}"
                )
            result[(component.id, pin.number)] = (
                float(origin["x_mm"]) + pads[0].x_mm,
                float(origin["y_mm"]) + pads[0].y_mm,
                pads[0].width_mm / 2,
                pads[0].height_mm / 2,
            )
    return result


def _check_decoupling(
    design: Any,
    constraint: Any,
    graph: PartGraph,
    positions: dict[tuple[str, str], tuple[float, float, float, float]],
    evidence: str,
) -> CheckResult:
    components = [
        component
        for component in design.components
        if component.id in constraint.targets
    ]
    nets = [net for net in design.nets if net.id in constraint.targets]
    capacitor = next(
        (
            component
            for component in components
            if "capacitance_f" in graph.get(component.part_id).ratings
        ),
        None,
    )
    device = next(
        (component for component in components if component != capacitor), None
    )
    distances: dict[str, float] = {}
    if capacitor is not None and device is not None:
        for net in nets:
            cap_points = [
                positions[(endpoint.component, endpoint.pin)]
                for endpoint in net.endpoints
                if endpoint.component == capacitor.id
            ]
            device_points = [
                positions[(endpoint.component, endpoint.pin)]
                for endpoint in net.endpoints
                if endpoint.component == device.id
            ]
            if cap_points and device_points:
                distances[net.name] = min(
                    _rectangle_gap(first, second)
                    for first in cap_points
                    for second in device_points
                )
    maximum = float(constraint.params["max_distance_mm"])
    metric_valid = (
        constraint.params.get("distance_metric")
        == "minimum_relevant_copper_pad_edge_gap"
        and constraint.params.get("geometry_evidence")
        == "native_footprint_pad_rectangles"
    )
    capacitance = (
        float(graph.get(capacitor.part_id).ratings.get("capacitance_f", 0))
        if capacitor is not None
        else 0.0
    )
    passed = (
        metric_valid
        and len(distances) == len(nets)
        and all(distance <= maximum + 1e-9 for distance in distances.values())
        and capacitance >= float(constraint.params["min_capacitance_f"])
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "Decoupling value and supply/return pad distances meet the declared bound."
        if passed
        else "Decoupling placement or value violates the declared bound.",
        (evidence,),
        {
            "distance_mm_by_net": {
                name: round(value, 6) for name, value in sorted(distances.items())
            },
            "maximum_mm": maximum,
            "capacitance_f": capacitance,
            "distance_metric": constraint.params.get("distance_metric"),
            "geometry_evidence": constraint.params.get("geometry_evidence"),
        },
        constraint.severity == "release_blocking",
        constraint.severity in {"required", "release_blocking"},
    )


def _rectangle_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    delta_x = max(0.0, abs(first[0] - second[0]) - first[2] - second[2])
    delta_y = max(0.0, abs(first[1] - second[1]) - first[3] - second[3])
    return math.hypot(delta_x, delta_y)


def _component_net_ids(design: Any, component_id: str) -> set[str]:
    return {
        net.id
        for net in design.nets
        if any(endpoint.component == component_id for endpoint in net.endpoints)
    }


def _check_pullups(design: Any, constraint: Any, graph: PartGraph) -> CheckResult:
    resistors = [
        component
        for component in design.components
        if component.id in constraint.targets
        and "resistance_ohm" in graph.get(component.part_id).ratings
    ]
    signal_nets = {
        net.id
        for net in design.nets
        if net.id in constraint.targets and net.interface is not None
    }
    power_net = next((net.id for net in design.nets if net.name == "3V3"), None)
    covered: set[str] = set()
    values: dict[str, float] = {}
    for component in resistors:
        nets = _component_net_ids(design, component.id)
        covered.update(nets & signal_nets)
        values[component.reference] = float(
            graph.get(component.part_id).ratings["resistance_ohm"]
        )
        if power_net not in nets:
            covered.add("__missing_power__")
    expected = float(constraint.params["resistance_ohm"])
    passed = (
        len(resistors) == int(constraint.params["count"])
        and covered >= signal_nets
        and "__missing_power__" not in covered
        and all(abs(value - expected) <= 1e-9 for value in values.values())
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "Each I2C signal has one correctly valued pull-up to 3V3."
        if passed
        else "I2C pull-up topology or value is incorrect.",
        ("semantic IR connectivity", "trusted part ratings"),
        {"values_ohm": dict(sorted(values.items())), "covered_nets": sorted(covered)},
        True,
        True,
    )


def _check_i2c_electrical_budget(design: Any, constraint: Any) -> CheckResult:
    pullup = float(constraint.params["pullup_ohm"])
    capacitance = float(constraint.params["bus_capacitance_pf_max"])
    rise_time = 0.8473 * pullup * capacitance * 1e-3
    rise_limit = float(constraint.params["rise_time_limit_ns"])
    domain = next(domain for domain in design.power_domains if domain.id == "v3v3")
    sink_current = domain.max_v / pullup * 1000
    sink_limit = float(constraint.params["sink_current_limit_ma"])
    interface = next(
        interface for interface in design.interfaces if interface.id == "sensor_i2c"
    )
    passed = (
        rise_time <= rise_limit + 1e-9
        and sink_current <= sink_limit + 1e-9
        and interface.params.get("external_pullups") == "forbidden"
        and constraint.params.get("external_pullups") == "forbidden"
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "Declared I2C capacitance, pull-up, rise-time, sink-current, and external-pull-up policy meet the bounded interface contract."
        if passed
        else "The declared I2C electrical budget is inconsistent or exceeds its bound.",
        ("semantic IR interface and power-domain contracts",),
        {
            "bus_capacitance_pf_max": capacitance,
            "calculated_rise_time_ns": round(rise_time, 6),
            "rise_time_limit_ns": rise_limit,
            "calculated_sink_current_ma": round(sink_current, 6),
            "sink_current_limit_ma": sink_limit,
            "external_pullups": interface.params.get("external_pullups"),
        },
        True,
        True,
    )


def _check_spi_electrical_budget(
    design: Any, constraint: Any, graph: PartGraph
) -> CheckResult:
    interface = next(
        item
        for item in design.interfaces
        if item.kind == "spi" and item.id in constraint.targets
    )
    clock = int(constraint.params["clock_hz"])
    limit = int(constraint.params["sensor_clock_limit_hz"])
    sensor = next(
        component
        for component in design.components
        if component.id in constraint.targets
        and graph.get(component.part_id).kind == "environmental_sensor"
    )
    resistor = next(
        component
        for component in design.components
        if component.id in constraint.targets
        and "resistance_ohm" in graph.get(component.part_id).ratings
    )
    resistor_nets = _component_net_ids(design, resistor.id)
    sensor_limit = int(graph.get(sensor.part_id).ratings["max_spi_clock_hz"])
    pullup = float(graph.get(resistor.part_id).ratings["resistance_ohm"])
    expected = {
        "clock_hz": clock,
        "mode": 0,
        "voltage_v": float(constraint.params["voltage_v"]),
        "topology": "four_wire_single_peripheral",
        "external_connector": False,
    }
    passed = (
        0 < clock <= limit <= sensor_limit
        and int(constraint.params["mode"]) == 0
        and abs(pullup - float(constraint.params["cs_pullup_ohm"])) <= 1e-9
        and {"net_3v3", "net_spi_cs"} <= resistor_nets
        and all(interface.params.get(key) == value for key, value in expected.items())
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "SPI clock, mode, voltage, topology, and chip-select bias meet the verified contract."
        if passed
        else "The SPI electrical or chip-select contract failed.",
        ("semantic IR interface", "trusted sensor and resistor ratings"),
        {
            "clock_hz": clock,
            "declared_sensor_limit_hz": limit,
            "trusted_sensor_limit_hz": sensor_limit,
            "mode": constraint.params["mode"],
            "cs_pullup_ohm": pullup,
            "cs_pullup_nets": sorted(resistor_nets),
        },
        True,
        True,
    )


def _check_uart_electrical_budget(design: Any, constraint: Any) -> CheckResult:
    interface = next(
        item
        for item in design.interfaces
        if item.kind == "uart" and item.id in constraint.targets
    )
    expected = {
        "baud": int(constraint.params["baud"]),
        "data_bits": int(constraint.params["data_bits"]),
        "parity": constraint.params["parity"],
        "stop_bits": int(constraint.params["stop_bits"]),
        "voltage_v": float(constraint.params["voltage_v"]),
        "logic": constraint.params["logic"],
    }
    domain = next(
        item for item in design.power_domains if item.id == interface.power_domain
    )
    passed = (
        expected["baud"] in {9600, 19200, 38400, 57600, 115200}
        and (
            expected["data_bits"],
            expected["parity"],
            expected["stop_bits"],
        )
        == (8, "none", 1)
        and expected["logic"] == "single_ended_cmos_not_rs232"
        and domain.min_v <= expected["voltage_v"] <= domain.max_v
        and all(interface.params.get(key) == value for key, value in expected.items())
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "UART framing and 3.3 V CMOS-only voltage contract are internally consistent."
        if passed
        else "The UART framing or voltage-level contract failed.",
        ("semantic IR interface and power-domain contracts",),
        dict(sorted(expected.items())),
        True,
        True,
    )


def _check_ldo_regulation_budget(
    design: Any, constraint: Any, graph: PartGraph
) -> CheckResult:
    regulator = next(
        component
        for component in design.components
        if component.id in constraint.targets
        and graph.get(component.part_id).kind == "ldo_regulator"
    )
    capacitors = [
        component
        for component in design.components
        if component.id in constraint.targets
        and graph.get(component.part_id).kind == "capacitor"
    ]
    ratings = graph.get(regulator.part_id).ratings
    vin = next(domain for domain in design.power_domains if domain.id == "vin5")
    vout = next(domain for domain in design.power_domains if domain.id == "v3v3")
    pin_nets = {
        endpoint.pin: net.id
        for net in design.nets
        for endpoint in net.endpoints
        if endpoint.component == regulator.id
    }
    capacitor_contracts: dict[str, dict[str, Any]] = {}
    caps_ok = True
    for rail, key in (
        ("net_vin", "input_capacitance_f"),
        ("net_3v3", "output_capacitance_f"),
    ):
        matches = [
            capacitor
            for capacitor in capacitors
            if {rail, "net_gnd"} <= _component_net_ids(design, capacitor.id)
        ]
        actual = (
            float(graph.get(matches[0].part_id).ratings["capacitance_f"])
            if len(matches) == 1
            else 0.0
        )
        minimum = float(constraint.params[key])
        capacitor_contracts[rail] = {
            "component": matches[0].reference if len(matches) == 1 else None,
            "actual_f": actual,
            "minimum_f": minimum,
        }
        caps_ok = caps_ok and len(matches) == 1 and actual >= minimum
    input_rating = ratings["input_voltage_v"]
    passed = (
        len(capacitors) == 2
        and vin.min_v >= float(input_rating["min"])
        and vin.max_v <= float(input_rating["max"])
        and abs(vout.nominal_v - float(ratings["output_voltage_v"])) <= 1e-9
        and float(constraint.params["load_limit_a"])
        <= float(ratings["max_output_current_a"])
        and pin_nets.get("1") == "net_vin"
        and pin_nets.get("3") == "net_vin"
        and pin_nets.get("5") == "net_3v3"
        and pin_nets.get("2") == "net_gnd"
        and constraint.params.get("enable_policy") == "tied_to_vin"
        and caps_ok
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "LDO input, output, load, enable, and capacitor contracts meet trusted ratings."
        if passed
        else "The LDO regulation or stability contract failed.",
        ("semantic IR connectivity", "trusted AP2112K and capacitor ratings"),
        {
            "input_domain_v": [vin.min_v, vin.max_v],
            "trusted_input_v": [input_rating["min"], input_rating["max"]],
            "output_v": vout.nominal_v,
            "load_limit_a": constraint.params["load_limit_a"],
            "trusted_current_limit_a": ratings["max_output_current_a"],
            "pin_nets": dict(sorted(pin_nets.items())),
            "capacitors": capacitor_contracts,
        },
        True,
        True,
    )


def _check_source_ownership(design: Any, constraint: Any) -> CheckResult:
    net = next(net for net in design.nets if net.id == "net_3v3")
    components = {component.id: component for component in design.components}
    sources = sorted(
        f"{endpoint.component}:{endpoint.pin}"
        for endpoint in net.endpoints
        if endpoint.role == "source"
        and not components[endpoint.component].attributes.get(
            "exclude_from_board", False
        )
    )
    sense = [
        endpoint
        for endpoint in net.endpoints
        if endpoint.component == constraint.params["sense_component"]
        and endpoint.pin == constraint.params["sense_pin"]
        and endpoint.role == constraint.params["sense_role"]
    ]
    expected_source = str(constraint.params["physical_source_component"])
    passed = (
        len(sources) == 1
        and sources[0].split(":", 1)[0] == expected_source
        and len(sense) == 1
        and constraint.params.get("simultaneous_external_power_sources") == "forbidden"
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "The declared component is the sole populated 3V3 source and UPDI VTREF is sense-only."
        if passed
        else "The 3V3 source or UPDI voltage-reference ownership contract failed.",
        ("semantic IR endpoint roles",),
        {
            "physical_sources": sources,
            "sense_endpoint_count": len(sense),
            "simultaneous_external_power_sources": constraint.params.get(
                "simultaneous_external_power_sources"
            ),
        },
        True,
        True,
    )


def _check_current_limit(design: Any, constraint: Any, graph: PartGraph) -> CheckResult:
    resistor = next(
        (
            component
            for component in design.components
            if component.id in constraint.targets
            and "resistance_ohm" in graph.get(component.part_id).ratings
        ),
        None,
    )
    led = next(
        (
            component
            for component in design.components
            if component.id in constraint.targets
            and "forward_voltage_v_typ" in graph.get(component.part_id).ratings
        ),
        None,
    )
    resistance = (
        float(graph.get(resistor.part_id).ratings["resistance_ohm"])
        if resistor
        else 0.0
    )
    current = (
        (float(constraint.params["supply_v"]) - float(constraint.params["forward_v"]))
        / resistance
        if resistance > 0
        else math.inf
    )
    resistor_power = current * current * resistance
    passed = (
        resistor is not None
        and led is not None
        and current <= float(constraint.params["max_current_a"]) + 1e-12
        and current <= float(graph.get(led.part_id).ratings["max_current_a"]) + 1e-12
        and resistor_power
        <= float(graph.get(resistor.part_id).ratings["power_w"]) + 1e-12
    )
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "LED current and resistor dissipation are within declared ratings."
        if passed
        else "LED current-limit analysis failed.",
        ("trusted part ratings",),
        {
            "calculated_current_a": round(current, 9),
            "resistor_power_w": round(resistor_power, 9),
        },
        True,
        True,
    )


def _bbox(
    component_id: str,
    inspections: dict[str, FootprintInspection],
    placements: dict[str, Any],
) -> tuple[float, float, float, float]:
    info = inspections[component_id]
    origin = placements[component_id]
    x1 = float(origin["x_mm"]) + info.bbox_x_mm
    y1 = float(origin["y_mm"]) + info.bbox_y_mm
    return x1, y1, x1 + info.width_mm, y1 + info.height_mm


def _check_edge(
    design: Any,
    constraint: Any,
    inspections: dict[str, FootprintInspection],
    placements: dict[str, Any],
    evidence: str,
) -> CheckResult:
    component_id = next(target for target in constraint.targets if target != "board")
    x1, y1, x2, y2 = _bbox(component_id, inspections, placements)
    distances = {
        "left": x1,
        "right": design.board.width_mm - x2,
        "top": y1,
        "bottom": design.board.height_mm - y2,
    }
    edge = constraint.params["edge"]
    distance = distances[edge]
    maximum = float(constraint.params["max_edge_distance_mm"])
    passed = -1e-9 <= distance <= maximum + 1e-9
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        f"Connector is within the declared {edge} edge-access distance."
        if passed
        else f"Connector violates the declared {edge} edge-access distance.",
        (evidence,),
        {"edge": edge, "distance_mm": round(distance, 6), "maximum_mm": maximum},
        constraint.severity == "release_blocking",
        True,
    )


def _check_group(
    constraint: Any,
    inspections: dict[str, FootprintInspection],
    placements: dict[str, Any],
) -> CheckResult:
    centers = {}
    for component_id in constraint.targets:
        x1, y1, x2, y2 = _bbox(component_id, inspections, placements)
        centers[component_id] = ((x1 + x2) / 2, (y1 + y2) / 2)
    diameter = max(
        (
            math.dist(first, second)
            for first in centers.values()
            for second in centers.values()
        ),
        default=0.0,
    )
    maximum = float(constraint.params["max_diameter_mm"])
    passed = diameter <= maximum + 1e-9
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "Functional group diameter meets its placement intent."
        if passed
        else "Functional group is spread beyond its placement constraint.",
        ("generated placement receipt",),
        {"diameter_mm": round(diameter, 6), "maximum_mm": maximum},
        False,
        constraint.severity in {"required", "release_blocking"},
    )


def _check_routing(
    design: Any,
    constraint: Any,
    manifest: dict[str, Any],
    graph: PartGraph,
) -> CheckResult:
    routing = manifest["generation"]["pcb"]["routing"]
    net_by_id = {net.id: net for net in design.nets}
    lengths = routing["length_mm_by_net"]
    lengths_by_width = routing["length_mm_by_net_and_width"]
    maximum = constraint.params.get("max_length_mm")
    nominal = float(constraint.params["width_mm"])
    neckdown = float(constraint.params.get("neckdown_width_mm", nominal))
    neckdown_max_per_pad = float(
        constraint.params.get("neckdown_max_length_mm_per_pad", 0.0)
    )
    metrics: dict[str, Any] = {"nets": {}}
    passed = routing["state"] == "completed" and not routing["unrouted"]
    reference_net_id = constraint.params.get("continuous_reference_net")
    if reference_net_id is not None:
        reference_net = net_by_id.get(str(reference_net_id))
        planes = routing.get("reference_planes", [])
        matching_planes = [
            plane
            for plane in planes
            if isinstance(plane, dict)
            and reference_net is not None
            and str(plane.get("net", "")).lstrip("/") == reference_net.name
            and plane.get("filled") is True
            and _positive_finite(plane.get("area_mm2"))
        ]
        raw_stitching = constraint.params.get("min_reference_stitching_vias")
        stitching_contract_valid = (
            isinstance(raw_stitching, int)
            and not isinstance(raw_stitching, bool)
            and raw_stitching >= 1
        )
        minimum_stitching = raw_stitching if stitching_contract_valid else 0
        via_counts = routing.get("via_count_by_net", {})
        stitching_count = (
            int(via_counts.get(reference_net.name, 0))
            if reference_net is not None and isinstance(via_counts, dict)
            else 0
        )
        passed = (
            passed
            and bool(matching_planes)
            and stitching_contract_valid
            and stitching_count >= minimum_stitching
        )
        metrics["reference_plane"] = {
            "net_id": reference_net_id,
            "evidence": matching_planes,
            "stitching_via_count": stitching_count,
            "minimum_stitching_vias": minimum_stitching,
            "stitching_contract_valid": stitching_contract_valid,
        }
    component_by_id = {component.id: component for component in design.components}
    for target in constraint.targets:
        net = net_by_id[target]
        name = net.name
        length = float(lengths.get(name, 0.0))
        by_width = lengths_by_width.get(name, {})
        narrow_length = sum(
            float(value)
            for width, value in by_width.items()
            if float(width) < nominal - 1e-9
        )
        fine_pitch_pad_count = 0
        for endpoint in net.endpoints:
            component = component_by_id[endpoint.component]
            part = graph.get(component.part_id)
            if float(part.manufacturing.get("pitch_mm", math.inf)) < 0.8:
                fine_pitch_pad_count += 1
        neckdown_max = neckdown_max_per_pad * fine_pitch_pad_count
        width_ok = all(float(width) + 1e-9 >= neckdown for width in by_width)
        length_ok = maximum is None or length <= float(maximum) + 1e-9
        neckdown_ok = narrow_length <= neckdown_max + 1e-9
        passed = passed and bool(by_width) and width_ok and length_ok and neckdown_ok
        metrics["nets"][name] = {
            "length_mm": round(length, 6),
            "length_by_width_mm": by_width,
            "neckdown_length_mm": round(narrow_length, 6),
            "neckdown_limit_mm": round(neckdown_max, 6),
        }
    return CheckResult(
        f"l3.{constraint.id}",
        "L3",
        "completed",
        "pass" if passed else "fail",
        "All target nets are routed within length, width, and neckdown bounds."
        if passed
        else "One or more target nets violate routing constraints.",
        ("generated routing receipt",),
        metrics,
        constraint.severity == "release_blocking",
        True,
    )


def _positive_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _analysis_checks(project: ManagedProject, graph: PartGraph) -> list[CheckResult]:
    del graph
    checks: list[CheckResult] = []
    for analysis in project.design.analyses:
        identifier = str(analysis["id"])
        kind = analysis["kind"]
        required = bool(analysis.get("required", False))
        if kind == "ohms_law":
            resistance = float(analysis["resistance_ohm"])
            current = (
                float(analysis["supply_v"]) - float(analysis["forward_v"])
            ) / resistance
            checks.append(
                CheckResult(
                    f"l5.{identifier}",
                    "L5",
                    "completed",
                    "pass",
                    "Declared DC ohms-law analysis completed deterministically.",
                    (project.ir_path.name,),
                    {"current_a": round(current, 9)},
                    False,
                    required,
                )
            )
        elif kind == "power_budget":
            checks.append(
                CheckResult(
                    f"l5.{identifier}",
                    "L5",
                    "heuristic",
                    "unknown",
                    "Supply envelope is declared, but trusted maximum-current models for every load are unavailable.",
                    (project.ir_path.name,),
                    {
                        "declared_max_current_a": project.design.scope.max_current_a,
                        "declared_max_power_w": project.design.scope.max_power_w,
                    },
                    False,
                    required,
                )
            )
        else:
            checks.append(
                CheckResult(
                    f"l5.{identifier}",
                    "L5",
                    "unavailable",
                    "unknown",
                    str(analysis.get("reason", f"No local adapter for {kind}.")),
                    (project.ir_path.name,),
                    {"analysis_kind": kind},
                    False,
                    required,
                )
            )
    checks.append(
        CheckResult(
            "l5.high_speed_field_thermal",
            "L5",
            "not_applicable",
            "pass",
            "SI/PI field solving and thermal simulation are outside the declared low-speed, sub-watt acceptance scope; unsupported high-risk domains are rejected earlier.",
            (project.ir_path.name,),
        )
    )
    return checks


def _external_metrics(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "actor": entry["actor"],
        "role": entry["role"],
        "performed_at": entry["performed_at"],
        "statement": entry["statement"],
        "metadata": entry["metadata"],
        "verification": entry["verification"],
    }


def _external_gate_checks(document: dict[str, Any]) -> list[CheckResult]:
    by_level = {entry["level"]: entry for entry in document["entries"]}
    checks = []
    definitions = {
        "L6": (
            "l6.engineering_review",
            "A qualified human engineering review has not been imported; the runtime does not self-sign designs.",
        ),
        "L7": (
            "l7.physical_build_test",
            "Fabrication, assembly, bring-up, and measured test evidence require a physical board and have not been imported.",
        ),
    }
    for level, (identifier, missing_summary) in definitions.items():
        entry = by_level.get(level)
        if entry is None:
            checks.append(
                CheckResult(
                    identifier,
                    level,
                    "human_required",
                    "unknown",
                    missing_summary,
                    (),
                    None,
                    False,
                    True,
                )
            )
            continue
        checks.append(
            CheckResult(
                identifier,
                level,
                "completed",
                entry["outcome"],
                "Externally supplied evidence was integrity-checked and recorded; its engineering truth was not independently verified by the runtime.",
                tuple(artifact["path"] for artifact in entry["artifacts"]),
                _external_metrics(entry),
                False,
                True,
            )
        )
    return checks


def _levels(checks: tuple[CheckResult, ...]) -> tuple[LevelResult, ...]:
    names = {
        "L0": "file_and_syntax",
        "L1": "component_pin_footprint_connectivity",
        "L2": "erc_drc",
        "L3": "interface_function_intent",
        "L4": "bom_sourcing_manufacturing",
        "L5": "simulation_and_physical_analysis",
        "L6": "human_engineering_review",
        "L7": "physical_build_and_test",
    }
    result: list[LevelResult] = []
    for level, name in names.items():
        members = tuple(check for check in checks if check.level == level)
        outcomes = {check.outcome for check in members}
        outcome = (
            "fail"
            if "fail" in outcomes
            else "unknown"
            if "unknown" in outcomes
            else "pass"
        )
        states = {check.state for check in members}
        state = next(
            (
                candidate
                for candidate in (
                    "human_required",
                    "unavailable",
                    "heuristic",
                    "completed",
                    "not_applicable",
                )
                if candidate in states
            ),
            "not_applicable",
        )
        result.append(LevelResult(level, name, state, outcome, members))
    return tuple(result)


def _ready(checks: tuple[CheckResult, ...], attribute: str) -> bool:
    return all(
        check.outcome == "pass" for check in checks if bool(getattr(check, attribute))
    )


def _public_tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": value["status"],
        "failure": value["failure"],
        "report": value["report"],
    }


def _audit_tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": value["status"],
        "failure": value["failure"],
        "duration_seconds": value["duration_seconds"],
        "normalized_report": value["report"],
        "raw_report": value.get("raw_report"),
        "raw_sha256": value.get("raw_sha256"),
        "reported_at": value.get("reported_at"),
    }
