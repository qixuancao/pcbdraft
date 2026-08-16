"""Lower a validated circuit plan into semantic PCB IR and local part data.

This compiler owns deterministic plan-to-IR translation, local component
resolution, initial placement seeds, and compiler-time constraints. It does
not parse provider output or persist an application project.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pcbdraft.agent.part_resolver import LocalKiCadPartResolver
from pcbdraft.agent.plan import (
    AGENT_PLAN_VERSION,
    AgentDesignRequest,
    CircuitPlan,
    PlanComponent,
    _normal_key,
)
from pcbdraft.agent.review import AgentPlanReview, review_agent_plan
from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.component_qualification import (
    COMPONENT_QUALIFICATION_SCHEMA,
    qualify_components,
)
from pcbdraft.domain.ir import (
    BoardSpec,
    Component,
    Constraint,
    Design,
    FunctionalBlock,
    Net,
    Placement,
    Provenance,
    Requirement,
)
from pcbdraft.domain.parts import PART_CATALOG_SCHEMA, PartGraph, PartRecord
from pcbdraft.domain.spatial_contracts import anchored_rectangle


@dataclass(frozen=True)
class AgentCompilation:
    request: AgentDesignRequest
    plan: CircuitPlan
    design: Design
    graph: PartGraph
    review: AgentPlanReview


def _baseline_agent_constraints(
    request: AgentDesignRequest, plan: CircuitPlan
) -> tuple[Constraint, ...]:
    """Carry the reviewed board manufacturing envelope into the semantic IR.

    This is generic project metadata derived from the user's accepted request,
    not a hidden electrical block or a model-authored geometry instruction.
    """

    constraints = [
        Constraint(
            id="agent_manufacturing_rules",
            kind="manufacturing_rules",
            targets=("board",),
            params={
                "process_profile": "stock_kicad_prototype",
                "min_track_mm": request.board.min_track_mm,
                "min_clearance_mm": request.board.min_clearance_mm,
                "min_drill_mm": request.board.min_drill_mm,
                "edge_clearance_mm": request.board.edge_clearance_mm,
            },
            severity="release_blocking",
            rationale=(
                "Preserve the approved board-manufacturing envelope for deterministic checks."
            ),
            provenance=("user_request",),
        ),
    ]
    ground = next(net for net in plan.nets if net.name.lstrip("/").casefold() == "gnd")
    routing_params: dict[str, Any] = {
        "width_mm": max(request.board.min_track_mm, 0.25),
        "auto_route": True,
        "neckdown_width_mm": request.board.min_track_mm,
        "neckdown_max_length_mm_per_pad": 2.25,
    }
    if len(ground.endpoints) >= 2:
        routing_params.update(
            {
                "continuous_reference_net": ground.id,
                "min_reference_stitching_vias": 0,
                "reference_connection_policy": "ensure_connected",
            }
        )
    constraints.append(
        Constraint(
            id="agent_routing",
            kind="routing",
            targets=tuple(sorted(net.id for net in plan.nets)),
            params=routing_params,
            severity="required",
            rationale="Route declared nets using the reviewed board rules.",
            provenance=("user_request",),
        )
    )
    return tuple(constraints)


def planner_symbol_context(
    request: AgentDesignRequest,
    *,
    resolver: LocalKiCadPartResolver | None = None,
    limit_per_query: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    """Return actual local symbol candidates, grouped by user-facing query.

    The reserved ``_runtime_primitives`` group only contains universal KiCad
    primitives.  It is not a hidden design template: the planning provider still
    chooses whether they are needed and must declare every component and net.
    """
    resolved = resolver or LocalKiCadPartResolver()
    queries = tuple(dict.fromkeys((*request.requested_parts, *request.functions)))
    result: dict[str, list[dict[str, Any]]] = {}
    primitives: list[dict[str, Any]] = []
    for symbol, footprint in (
        ("Device:R", "Resistor_SMD:R_0603_1608Metric"),
        ("Device:C", "Capacitor_SMD:C_0603_1608Metric"),
        ("Device:LED", "LED_SMD:LED_0603_1608Metric"),
        ("power:GND", None),
        ("power:+3V3", None),
        (
            "Connector_Generic:Conn_01x02",
            "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
        ),
    ):
        try:
            primitive = resolved.describe(symbol).to_dict()
            if footprint is not None:
                primitive["footprint"] = footprint
            primitives.append(primitive)
        except ValidationError:
            # A partial KiCad installation should not turn a normal user request
            # into a fictional plan.  The planner receives the missing-library
            # evidence with the query-specific candidates below.
            continue
    result["_runtime_primitives"] = primitives
    for query in queries[:64]:
        try:
            candidates = resolved.find(query, limit=limit_per_query)
        except ValidationError as exc:
            result[query] = [{"error": str(exc)}]
            continue
        result[query] = [candidate.to_dict() for candidate in candidates]
    return result


def compile_agent_plan(
    request: AgentDesignRequest,
    plan_value: CircuitPlan | Mapping[str, Any],
    *,
    resolver: LocalKiCadPartResolver | None = None,
    base_graph: PartGraph | None = None,
) -> AgentCompilation:
    """Resolve a generic plan into semantic IR and a project-local part graph."""
    plan = (
        plan_value
        if isinstance(plan_value, CircuitPlan)
        else CircuitPlan.from_dict(plan_value)
    )
    if plan.design_id != request.design_id:
        raise ValidationError(
            "circuit plan design_id does not match the approved request"
        )
    resolved = resolver or LocalKiCadPartResolver()
    parts_by_component: dict[str, PartRecord] = {}
    for component in plan.components:
        parts_by_component[component.id] = resolved.resolve(component)
    _assert_requested_part_identity(request, plan)
    base = base_graph or PartGraph(
        (),
        license_id="local-stock-kicad-libraries",
        source="installed_stock_kicad",
    )
    graph = base.merged(parts_by_component.values(), source="project_local_kicad")
    placements = _seed_placements(plan.components, request.board)
    if plan.version == AGENT_PLAN_VERSION:
        component_blocks = {
            component_id: block.id
            for block in plan.blocks
            for component_id in block.components
        }
        ir_blocks = tuple(
            FunctionalBlock(
                id=block.id,
                kind=block.kind,
                name=block.name,
                version="agent_plan_v2",
                intent=block.intent,
                components=block.components,
                provenance=("agent_plan",),
            )
            for block in plan.blocks
        )
    else:
        component_blocks = {
            component.id: f"block.{component.id}" for component in plan.components
        }
        ir_blocks = tuple(
            FunctionalBlock(
                id=f"block.{item.id}",
                kind="agent_component",
                name=item.reference,
                version="agent_plan_v1",
                intent=f"{item.role}: {item.value}",
                components=(item.id,),
                provenance=("agent_plan",),
            )
            for item in plan.components
        )
    ir_components = tuple(
        Component(
            id=item.id,
            reference=item.reference,
            part_id=parts_by_component[item.id].id,
            value=item.value,
            block_id=component_blocks[item.id],
            placement=placements[item.id],
            attributes={
                "planner_role": item.role,
                "exclude_from_board": not item.on_board,
                "source_symbol": item.symbol,
            },
        )
        for item in plan.components
    )
    provenance = (
        Provenance(
            id="user_request",
            kind="user_requirement",
            source="PCBDraft conversation",
            locator=str(request.source.get("locator", "conversation")),
            acquired_at=request.source.get("date")
            if isinstance(request.source.get("date"), str)
            else None,
            method="user_supplied",
            confidence=1.0,
            notes="Sanitized user request retained in the project record.",
        ),
        Provenance(
            id="agent_plan",
            kind="agent_plan",
            source="Configured planning provider",
            locator="circuit-plan.json",
            acquired_at=None,
            method="structured_output",
            confidence=0.5,
            notes="Untrusted plan constrained to local symbols and semantic topology.",
        ),
    )
    design = Design(
        design_id=request.design_id,
        name=request.name,
        revision=request.revision,
        scope=request.scope,
        requirements=(
            Requirement(
                id="user_board_request",
                text=request.request_summary,
                acceptance=(
                    "Generate a reviewable KiCad schematic and PCB attempt from the approved semantic plan.",
                ),
                risk="unknown",
                provenance=("user_request",),
            ),
        ),
        provenance=provenance,
        blocks=ir_blocks,
        power_domains=tuple(domain.to_ir() for domain in plan.power_domains),
        interfaces=tuple(interface.to_ir() for interface in plan.interfaces),
        components=ir_components,
        nets=tuple(
            Net(
                id=item.id,
                name=item.name,
                endpoints=item.endpoints,
                net_class=item.net_class,
                power_domain=item.power_domain,
                interface=item.interface,
                intent=item.intent,
            )
            for item in plan.nets
        ),
        constraints=(
            *_baseline_agent_constraints(request, plan),
            *(constraint.to_ir() for constraint in plan.constraints),
            *(assertion.to_ir() for assertion in plan.assertions),
        ),
        board=request.board,
        analyses=(
            {
                "id": "agent_plan",
                "kind": "agent_plan",
                "state": "provisional",
                "required": False,
                "reason": "No functional simulation was performed for the generated circuit plan.",
                "summary": plan.summary,
                "notes": list(plan.notes),
            },
        ),
        metadata={
            "generator": f"agent_plan_v{plan.version}",
            "assurance": "provisional",
            "requirements_hash": hashlib.sha256(request.canonical_bytes()).hexdigest(),
            "plan_hash": hashlib.sha256(plan.canonical_bytes()).hexdigest(),
            "requested_parts": list(request.requested_parts),
            "planner_assumptions": sorted({*request.assumptions, *plan.assumptions}),
            "part_catalog_schema": PART_CATALOG_SCHEMA,
            **(
                {
                    "block_hierarchy": [
                        {"id": block.id, "parent": block.parent}
                        for block in plan.blocks
                    ]
                }
                if plan.version == AGENT_PLAN_VERSION
                else {}
            ),
        },
    )
    routed_widths: dict[str, tuple[float, str]] = {}
    differential_membership: dict[str, str] = {}
    for constraint in plan.constraints:
        if constraint.kind in {"routing", "differential_pair"}:
            width = float(constraint.parameters["width_mm"])
            for target in constraint.targets:
                previous = routed_widths.get(target)
                if previous is not None and not math.isclose(
                    previous[0], width, rel_tol=0, abs_tol=1e-12
                ):
                    raise ValidationError(
                        f"net {target} has conflicting route widths in constraints "
                        f"{previous[1]} and {constraint.id}"
                    )
                routed_widths[target] = (width, constraint.id)
        if constraint.kind == "differential_pair":
            for target in constraint.targets:
                previous_pair = differential_membership.get(target)
                if previous_pair is not None:
                    raise ValidationError(
                        f"net {target} belongs to multiple differential pairs: "
                        f"{previous_pair} and {constraint.id}"
                    )
                differential_membership[target] = constraint.id
        if (
            constraint.kind in {"routing", "differential_pair"}
            and float(constraint.parameters["width_mm"]) < request.board.min_track_mm
        ):
            raise ValidationError(
                f"routing constraint {constraint.id} width is below the board minimum"
            )
        if (
            constraint.kind == "differential_pair"
            and float(constraint.parameters["gap_mm"]) < request.board.min_clearance_mm
        ):
            raise ValidationError(
                f"differential-pair constraint {constraint.id} gap is below the board minimum"
            )
        if constraint.kind == "board_keepout":
            try:
                anchored_rectangle(
                    str(constraint.parameters["anchor"]),
                    float(constraint.parameters["width_mm"]),
                    float(constraint.parameters["height_mm"]),
                    request.board.width_mm,
                    request.board.height_mm,
                    request.board.edge_clearance_mm,
                )
            except ValueError as exc:
                raise ValidationError(
                    f"board keepout {constraint.id} does not fit the board"
                ) from exc
    design.assert_valid()
    graph.assert_design(design, check_libraries=True, allow_provisional=True)
    qualification = qualify_components(design, graph)
    if qualification.pad_mapping_failures:
        raise ValidationError(
            "component qualification failed: part.footprint_pad_mapping: "
            + ", ".join(qualification.pad_mapping_failures)
        )
    design = replace(
        design,
        metadata={
            **design.metadata,
            "component_qualification_schema": COMPONENT_QUALIFICATION_SCHEMA,
            "component_qualification_hash": qualification.sha256(),
        },
    )
    design.assert_valid()
    review = review_agent_plan(request, plan, design, graph)
    return AgentCompilation(
        request=request,
        plan=plan,
        design=design,
        graph=graph,
        review=review,
    )


def _assert_requested_part_identity(
    request: AgentDesignRequest, plan: CircuitPlan
) -> None:
    for requested in request.requested_parts:
        needle = _normal_key(requested)
        if not needle:
            continue
        candidates = (
            _normal_key(component.exact_name or "")
            + " "
            + _normal_key(component.symbol.split(":", 1)[1])
            + " "
            + _normal_key(component.mpn or "")
            for component in plan.components
        )
        if not any(needle in candidate for candidate in candidates):
            raise ValidationError(
                "circuit plan did not preserve explicitly requested part identity: "
                + requested
            )


def _seed_placements(
    components: Iterable[PlanComponent], board: BoardSpec
) -> dict[str, Placement]:
    entries = tuple(components)
    count = len(entries)
    columns = max(1, min(8, int(count**0.5 + 0.999)))
    rows = max(1, (count + columns - 1) // columns)
    result: dict[str, Placement] = {}
    for index, component in enumerate(entries):
        column = index % columns
        row = index // columns
        # These are only deterministic solver seeds, not LLM-authored geometry.
        x = board.width_mm * (column + 1) / (columns + 1)
        y = board.height_mm * (row + 1) / (rows + 1)
        result[component.id] = Placement(
            x_mm=round(x, 6),
            y_mm=round(y, 6),
            rotation_deg=0.0,
            side="front",
            fixed=False,
        )
    return result
