"""Layered L0-L7 validation with explicit, honest evidence states."""

from __future__ import annotations

import math
import secrets
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PcbAgentError, ValidationError
from .external_evidence import load_external_evidence
from .gates import GATE_JSON_LIMIT, count_severities
from .io import atomic_write_json, load_json_limited, make_directory
from .kicad_pcb import FootprintInspection, inspect_footprints
from .locking import ResourceLock
from .managed import ManagedProject, open_managed_project
from .parts import PartGraph
from .process import run_command
from .project import sha256_file
from .runs import utc_timestamp
from .semantic_rules import evaluate_semantic_rules

VALIDATION_SCHEMA = "pcb-agent-validation"
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
        "schema": "pcb-agent-validation-receipt",
        "version": 1,
        "status": "running",
        "started_at": utc_timestamp(),
        "project": str(project.root),
        "design_content_hash": project.design.content_hash(),
    }
    atomic_write_json(output_dir / "receipt.json", receipt)
    try:
        lock = (
            nullcontext()
            if _already_locked
            else ResourceLock(
                project.root,
                project.root.parent / ".pcb-agent-locks",
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
                "created_at": utc_timestamp(),
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
            str(output),
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
            str(output),
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
    except PcbAgentError as exc:
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
            load_json_limited(output, GATE_JSON_LIMIT) if failure is None else None
        )
    except PcbAgentError:
        failure = "missing_or_invalid_json"
        document = None
    return {
        "status": "completed" if failure is None else "unavailable",
        "failure": failure,
        "duration_seconds": result.duration_seconds,
        "report": output.name,
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
    checks.append(
        CheckResult(
            "l4.live_sourcing",
            "L4",
            "unavailable",
            "unknown",
            "Live authorized-distributor stock and price were not queried; no current availability claim is made.",
            ("bundled part catalog sourcing.stock_status=not_checked",),
            None,
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
    checks.extend(_external_gate_checks(project))
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
        if constraint.kind == "decoupling":
            checks.append(
                _check_decoupling(
                    design, constraint, graph, positions, project.ir_path.name
                )
            )
        elif constraint.kind == "interface_pullups":
            checks.append(_check_pullups(design, constraint, graph))
        elif constraint.kind == "current_limit":
            checks.append(_check_current_limit(design, constraint, graph))
        elif constraint.kind == "edge_placement":
            checks.append(
                _check_edge(
                    design, constraint, inspections, placements, project.board_path.name
                )
            )
        elif constraint.kind == "functional_group":
            checks.append(_check_group(constraint, inspections, placements))
        elif constraint.kind == "routing":
            checks.append(_check_routing(design, constraint, project.manifest, graph))
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
    capacitance = (
        float(graph.get(capacitor.part_id).ratings.get("capacitance_f", 0))
        if capacitor is not None
        else 0.0
    )
    passed = (
        len(distances) == len(nets)
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


def _external_gate_checks(project: ManagedProject) -> list[CheckResult]:
    document = load_external_evidence(project)
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
                {
                    "actor": entry["actor"],
                    "role": entry["role"],
                    "performed_at": entry["performed_at"],
                    "statement": entry["statement"],
                    "metadata": entry["metadata"],
                    "verification": entry["verification"],
                },
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
        "duration_seconds": value["duration_seconds"],
        "report": value["report"],
    }
