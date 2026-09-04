"""Authoritative project and conversation service shared by terminal and web UIs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.agent.compiler import compile_agent_plan, planner_symbol_context
from pcbdraft.agent.plan import (
    AgentDesignRequest,
    CircuitPlan,
)
from pcbdraft.agent.repair import (
    normalize_repair_feedback,
    user_revision_feedback,
    validation_feedback_from_levels,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import (
    atomic_write_json,
    load_json_limited,
    make_directory,
)
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.repository import (
    ProjectRepository,
    configure_repository,
    current_repository,
    explicit_repository,
)
from pcbdraft.core.runs import new_run_id, utc_timestamp
from pcbdraft.domain.component_qualification import (
    COMPONENT_QUALIFICATION_SCHEMA,
    qualify_components,
)
from pcbdraft.domain.ir import BoardSpec, Design, Scope, canonical_json_bytes
from pcbdraft.domain.operations import (
    ChangeSet,
    ConnectGroupEntry,
    PlaceGroupEntry,
    apply_change_set,
    parse_connect_group,
    parse_place_group,
    semantic_diff,
)
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.scope import evaluate_scope
from pcbdraft.kicad.consistency import (
    NATIVE_OPERATION_POLICIES,
    NativeBoardProjection,
    NativeConsistencyReport,
    NativeMismatch,
    NativeOperationDeltaReport,
    NativeSchematicProjection,
    compare_native_consistency,
    compare_native_operation_delta,
    inspect_native_consistency,
)
from pcbdraft.kicad.pcb import inspect_native_board
from pcbdraft.kicad.previews import generate_preview, generate_previews
from pcbdraft.kicad.routing import (
    ROUTING_FAILURE_CODES,
    RoutingFailure,
    RoutingFailureError,
)
from pcbdraft.kicad.schematic import inspect_native_schematic
from pcbdraft.kicad.sync import apply_kicad_import, preview_kicad_import
from pcbdraft.model.providers import (
    MAX_USER_MESSAGE_BYTES,
    IntentProvider,
    ProviderContext,
    resolve_provider,
)
from pcbdraft.services.doctor import doctor_report
from pcbdraft.services.managed import (
    IR_NAME,
    EmptyDesignRequest,
    load_generation_request,
    materialize_managed_design,
    open_managed_project,
)
from pcbdraft.services.progress import (
    DEFAULT_CONVERGENCE_POLICY,
    ConvergenceDecision,
    ConvergenceObservation,
    EngineeringStage,
    EvidenceCheck,
    EvidenceStatus,
    MetricValue,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    ProgressClassification,
    ProgressVector,
    StageEvidence,
    StageProjection,
    compare_progress,
    derive_stage,
    evaluate_convergence,
    product_terminal_receipt_id,
    store_product_session_terminal,
    terminal_outcome,
    validate_product_terminal_receipt_id,
)
from pcbdraft.verification.gates import (
    GATE_JSON_LIMIT,
    count_severities,
    structured_violations,
)
from pcbdraft.verification.release import (
    build_manufacturing_release,
    export_manufacturing_output,
    verify_manufacturing_release,
)
from pcbdraft.verification.validation import (
    run_individual_check,
    validate_managed_project,
)

APP_PROJECT_SCHEMA = "pcbdraft-application-project"
APP_PROJECT_VERSION = 1
CONVERSATION_SCHEMA = "pcbdraft-conversation-record"
CONVERSATION_VERSION = 1
ATTEMPT_SCHEMA = "pcbdraft-generation-attempt"
ATTEMPT_VERSION = 2
_ATTEMPT_FIELDS = {
    "schema",
    "version",
    "id",
    "status",
    "phase",
    "runtime",
    "assurance",
    "started_at",
    "completed_at",
    "part_ids",
    "requested_parts",
    "files",
    "error",
}
APP_FILE_LIMIT = 4 * 1024 * 1024
TRANSACTION_INSPECTION_FILE_LIMIT = 256 * 1024
TRANSACTION_INSPECTION_ITEM_LIMIT = 16
TRANSACTION_INSPECTION_DEPTH_LIMIT = 8
PENDING_REQUEST_NAME = "pending-agent-request.json"
PENDING_PLAN_NAME = "pending-circuit-plan.json"
PENDING_DESIGN_NAME = "pending-design.pcbir.json"
PENDING_PARTS_NAME = "pending-parts.pcbdraft.json"
MAX_MESSAGES = 2_000
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_TRANSIENT_STATES = {
    "interpreting",
    "generating",
    "repairing",
    "validating",
    "releasing",
    "applying_change",
    "importing_external",
}
_STATE_FIELDS = {
    "schema",
    "version",
    "id",
    "name",
    "created_at",
    "updated_at",
    "status",
    "provider",
    "revision",
    "design_revision",
    "event_sequence",
    "active_transaction",
    "last_transaction",
    "last_validation",
    "last_preview",
    "last_release",
}
_CONVERSATION_FIELDS = {
    "schema",
    "version",
    "messages",
    "proposal",
    "decisions",
}
_NATIVE_DELTA_OPERATIONS = frozenset(NATIVE_OPERATION_POLICIES) - {
    "register_kicad_part"
}


def _transaction_inspection_depth_is_valid(value: object, *, depth: int = 0) -> bool:
    """Reject pathological receipt nesting before projecting explicit detail."""

    if depth > TRANSACTION_INSPECTION_DEPTH_LIMIT:
        return False
    if isinstance(value, Mapping):
        return all(
            _transaction_inspection_depth_is_valid(item, depth=depth + 1)
            for item in value.values()
        )
    if isinstance(value, list):
        return all(
            _transaction_inspection_depth_is_valid(item, depth=depth + 1)
            for item in value
        )
    return value is None or isinstance(value, (bool, int, float, str))


class _PCBOperationPostconditionError(ValidationError):
    """Internal expected failure carrying a stable transaction error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        routing_failure: RoutingFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.routing_failure = routing_failure


def _bind_transaction_failure(exc: BaseException, transaction_id: str) -> None:
    """Attach only an opaque retained receipt identity to an expected failure."""

    if isinstance(exc, PCBDraftError):
        exc.transaction_id = transaction_id  # type: ignore[attr-defined]


def _native_postconditions(
    tool_name: str, report: NativeConsistencyReport
) -> list[dict[str, Any]]:
    codes = {item.code for item in report.mismatches}
    conditions: list[dict[str, Any]] = [
        {
            "name": "native_consistency",
            "passed": report.consistency_passed,
            "mismatch_count": len(report.mismatches),
        }
    ]
    if tool_name == "route_net":
        conditions.extend(
            (
                {
                    "name": "native_nonzero_copper",
                    "passed": "native_zero_copper" not in codes,
                },
                {
                    "name": "native_endpoint_connectivity",
                    "passed": "native_connectivity_failed" not in codes,
                },
                {
                    "name": "native_net_isolation",
                    "passed": not codes
                    & {"unintended_net_merge", "unintended_board_net_merge"},
                },
            )
        )
    return conditions


def _routing_failure_context(design: Design, net_id: str | None) -> tuple[str, ...]:
    net = next((item for item in design.nets if item.id == net_id), None)
    if net is None:
        return (f"layers=board:{design.board.layers}", "order=unknown")
    components = {item.id: item for item in design.components}
    placements: list[str] = []
    for component_id in sorted({item.component for item in net.endpoints})[:8]:
        component = components.get(component_id)
        placement = component.placement if component is not None else None
        placements.append(
            f"{component_id}@"
            + (
                f"{placement.x_mm:.9g},{placement.y_mm:.9g}/{placement.rotation_deg:.9g}/{placement.side}"
                if placement is not None
                else "unplaced"
            )
        )
    order = tuple(item.id for item in sorted(design.nets, key=lambda item: item.id))
    return (
        *("placement=" + item for item in placements),
        f"layers=board:{design.board.layers}",
        f"order={order.index(net.id)}/{len(order)}",
    )


def _consistency_rejection(
    tool_name: str,
    net_id: str | None,
    report: NativeConsistencyReport,
    *,
    design: Design | None = None,
    state_revision: int = 0,
) -> _PCBOperationPostconditionError:
    codes = {item.code for item in report.mismatches}
    shown = ", ".join(sorted(codes)[:4]) or "unknown mismatch"
    if tool_name != "route_net":
        return _PCBOperationPostconditionError(
            "native_consistency_failed",
            f"native KiCad consistency postcondition failed: {shown}",
        )
    if codes & {"unintended_net_merge", "unintended_board_net_merge"}:
        error_code = "unintended_net_merge"
    elif codes & {"native_connectivity_failed", "native_zero_copper"}:
        error_code = "native_connectivity_failed"
    else:
        error_code = "native_commit_failed"
    failure = RoutingFailure(
        code=error_code,
        net=net_id or "unknown",
        expanded_nodes=0,
        blocking_summary=f"native postcondition mismatch: {shown}",
        recommendations=("inspect_native_artifact",),
        nearest_obstacle_class="native_artifact",
        state_revision=state_revision,
        state_context=(
            _routing_failure_context(design, net_id) if design is not None else ()
        ),
    )
    return _PCBOperationPostconditionError(
        error_code,
        failure.diagnostic,
        routing_failure=failure,
    )


def _unavailable_consistency_report(candidate_revision: int) -> NativeConsistencyReport:
    return NativeConsistencyReport(
        candidate_revision,
        "unknown",
        "unknown",
        "not_evaluated",
        (
            NativeMismatch(
                "board_projection_unknown",
                "board",
                "connectivity",
                "evaluated",
                "native inspection failed",
            ),
            NativeMismatch(
                "schematic_projection_unknown",
                "schematic",
                "connectivity",
                "evaluated",
                "native inspection failed",
            ),
        ),
    )


def _native_delta_postconditions(
    report: NativeOperationDeltaReport,
) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "passed": item.passed,
            "expected": item.expected,
            "observed": item.observed,
        }
        for item in report.checks
    ]


def _native_board_projection(managed: Any) -> NativeBoardProjection:
    snapshots = managed.manifest.get("native_snapshots")
    if not isinstance(snapshots, dict) or "board" not in snapshots:
        raise ValidationError("managed project lacks a native board snapshot")
    snapshot = snapshots["board"]
    projection = NativeBoardProjection.from_snapshot(snapshot)
    if _native_board_projection_complete(snapshot, projection):
        return projection
    refreshed_snapshot = inspect_native_board(
        managed.design,
        managed.board_path,
        include_connectivity=True,
    )
    refreshed = NativeBoardProjection.from_snapshot(refreshed_snapshot)
    if not _native_board_projection_complete(refreshed_snapshot, refreshed):
        raise ValidationError(
            "native board reinspection lacks operation-delta evidence"
        )
    return refreshed


def _native_board_projection_complete(
    snapshot: Any,
    projection: NativeBoardProjection,
) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    pose_references = {item.reference for item in projection.footprint_poses}
    complete_poses = len(projection.footprint_poses) == len(
        projection.components
    ) and pose_references == set(projection.components)
    required_board_rules = {
        "layers",
        "thickness_mm",
        "min_clearance_mm",
        "min_track_mm",
        "min_drill_mm",
        "edge_clearance_mm",
    }
    complete_board_rules = required_board_rules <= {
        key for key, _value in projection.board_rules
    }
    detailed_copper = all(
        item.geometry for item in projection.copper if item.kind in {"segment", "via"}
    )
    complete_components = (
        len(projection.component_artifacts) == len(projection.components)
        and {item.reference for item in projection.component_artifacts}
        == set(projection.components)
        and all(item.part_id is not None for item in projection.component_artifacts)
    )
    complete_nets = isinstance(snapshot, Mapping) and isinstance(
        snapshot.get("nets"), list
    )
    tracks = snapshot.get("tracks")
    complete_tracks = isinstance(tracks, list) and all(
        isinstance(item, Mapping)
        and (
            (
                item.get("kind") == "segment"
                and "width_mm" in item
                and ("layer_index" in item or "layer" in item)
            )
            or (
                item.get("kind") == "via"
                and {
                    "x_mm",
                    "y_mm",
                    "width_mm",
                    "drill_mm",
                    "from_layer",
                    "to_layer",
                }
                <= set(item)
            )
        )
        for item in tracks
    )
    zones = snapshot.get("zones")
    complete_zones = isinstance(zones, list) and all(
        isinstance(item, Mapping) and isinstance(item.get("pad_connection"), str)
        for item in zones
    )
    return (
        projection.status == "evaluated"
        and complete_poses
        and complete_board_rules
        and complete_components
        and complete_nets
        and complete_tracks
        and complete_zones
        and len(projection.outline) == 4
        and detailed_copper
    )


def _native_schematic_projection(managed: Any) -> NativeSchematicProjection:
    snapshots = managed.manifest.get("native_snapshots")
    if not isinstance(snapshots, dict) or "schematic" not in snapshots:
        raise ValidationError("managed project lacks a native schematic snapshot")
    projection = NativeSchematicProjection.from_snapshot(snapshots["schematic"])
    if _native_schematic_projection_complete(projection):
        return projection
    refreshed = NativeSchematicProjection.from_snapshot(
        inspect_native_schematic(managed.schematic_path, include_connectivity=True)
    )
    if not _native_schematic_projection_complete(refreshed):
        raise ValidationError(
            "native schematic reinspection lacks operation-delta evidence"
        )
    return refreshed


def _native_schematic_projection_complete(
    projection: NativeSchematicProjection,
) -> bool:
    return (
        projection.status == "evaluated"
        and len(projection.component_artifacts) == len(projection.components)
        and {item.reference for item in projection.component_artifacts}
        == set(projection.components)
        and all(item.part_id is not None for item in projection.component_artifacts)
    )


def _routed_component_nets(design: Design, component_id: str) -> tuple[str, ...]:
    connected = {
        net.id
        for net in design.nets
        if any(endpoint.component == component_id for endpoint in net.endpoints)
    }
    retained = {route.net for route in design.native_intent.routes}
    retained.update(via.net for via in design.native_intent.vias)
    return tuple(sorted(connected & retained))


def _reject_stale_copper_transform(
    tool_name: str,
    arguments: Mapping[str, Any],
    before: Design,
    candidate: Design,
    *,
    before_graph: PartGraph,
    candidate_graph: PartGraph,
) -> None:
    transform_operations = {
        "place_footprint",
        "place_group",
        "move_footprint",
        "rotate_footprint",
        "unplace_footprint",
    }
    component_contract_operations = {"assign_footprint", "update_component"}
    if tool_name not in transform_operations | component_contract_operations:
        return
    component_ids = (
        tuple(
            entry.component_id for entry in parse_place_group(arguments["placements"])
        )
        if tool_name == "place_group"
        else (str(arguments["component_id"]),)
    )
    for component_id in component_ids:
        before_component = next(
            (item for item in before.components if item.id == component_id), None
        )
        after_component = next(
            (item for item in candidate.components if item.id == component_id), None
        )
        if before_component is None or after_component is None:
            continue
        if tool_name in transform_operations:
            changed = before_component.placement != after_component.placement
        else:
            changed = _component_footprint_contract(
                before_component,
                before_graph,
            ) != _component_footprint_contract(after_component, candidate_graph)
        if not changed:
            continue
        routed_nets = _routed_component_nets(before, component_id)
        if routed_nets:
            raise _PCBOperationPostconditionError(
                "routed_footprint_transform_unsupported",
                "footprint geometry change is blocked until associated retained copper is unrouted: "
                + ", ".join(routed_nets),
            )


def _component_footprint_contract(component: Any, graph: PartGraph) -> object:
    if component.attributes.get("exclude_from_board", False):
        return None
    part = graph.get(component.part_id)
    if part.footprint is None:
        return None
    return (
        part.footprint,
        tuple((pin.number, pin.footprint_pad) for pin in part.pins),
    )


def _operation_failure_code(exc: BaseException, *, stage: str, tool_name: str) -> str:
    if isinstance(exc, RoutingFailureError):
        return exc.failure.code
    if isinstance(exc, _PCBOperationPostconditionError):
        return exc.error_code
    if stage in {"routing_probe", "routing_commit", "native_consistency"} and (
        tool_name == "route_net"
    ):
        return "native_commit_failed"
    if stage == "native_consistency":
        return "native_verification_failed"
    if stage == "native_delta":
        return (
            "native_commit_failed"
            if tool_name == "route_net"
            else "native_delta_failed"
        )
    if stage == "drc":
        return "drc_unavailable"
    if stage == "publication":
        return "publication_failed"
    return "native_materialization_failed"


_FATAL_DRC_MARKERS = (
    "short",
    "clearance",
    "board_edge",
    "copper_edge",
    "hole",
    "courtyard_overlap",
)


def _fatal_drc_count(document: Any) -> int:
    """Count fatal DRC classes from the full native report, never its display cap."""

    total = 0

    def visit(value: Any) -> None:
        nonlocal total
        if isinstance(value, Mapping):
            severity = value.get("severity")
            violation_type = value.get("type")
            if (
                isinstance(severity, str)
                and severity.lower() == "error"
                and isinstance(violation_type, str)
                and any(
                    marker in violation_type.lower() for marker in _FATAL_DRC_MARKERS
                )
            ):
                total += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return total


def _progress_vector(
    design: Design,
    graph: PartGraph,
    source_revision: int,
    *,
    consistency: NativeConsistencyReport | None,
    board: NativeBoardProjection | None,
    fatal_drc: MetricValue | None = None,
    error_drc: MetricValue | None = None,
    erc_error: MetricValue | None = None,
    routing_failure_count: int = 0,
) -> ProgressVector:
    consistency_known = bool(
        consistency is not None
        and consistency.schematic_status == "evaluated"
        and consistency.board_status == "evaluated"
    )
    mismatch = (
        MetricValue.known(len(consistency.mismatches), source_revision)
        if consistency_known and consistency is not None
        else MetricValue.unknown(source_revision)
    )
    unresolved = (
        MetricValue.known(board.unconnected_count, source_revision)
        if board is not None
        and board.status == "evaluated"
        and board.unconnected_count is not None
        else MetricValue.unknown(source_revision)
    )
    unplaced = sum(
        component.placement is None
        for component in design.components
        if graph.get(component.part_id).footprint is not None
        and not component.attributes.get("exclude_from_board", False)
    )
    return ProgressVector(
        source_revision,
        mismatch,
        unresolved,
        (fatal_drc or MetricValue.unknown(source_revision)).for_revision(
            source_revision
        ),
        (error_drc or MetricValue.unknown(source_revision)).for_revision(
            source_revision
        ),
        (erc_error or MetricValue.unknown(source_revision)).for_revision(
            source_revision
        ),
        MetricValue.known(unplaced, source_revision),
        MetricValue.known(routing_failure_count, source_revision),
    )


def _progress_stage_evidence(
    design: Design,
    source_revision: int,
    *,
    requirements_frozen: bool,
    consistency: NativeConsistencyReport | None,
    progress: ProgressVector,
    erc_check: EvidenceCheck,
    drc_check: EvidenceCheck,
) -> StageEvidence:
    current = lambda passed: EvidenceCheck.known(passed, source_revision)
    consistency_current = bool(
        consistency is not None
        and consistency.candidate_revision == source_revision
        and consistency.schematic_status == "evaluated"
        and consistency.board_status == "evaluated"
    )
    schematic_clean = consistency_current and not any(
        item.scope == "schematic"
        for item in consistency.mismatches  # type: ignore[union-attr]
    )
    native_clean = consistency_current and consistency.consistency_passed  # type: ignore[union-attr]
    unresolved = progress.unresolved_connection_count
    routing_failures = progress.routing_failure_count
    routing_started = (
        bool(design.native_intent.routes or design.native_intent.vias)
        or (unresolved.is_current(source_revision) and unresolved.value == 0)
        or (
            routing_failures.is_current(source_revision)
            and bool(routing_failures.value)
        )
    )
    native_connected = bool(
        native_clean
        and unresolved.is_current(source_revision)
        and unresolved.value == 0
    )
    return StageEvidence(
        source_revision,
        current(requirements_frozen),
        current(not design.issues()),
        current(schematic_clean),
        current(native_clean),
        current(routing_started),
        current(native_connected),
        erc_check,
        drc_check,
    )


def _attach_progress(
    receipt: dict[str, Any],
    before: ProgressVector,
    after: ProgressVector,
    before_stage: StageProjection,
    after_stage: StageProjection,
) -> None:
    delta = compare_progress(before, after)
    receipt["progress_before"] = before.to_dict()
    receipt["progress_after"] = after.to_dict()
    receipt["progress_delta"] = delta.to_dict()
    receipt["stage_before"] = before_stage.to_dict()
    receipt["stage_after"] = after_stage.to_dict()


def _transaction_progress_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Expose factual legacy-transaction progress to its durable caller."""

    fields = (
        "version",
        "status",
        "progress_before",
        "progress_after",
        "progress_delta",
        "stage_before",
        "stage_after",
        "candidate_progress",
        "candidate_stage",
        "convergence_classification",
        "publication",
        "application",
        "application_progress",
        "undo",
        "undo_progress",
    )
    return {key: copy.deepcopy(receipt[key]) for key in fields if key in receipt}


def _route_state_key(design: Design, net_id: str, design_revision: int) -> str:
    return "|".join(
        (f"revision={design_revision}", *_routing_failure_context(design, net_id))
    )


def _route_state_record(
    design: Design, net_id: str, design_revision: int
) -> dict[str, Any]:
    """Return the structured facts whose changes make a route retry distinct."""

    return {
        "design_revision": design_revision,
        "net_id": net_id,
        "context": list(_routing_failure_context(design, net_id)),
    }


def _route_state_key_from_record(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {
        "design_revision",
        "net_id",
        "context",
    }:
        raise ValidationError("route convergence state is malformed")
    revision = value["design_revision"]
    net_id = value["net_id"]
    context = value["context"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(net_id, str)
        or not net_id
        or not isinstance(context, list)
        or not all(isinstance(item, str) and item for item in context)
    ):
        raise ValidationError("route convergence state is malformed")
    return "|".join((f"revision={revision}", *context))


def _routing_failure_retry_key(value: object) -> str:
    """Validate retained structured Router evidence and recompute its retry key."""

    if not isinstance(value, Mapping):
        raise ValidationError("route convergence failure is malformed")
    try:
        blocking_region_value = value.get("blocking_region")
        blocking_region = (
            tuple(float(item) for item in blocking_region_value)
            if isinstance(blocking_region_value, list)
            else None
        )
        failure = RoutingFailure(
            code=str(value["code"]),
            net=str(value["net"]),
            endpoints=tuple(str(item) for item in value["endpoints"]),
            expanded_nodes=value["expanded_nodes"],
            blocking_summary=str(value["blocking_summary"]),
            recommendations=tuple(str(item) for item in value["recommendations"]),
            blocking_region=blocking_region,  # type: ignore[arg-type]
            nearest_obstacle_class=value["nearest_obstacle_class"],
            state_revision=value["state_revision"],
            state_context=tuple(str(item) for item in value["state_context"]),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValidationError("route convergence failure is malformed") from exc
    if value.get("retry_key") != failure.retry_key:
        raise ValidationError("route convergence retry key is inconsistent")
    return failure.retry_key


@dataclass(frozen=True)
class ApplicationProject:
    root: Path
    state: dict[str, Any]
    conversation: dict[str, Any]

    @property
    def design_root(self) -> Path:
        return self.root / "design"


def default_application_home() -> Path:
    """Return the persistent PCB project repository for normal launches.

    ``PCBDRAFT_HOME`` remains an explicit compatibility and automation override.
    It is never inferred from the shell's current directory.
    """

    configured = os.environ.get("PCBDRAFT_HOME")
    if configured:
        return Path(configured).expanduser()
    return current_repository().root


def _safe_text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    normalized = value.replace("\x00", "").strip()
    if len(normalized.encode("utf-8")) > limit:
        raise ValidationError(f"{field} exceeds the {limit} byte limit")
    return normalized


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "project"
    return slug[:56].rstrip("-") or "project"


def _initial_stackup_layers(request: str) -> int:
    """Infer a provisional stackup only when the planner returned no selection."""

    words = request.casefold()
    if any(token in words for token in ("ddr", "pcie", "serdes", "high-speed", "高速")):
        return 6
    if any(
        token in words
        for token in (
            "rf",
            "antenna",
            "射频",
            "天线",
            "usb",
            "ethernet",
            "high-power",
            "高功率",
        )
    ):
        return 4
    return 2


# Historical internal name retained locally while public callers use the core
# utility.  Keeping redaction below the application layer prevents protocol
# adapters and durable agent records from importing this concrete service.
_sanitize_secret_text = sanitize_user_text


def _public_readiness_record(value: Any) -> Any:
    """Normalize legacy records without mutating retained audit artifacts."""

    if not isinstance(value, dict):
        return value
    result = dict(value)
    result.setdefault(
        "production_evidence_complete", value.get("production_ready") is True
    )
    result["production_ready"] = False
    result["production_claimed"] = False
    return result


class ApplicationService:
    """Single write authority for product projects and their engineering runtime."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        provider_name: str = "auto",
        provider: IntentProvider | None = None,
        recover_interrupted: bool = True,
    ) -> None:
        # A caller-provided workspace exists for isolated automation and tests.
        # Normal product launches always resolve the persisted PCB repository;
        # neither path depends on the process working directory.
        repository: ProjectRepository
        if workspace is not None:
            repository = explicit_repository(workspace)
        elif os.environ.get("PCBDRAFT_HOME"):
            repository = explicit_repository(default_application_home())
        else:
            repository = current_repository()
        self._use_repository(repository)
        self.provider = provider or resolve_provider(provider_name)
        if recover_interrupted:
            self._recover_interrupted_projects()

    def set_repository(self, directory: str | Path) -> ProjectRepository:
        """Persist and start using a new normal project repository.

        This is intentionally unavailable for callers that supplied an explicit
        workspace.  Those callers use an isolated automation location and must
        restart without ``--workspace`` before changing the user's persistent
        product repository.
        """

        if self.repository.source == "explicit":
            raise ValidationError(
                "this session uses an explicit workspace; restart PCBDraft without "
                "--workspace before changing the persistent project repository"
            )
        repository = configure_repository(directory)
        self._use_repository(repository)
        self._recover_interrupted_projects()
        return repository

    def _use_repository(self, repository: ProjectRepository) -> None:
        """Adopt an already validated repository without changing its pointer."""

        self.repository = repository
        self.root = repository.root
        self.repository_source = repository.source
        self.repository_configured_now = repository.configured_now
        self.projects_root = make_directory(self.root / "projects")
        self.locks_root = make_directory(self.root / "locks")

    @staticmethod
    def _bind_expected_revision(
        project: ApplicationProject,
        expected_revision: int | None,
        *,
        operation: str,
    ) -> int:
        """Bind a mutation to the caller's snapshot before any expensive work."""

        if expected_revision is None:
            return int(project.state["revision"])
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValidationError("expected project revision must be non-negative")
        current_revision = int(project.state["revision"])
        if current_revision != expected_revision:
            raise ValidationError(
                f"project changed before {operation}: expected revision "
                f"{expected_revision}, current {current_revision}"
            )
        return expected_revision

    def diagnostics(self) -> dict[str, Any]:
        from pcbdraft.model.tool_calls import provider_agent_protocol

        tools = doctor_report()
        library_tables = tools["library_tables"]
        libraries_ready = all(item["configured"] for item in library_tables.values())
        library_data_ready = all(
            item["available"] for item in tools["library_data"].values()
        )
        return {
            "schema": "pcbdraft-first-run-diagnostics",
            "version": 1,
            "workspace": str(self.root),
            "repository": {
                "root": str(self.root),
                "projects_root": str(self.projects_root),
                "source": self.repository_source,
            },
            "loopback_default": True,
            "provider": (
                self.provider.diagnostic()
                if self.provider is not None
                else {
                    "id": "unconfigured",
                    "available": False,
                    "planning": (
                        "no model provider configured; run `pcbdraft connect` or /connect"
                    ),
                }
            ),
            "agent_orchestration": {
                "router": provider_agent_protocol(self.provider),
                "workflow": "local-evidence-policy",
                "model_decisions_per_turn": 1,
                "parallel_tool_calls": False,
                "engineering_authority": "local registry, permissions, revision CAS, and validation gates",
            },
            "tools": tools["tools"],
            "kicad_library_tables": library_tables,
            "kicad_library_data": tools["library_data"],
            "ready_for_generation": (
                tools["ok"] and libraries_ready and library_data_ready
            ),
            "generation_runtime": {
                "architecture": "requirements -> circuit plan -> local KiCad symbols -> semantic IR -> transactional KiCad",
                "product_path": "generic_agent_plan",
                "component_libraries": "installed stock KiCad symbols and footprints only",
                "validation_note": "results state only what PCBDraft and KiCad actually checked",
            },
            "credential_guidance": {
                "config": "Use `pcbdraft connect` or /connect; credentials stay in PCBDraft's private Hermes home.",
                "persistence": "Credential values are never written to project records or model receipts.",
                "kicad": (
                    "Run `pcbdraft setup` to detect a compatible KiCad 10.0.x "
                    "runtime and initialize missing stock-library tables."
                ),
            },
        }

    def list_projects(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for candidate in sorted(self.projects_root.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                project = self._open_path(candidate)
            except PCBDraftError:
                continue
            result.append(self._summary(project))
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def create_project(self, name: str, request: str) -> dict[str, Any]:
        draft = self.create_draft(name)
        return self.send_message(draft["project"]["id"], request)

    def create_draft(self, name: str) -> dict[str, Any]:
        """Create only the local conversation record; no engineering files exist yet."""

        clean_name = _sanitize_secret_text(_safe_text(name, "project name", limit=512))
        project_id = f"{_slug(clean_name)}-{secrets.token_hex(4)}"
        target = self.projects_root / project_id
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{project_id}.creating-", dir=self.projects_root)
        )
        os.chmod(temporary, 0o700)
        created_at = utc_timestamp()
        state = {
            "schema": APP_PROJECT_SCHEMA,
            "version": APP_PROJECT_VERSION,
            "id": project_id,
            "name": clean_name,
            "created_at": created_at,
            "updated_at": created_at,
            "status": "draft",
            "provider": (
                self.provider.provider_id
                if self.provider is not None
                else "unconfigured"
            ),
            "revision": 0,
            "design_revision": 0,
            "event_sequence": 0,
            "active_transaction": None,
            "last_transaction": None,
            "last_validation": None,
            "last_preview": None,
            "last_release": None,
        }
        conversation = {
            "schema": CONVERSATION_SCHEMA,
            "version": CONVERSATION_VERSION,
            "messages": [],
            "proposal": None,
            "decisions": {},
        }
        try:
            for name_value in (
                "events",
                "jobs",
                "provider-runs",
                "attempts",
                "transactions",
                "releases",
                "validation",
                "previews",
            ):
                make_directory(temporary / name_value)
            atomic_write_json(temporary / "project.json", state)
            atomic_write_json(temporary / "conversation.json", conversation)
            with ResourceLock(target, self.locks_root):
                if target.exists() or target.is_symlink():
                    raise ValidationError("application project identity collision")
                os.replace(temporary, target)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
        return self.open_project(project_id)

    def create_empty_project(self, name: str) -> dict[str, Any]:
        """Create and publish an empty synchronized semantic/KiCad project."""

        draft = self.create_draft(name)
        project_id = str(draft["project"]["id"])
        project = self._open(project_id)
        board = BoardSpec.from_dict(
            {
                "width_mm": 80.0,
                "height_mm": 50.0,
                "layers": 2,
                "thickness_mm": 1.6,
                "edge_clearance_mm": 0.5,
                "min_track_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "finish": "hasl_lead_free",
            }
        )
        scope = Scope.from_dict(
            {
                "domains": ["simple_control"],
                "max_voltage_v": 24.0,
                "max_current_a": 2.0,
                "max_power_w": 24.0,
                "layers": 2,
                "intended_use": "small non-safety-critical prototype board",
                "risk_class": "prototype",
            }
        )
        request = EmptyDesignRequest(
            design_id=project_id,
            name=str(draft["project"]["name"]),
            revision="1",
            scope=scope,
            board=board,
        )
        design = Design.from_dict(
            {
                "schema": "pcbdraft-ir",
                "version": 2,
                "design_id": project_id,
                "name": request.name,
                "revision": request.revision,
                "scope": scope.to_dict(),
                "requirements": [],
                "provenance": [],
                "blocks": [],
                "power_domains": [],
                "interfaces": [],
                "components": [],
                "nets": [
                    {
                        "id": "gnd",
                        "name": "GND",
                        "endpoints": [],
                        "net_class": "power",
                        "intent": "Required native reference plane.",
                    }
                ],
                "constraints": [],
                "board": board.to_dict(),
                "analyses": [],
                "metadata": {
                    "generator": "flat_toolbox_v1",
                    "requirements_hash": hashlib.sha256(
                        request.canonical_bytes()
                    ).hexdigest(),
                    "assurance": "verified",
                },
                "native_intent": {
                    "outline": [
                        {"x_mm": 0.0, "y_mm": 0.0},
                        {"x_mm": board.width_mm, "y_mm": 0.0},
                        {"x_mm": board.width_mm, "y_mm": board.height_mm},
                        {"x_mm": 0.0, "y_mm": board.height_mm},
                    ],
                    "footprint_poses": [],
                    "routes": [],
                    "vias": [],
                    "unrouted_nets": [],
                    "provenance": "pcbdraft",
                    "geometry_revision": 0,
                },
            }
        )
        try:
            generated = materialize_managed_design(
                request,
                design,
                project.design_root,
                graph=PartGraph.bundled(),
            )
        except BaseException:
            # Creation is one atomic operation: a native-generation failure
            # must not leave a misleading draft with the requested identity.
            shutil.rmtree(project.root, ignore_errors=True)
            raise
        try:
            with ResourceLock(project.root, self.locks_root):
                current = self._open(project_id)
                current.state["status"] = "generated"
                current.state["revision"] = 1
                current.state["design_revision"] = 1
                current.state["updated_at"] = utc_timestamp()
                self._event(
                    current.state,
                    current.root,
                    "project.synchronized_empty",
                    "Created an empty synchronized semantic and KiCad project",
                )
                self._write_records(current.root, current.state, current.conversation)
        except BaseException:
            # No project identity is published until both native and
            # application records describe the same synchronized revision.
            shutil.rmtree(project.root, ignore_errors=True)
            raise
        return self._with_tool_result(
            self.open_project(project_id),
            {
                "created": True,
                "synchronized": True,
                "design_content_hash": generated.project.design.content_hash(),
                "manifest_hashes": generated.project.manifest["hashes"],
            },
        )

    def open_project(self, project_id: str) -> dict[str, Any]:
        return self._public_project(self._open(project_id))

    def try_open_project_snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        """Read a self-consistent public view without blocking live UI polling.

        Project records and managed design directories are updated under the
        project lock.  A live client must use the same lock or it can otherwise
        observe a conversation from one revision and state/design files from
        another.  Returning ``None`` when the writer is busy lets callers keep
        streaming events and retry on their next poll.
        """

        root = self._project_path(project_id)
        lock = ResourceLock(root, self.locks_root, timeout=timeout)
        try:
            lock.acquire()
        except PCBDraftError as exc:
            if "resource is locked by another runtime process" in str(exc):
                return None
            raise
        try:
            return self._public_project(self._open_path(root))
        finally:
            lock.release()

    def project_root(self, project_id: str) -> Path:
        """Return a validated application-owned root for internal adapters."""

        return self._open(project_id).root

    def execute_pcb_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Execute one registry-bound flat PCB operation.

        The model-facing name is already resolved by the closed registry. This
        method owns the final service dispatch and always returns one public
        project view augmented with a bounded, fact-only operation result.
        """

        if tool_name == "inspect_project":
            return self._with_tool_result(
                self.open_project(project_id), {"inspection": "project"}
            )
        if tool_name in {
            "inspect_design",
            "inspect_component",
            "inspect_net",
            "inspect_board",
            "inspect_events",
            "inspect_evidence",
            "inspect_transaction",
        }:
            return self._inspect_pcb_tool(project_id, tool_name, arguments)
        if tool_name in {
            "search_symbols",
            "describe_symbol",
            "search_footprints",
            "describe_footprint",
        }:
            return self._inspect_library_tool(project_id, tool_name, arguments)
        if tool_name in {"search_parts", "describe_part"}:
            return self._inspect_part_tool(project_id, tool_name, arguments)
        if tool_name == "register_kicad_part":
            return self.register_kicad_part(
                project_id,
                arguments["value"],
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {
            "check_semantics",
            "check_connectivity",
            "run_erc",
            "run_drc",
        }:
            return self.run_pcb_check(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {"render_schematic", "render_board", "render_3d"}:
            return self.render_pcb_output(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {
            "export_gerbers",
            "export_drill",
            "export_bom",
            "export_pick_place",
            "export_step",
        }:
            return self.export_pcb_output(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        return self.apply_pcb_operation(
            project_id,
            tool_name,
            arguments,
            timeout=timeout,
            expected_revision=expected_revision,
        )

    @staticmethod
    def _with_tool_result(
        view: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        detached = dict(view)
        detached["tool_result"] = result
        return detached

    def _inspect_pcb_tool(
        self, project_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._open(project_id)
        view = self._public_project(project)
        facts: dict[str, Any]
        if tool_name == "inspect_transaction":
            facts = self._inspect_transaction_artifact(
                project, str(arguments["artifact_id"])
            )
        elif tool_name == "inspect_events":
            facts = {"events": self.events(project_id)[-100:]}
        elif tool_name == "inspect_evidence":
            facts = {
                "artifacts": view["artifacts"],
                "attempts": view["attempts"],
                "individual_checks": self._retained_evidence(
                    project.root / "validation",
                    schema="pcbdraft-individual-check-receipt",
                ),
                "individual_renders": self._retained_evidence(
                    project.root / "previews",
                    schema="pcbdraft-preview-bundle",
                    require_single="renders",
                ),
                "individual_exports": self._retained_evidence(
                    project.root / "releases",
                    schema="pcbdraft-individual-manufacturing-export",
                ),
            }
        else:
            if not project.design_root.is_dir() or project.design_root.is_symlink():
                raise ValidationError("project has no synchronized design to inspect")
            managed = open_managed_project(project.design_root)
            managed.assert_synchronized()
            if tool_name == "inspect_design":
                facts = {
                    "design": managed.design.to_dict(),
                    "content_hash": managed.design.content_hash(),
                }
            elif tool_name == "inspect_board":
                facts = {
                    "board": managed.manifest["native_snapshots"]["board"],
                    "content_hash": managed.design.content_hash(),
                }
            elif tool_name == "inspect_component":
                component_id = str(arguments["component_id"])
                component = next(
                    (
                        item
                        for item in managed.design.components
                        if item.id == component_id
                    ),
                    None,
                )
                if component is None:
                    raise ValidationError(f"component is absent: {component_id}")
                facts = {
                    "component": component.to_dict(),
                    "nets": [
                        net.to_dict()
                        for net in managed.design.nets
                        if any(
                            endpoint.component == component_id
                            for endpoint in net.endpoints
                        )
                    ],
                }
            else:
                net_id = str(arguments["net_id"])
                net = next(
                    (item for item in managed.design.nets if item.id == net_id), None
                )
                if net is None:
                    raise ValidationError(f"net is absent: {net_id}")
                facts = {
                    "net": net.to_dict(),
                    "routes": [
                        item.to_dict()
                        for item in managed.design.native_intent.routes
                        if item.net == net_id
                    ],
                    "vias": [
                        item.to_dict()
                        for item in managed.design.native_intent.vias
                        if item.net == net_id
                    ],
                }
        facts["project_id"] = project_id
        facts["revision"] = project.state["revision"]
        return self._with_tool_result(view, facts)

    @staticmethod
    def _inspect_transaction_artifact(
        project: ApplicationProject, artifact_id: str
    ) -> dict[str, Any]:
        """Read one current-project transaction receipt through a fixed boundary."""

        match = re.fullmatch(
            r"transaction:([0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})", artifact_id
        )
        if match is None:
            raise ValidationError("transaction artifact identity is invalid")
        transaction_id = match.group(1)
        transactions_root = project.root / "transactions"
        if transactions_root.is_symlink() or not transactions_root.is_dir():
            raise ValidationError("transaction artifact is unavailable")
        transaction = transactions_root / transaction_id
        receipt_path = transaction / "receipt.json"
        if (
            transaction.is_symlink()
            or not transaction.is_dir()
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            raise ValidationError("transaction artifact is unavailable")
        receipt = load_json_limited(receipt_path, TRANSACTION_INSPECTION_FILE_LIMIT)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema")
            not in {
                "pcbdraft-flat-operation-receipt",
                "pcbdraft-kicad-part-registration-receipt",
            }
            or receipt.get("version") not in {1, 2}
            or receipt.get("status") not in {"preparing", "noop", "applied", "failed"}
            or not isinstance(receipt.get("operation"), str)
            or not receipt["operation"]
            or not _transaction_inspection_depth_is_valid(receipt)
        ):
            raise ValidationError("transaction artifact receipt is invalid")

        def bounded_list(name: str) -> list[Any]:
            values = receipt.get(name)
            if not isinstance(values, list):
                return []
            return copy.deepcopy(values[:TRANSACTION_INSPECTION_ITEM_LIMIT])

        detail = {
            key: copy.deepcopy(receipt[key])
            for key in (
                "schema",
                "version",
                "status",
                "operation",
                "error_code",
                "failure",
                "baseline_revision",
                "baseline_design_revision",
                "candidate_revision",
                "committed_revision",
                "committed_design_revision",
                "consistency_passed",
                "intended_delta",
                "native_delta",
                "routing_failure",
                "rollback_performed",
                "rollback",
                "progress_delta",
                "stage_before",
                "stage_after",
                "convergence",
                "transaction_scope",
            )
            if key in receipt
        }
        detail["postconditions"] = bounded_list("postconditions")
        artifacts = receipt.get("artifact")
        detail["available_details"] = (
            sorted(str(key) for key in artifacts)[:TRANSACTION_INSPECTION_ITEM_LIMIT]
            if isinstance(artifacts, Mapping)
            else []
        )
        return {
            "artifact_id": artifact_id,
            "detail": detail,
            "detail_truncated": bool(
                isinstance(receipt.get("postconditions"), list)
                and len(receipt["postconditions"]) > TRANSACTION_INSPECTION_ITEM_LIMIT
            ),
        }

    @staticmethod
    def _retained_evidence(
        root: Path,
        *,
        schema: str,
        require_single: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded completed evidence receipts without trusting paths."""

        if root.is_symlink() or not root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for candidate in sorted(root.iterdir(), reverse=True):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            receipt_path = candidate / "receipt.json"
            try:
                receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if (
                not isinstance(receipt, dict)
                or receipt.get("schema") != schema
                or receipt.get("status") != "complete"
            ):
                continue
            if require_single is not None:
                selected = receipt.get(require_single)
                if not isinstance(selected, list) or len(selected) != 1:
                    continue
            record = dict(receipt)
            record["run_id"] = candidate.name
            record["receipt"] = receipt_path.relative_to(root.parent).as_posix()
            records.append(record)
            if len(records) >= 100:
                break
        return records

    def _inspect_library_tool(
        self, project_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        facts = self.inspect_installed_library(tool_name, arguments)
        return self._with_tool_result(self.open_project(project_id), facts)

    @staticmethod
    def inspect_installed_library(
        tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Read installed KiCad facts without reading or selecting a project."""

        if tool_name in {"search_symbols", "describe_symbol"}:
            from pcbdraft.agent.part_resolver import LocalKiCadPartResolver

            resolver = LocalKiCadPartResolver()
            if tool_name == "search_symbols":
                facts: dict[str, Any] = {
                    "query": arguments["query"],
                    "symbols": list(
                        resolver.find_ids(str(arguments["query"]), limit=24)
                    ),
                }
            else:
                facts = resolver.describe(str(arguments["symbol"])).to_dict()
        else:
            from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver

            footprint_resolver = LocalKiCadFootprintResolver()
            facts = (
                {
                    "query": arguments["query"],
                    "footprints": [
                        item.to_dict()
                        for item in footprint_resolver.find(
                            str(arguments["query"]), limit=24
                        )
                    ],
                }
                if tool_name == "search_footprints"
                else footprint_resolver.describe(str(arguments["footprint"])).to_dict()
            )
        return facts

    def _inspect_part_tool(
        self, project_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._open(project_id)
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized part catalog")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        if tool_name == "search_parts":
            matches = managed.graph.search(str(arguments["query"]), limit=24)
            facts: dict[str, Any] = {
                "query": arguments["query"],
                "available_count": len(managed.graph),
                "matched_count": len(matches),
                "parts": [
                    {
                        "id": part.id,
                        "kind": part.kind,
                        "description": part.description,
                        "symbol": part.symbol,
                        "footprint": part.footprint,
                        "trust": part.trust,
                    }
                    for part in matches
                ],
            }
        else:
            part = managed.graph.get(str(arguments["part_id"]))
            facts = {
                "available_count": len(managed.graph),
                "part": part.to_dict(),
            }
        facts["project_id"] = project_id
        facts["revision"] = project.state["revision"]
        return self._with_tool_result(self._public_project(project), facts)

    def register_kicad_part(
        self,
        project_id: str,
        value: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Atomically publish one inspected local KiCad part contract."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="register_kicad_part"
        )
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized design to modify")
        authoritative = open_managed_project(project.design_root)
        authoritative.assert_synchronized()
        before_catalog_hash = hashlib.sha256(
            canonical_json_bytes(authoritative.graph.to_dict())
        ).hexdigest()
        baseline_design_revision = int(project.state["design_revision"])
        before_progress, before_stage = self._current_progress_and_stage(project)
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-kicad-part-registration-receipt",
            "version": 2,
            "status": "preparing",
            "operation": "register_kicad_part",
            "native_scope": "catalog_plus_native_rematerialization",
            "created_at": utc_timestamp(),
            "baseline_revision": expected_revision,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": None,
            "committed_revision": None,
            "committed_design_revision": None,
            "before_hash": authoritative.design.content_hash(),
            "before_catalog_hash": before_catalog_hash,
            "part_id": value.get("id"),
            "rollback_performed": False,
            "rollback": {
                "state": "not_required",
                "performed": False,
                "live_unchanged": True,
            },
            "artifact": {
                "transaction_id": transaction_id,
                "receipt": "receipt.json",
            },
        }
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        atomic_write_json(receipt_path, receipt)
        stage = "inspection"
        try:
            from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver
            from pcbdraft.agent.part_resolver import LocalKiCadPartResolver

            symbol = LocalKiCadPartResolver().describe(str(value.get("symbol", "")))
            footprint = LocalKiCadFootprintResolver().describe(
                str(value.get("footprint", ""))
            )
            pins = value.get("pins")
            if not isinstance(pins, list):
                raise ValidationError("part pins must be an array")
            installed_pins = {item["number"]: item for item in symbol.pins}
            supplied_pins = {
                str(item.get("number")): item for item in pins if isinstance(item, dict)
            }
            if set(supplied_pins) != set(installed_pins):
                raise ValidationError(
                    "part pins must exactly cover the installed symbol pin numbers"
                )
            available_pads = set(footprint.pad_numbers)
            for number, installed in installed_pins.items():
                supplied = supplied_pins[number]
                if supplied.get("name") != installed["name"]:
                    raise ValidationError(
                        f"part pin {number} name differs from the installed symbol"
                    )
                if supplied.get("electrical_type") != installed["electrical_type"]:
                    raise ValidationError(
                        f"part pin {number} electrical type differs from the installed symbol"
                    )
                if supplied.get("footprint_pad") not in available_pads:
                    raise ValidationError(
                        f"part pin {number} maps to a missing footprint pad"
                    )
            part = PartGraph.installed_kicad_part(
                value, footprint_sha256=footprint.sha256
            )
            existing = authoritative.graph.get_optional(part.id)
            if existing is not None:
                if existing.to_dict() != part.to_dict():
                    raise ValidationError(
                        f"part id already exists with different facts: {part.id}"
                    )
                with ResourceLock(project.root, self.locks_root):
                    current = self._open(project_id)
                    if current.state["revision"] != expected_revision:
                        raise ValidationError(
                            "project changed while part registration was inspected"
                        )
                    current_design = open_managed_project(current.design_root)
                    current_design.assert_synchronized()
                    current_catalog_hash = hashlib.sha256(
                        canonical_json_bytes(current_design.graph.to_dict())
                    ).hexdigest()
                    if current_design.design.content_hash() != receipt["before_hash"]:
                        raise ValidationError(
                            "authoritative design changed before part no-op"
                        )
                    if current_catalog_hash != receipt["before_catalog_hash"]:
                        raise ValidationError(
                            "authoritative part catalog changed before part no-op"
                        )
                receipt.update(
                    {
                        "status": "noop",
                        "native_scope": "catalog_noop_no_native_write",
                        "completed_at": utc_timestamp(),
                        "candidate_revision": baseline_design_revision,
                        "after_hash": authoritative.design.content_hash(),
                        "after_catalog_hash": before_catalog_hash,
                    }
                )
                atomic_write_json(receipt_path, receipt)
                return self._with_tool_result(
                    self.open_project(project_id),
                    {
                        "operation": "register_kicad_part",
                        "transaction_id": transaction_id,
                        "project_id": project_id,
                        "part": part.to_dict(),
                        "changed": False,
                        "catalog_count": len(authoritative.graph),
                    },
                )
            graph = authoritative.graph.merged([part], source=f"project:{project_id}")
            document = authoritative.design.to_dict()
            document["metadata"]["assurance"] = "provisional"
            candidate = Design.from_dict(document)
            after_catalog_hash = hashlib.sha256(
                canonical_json_bytes(graph.to_dict())
            ).hexdigest()
            receipt.update(
                {
                    "candidate_revision": baseline_design_revision + 1,
                    "after_hash": candidate.content_hash(),
                    "after_catalog_hash": after_catalog_hash,
                    "symbol": symbol.to_dict(),
                    "footprint": footprint.to_dict(),
                }
            )
            atomic_write_json(receipt_path, receipt)
            request = load_generation_request(authoritative.requirements_path)
            stage = "materialization"
            materialize_managed_design(
                request,
                candidate,
                staged,
                graph=graph,
                plan=authoritative.plan,
                retain_failed_attempt=transaction / "failed-native",
                lock_timeout=min(10.0, timeout),
                auto_place=False,
                route_net_ids=frozenset(),
                allow_incomplete=True,
            )
            staged_project = open_managed_project(staged)
            staged_project.assert_synchronized()
            if staged_project.design.content_hash() != candidate.content_hash():
                raise ValidationError("staged semantic design hash changed")
            if (
                hashlib.sha256(
                    canonical_json_bytes(staged_project.graph.to_dict())
                ).hexdigest()
                != after_catalog_hash
            ):
                raise ValidationError("staged part catalog hash changed")
            candidate_revision = baseline_design_revision + 1
            stage = "native_consistency"
            consistency_failure: PCBDraftError | None = None
            try:
                consistency = inspect_native_consistency(
                    candidate,
                    staged_project.schematic_path,
                    staged_project.board_path,
                    candidate_revision=candidate_revision,
                    graph=graph,
                )
            except PCBDraftError as exc:
                consistency_failure = exc
                consistency = _unavailable_consistency_report(candidate_revision)
            atomic_write_json(
                transaction / "native-consistency.json", consistency.to_dict()
            )
            receipt["consistency_passed"] = consistency.consistency_passed
            receipt["postconditions"] = _native_postconditions(
                "register_kicad_part", consistency
            )
            receipt["artifact"]["native_consistency"] = "native-consistency.json"
            try:
                candidate_board = _native_board_projection(staged_project)
            except PCBDraftError:
                candidate_board = None
            after_progress = _progress_vector(
                candidate,
                graph,
                candidate_revision,
                consistency=consistency,
                board=candidate_board,
                fatal_drc=before_progress.fatal_drc_count.for_revision(
                    candidate_revision
                ),
                error_drc=before_progress.error_drc_count.for_revision(
                    candidate_revision
                ),
                erc_error=before_progress.erc_error_count.for_revision(
                    candidate_revision
                ),
            )
            after_stage = derive_stage(
                after_progress,
                _progress_stage_evidence(
                    candidate,
                    candidate_revision,
                    requirements_frozen=staged_project.requirements_path.is_file(),
                    consistency=consistency,
                    progress=after_progress,
                    erc_check=EvidenceCheck.unknown(candidate_revision),
                    drc_check=EvidenceCheck.unknown(candidate_revision),
                ),
            )
            _attach_progress(
                receipt,
                before_progress,
                after_progress,
                before_stage,
                after_stage,
            )
            if consistency_failure is not None:
                raise _PCBOperationPostconditionError(
                    "native_verification_failed",
                    "native KiCad inspection failed",
                ) from consistency_failure
            if not consistency.consistency_passed:
                raise _consistency_rejection("register_kicad_part", None, consistency)
            stage = "native_delta"
            native_delta = compare_native_operation_delta(
                "register_kicad_part",
                {},
                authoritative.design,
                candidate,
                _native_board_projection(authoritative),
                _native_board_projection(staged_project),
                before_schematic=_native_schematic_projection(authoritative),
                after_schematic=_native_schematic_projection(staged_project),
                graph=graph,
            )
            atomic_write_json(
                transaction / "native-operation-delta.json", native_delta.to_dict()
            )
            receipt["artifact"]["native_delta"] = "native-operation-delta.json"
            receipt["postconditions"].extend(_native_delta_postconditions(native_delta))
            receipt["native_delta"] = {
                "operation_checked": True,
                "policy": native_delta.policy,
                "passed": native_delta.passed,
                "failed_checks": [
                    item.name for item in native_delta.checks if not item.passed
                ][:4],
            }
            atomic_write_json(receipt_path, receipt)
            if not native_delta.passed:
                raise _PCBOperationPostconditionError(
                    "native_delta_failed",
                    "part registration changed unrelated native project state",
                )
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage=stage, tool_name="register_kicad_part"
            )
            _attach_progress(
                receipt,
                before_progress,
                before_progress,
                before_stage,
                before_stage,
            )
            atomic_write_json(receipt_path, receipt)
            _bind_transaction_failure(exc, transaction_id)
            raise

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            moved_before = False
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while part registration was staged"
                    )
                current_design = open_managed_project(current.design_root)
                current_design.assert_synchronized()
                current_catalog_hash = hashlib.sha256(
                    canonical_json_bytes(current_design.graph.to_dict())
                ).hexdigest()
                if current_design.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed before part publication"
                    )
                if current_catalog_hash != receipt["before_catalog_hash"]:
                    raise ValidationError(
                        "authoritative part catalog changed before publication"
                    )
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                published = open_managed_project(current.design_root)
                published.assert_synchronized()
                current.state["status"] = "generated"
                current.state["revision"] += 1
                current.state["design_revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                current.state["last_validation"] = None
                current.state["last_preview"] = None
                current.state["last_release"] = None
                current.state["last_transaction"] = transaction_id
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "pcb.kicad_part_registered",
                    f"Registered installed KiCad part {part.id}",
                )
                self._write_records(current.root, current.state, current.conversation)
                # Publish success last.  A receipt write failure must still be
                # able to restore the live design, event, and project records
                # without leaving a durable applied claim for a rolled-back part.
                receipt.update(
                    {
                        "status": "applied",
                        "applied_at": utc_timestamp(),
                        "committed_revision": current.state["revision"],
                        "committed_design_revision": current.state["design_revision"],
                        "rollback": {
                            "state": "committed",
                            "performed": False,
                            "live_unchanged": False,
                        },
                    }
                )
                atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        failed_published = transaction / "failed-published"
                        if current.design_root.exists():
                            os.replace(current.design_root, failed_published)
                        if before.exists():
                            os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                    rollback_failures.append(rollback_exc)
                receipt["status"] = "failed"
                receipt["failed_at"] = utc_timestamp()
                receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
                receipt.pop("applied_at", None)
                receipt["committed_revision"] = None
                receipt["committed_design_revision"] = None
                receipt["error_code"] = _operation_failure_code(
                    exc, stage="publication", tool_name="register_kicad_part"
                )
                receipt["rollback_performed"] = moved_before and not rollback_failures
                receipt["rollback"] = {
                    "state": (
                        "restored"
                        if moved_before and not rollback_failures
                        else "incomplete"
                        if rollback_failures
                        else "not_required"
                    ),
                    "performed": moved_before and not rollback_failures,
                    "live_unchanged": not rollback_failures,
                }
                if not rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(baseline_design_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    failure = PCBDraftError(
                        "part publication failed and rollback was incomplete"
                    )
                    _bind_transaction_failure(failure, transaction_id)
                    raise failure from exc
                _bind_transaction_failure(exc, transaction_id)
                raise
        return self._with_tool_result(
            self.open_project(project_id),
            {
                "operation": "register_kicad_part",
                "transaction_id": transaction_id,
                "project_id": project_id,
                "part": part.to_dict(),
                "symbol": symbol.to_dict(),
                "footprint": footprint.to_dict(),
                "changed": True,
                "catalog_count": len(graph),
                "before_catalog_hash": before_catalog_hash,
                "after_catalog_hash": after_catalog_hash,
                "revision": current.state["revision"],
                "design_revision": current.state["design_revision"],
                "progress_before": receipt["progress_before"],
                "progress_after": receipt["progress_after"],
                "progress_delta": receipt["progress_delta"],
                "stage": receipt["stage_after"],
            },
        )

    def run_pcb_check(
        self,
        project_id: str,
        kind: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Run and retain exactly one source-bound flat-toolbox check."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=kind
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        output = project.root / "validation" / run_id
        result = run_individual_check(
            managed,
            kind,
            output=output,
            timeout=timeout,
        )
        check_receipt_path = output / "receipt.json"
        check_receipt = load_json_limited(check_receipt_path, APP_FILE_LIMIT)
        if (
            not isinstance(check_receipt, dict)
            or check_receipt.get("schema") != "pcbdraft-individual-check-receipt"
            or check_receipt.get("status") != "complete"
        ):
            raise ValidationError("individual PCB check receipt is incomplete")
        # The low-level checker is reusable outside ApplicationService and binds
        # itself to content.  The product boundary additionally binds its
        # evidence to the exact semantic revision before it can advance a stage.
        check_receipt["source_revision"] = expected_revision
        check_receipt["source_design_revision"] = project.state["design_revision"]
        atomic_write_json(check_receipt_path, check_receipt)
        summary = {
            "run_id": run_id,
            "check": kind,
            "report": result.report_path.relative_to(project.root).as_posix(),
            "report_sha256": result.report_sha256,
            "state": result.state,
            "outcome": result.outcome,
            "design_content_hash": result.design_content_hash,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "production_ready": False,
            "production_claimed": False,
        }
        report = load_json_limited(result.report_path, GATE_JSON_LIMIT)
        details = report.get("details", {}) if isinstance(report, dict) else {}
        if isinstance(details, dict):
            violations = details.get("violations")
            issues = details.get("issues")
            if isinstance(violations, list):
                errors, warnings = count_severities(violations)
                diagnostics = {
                    "counts": {
                        "error": errors,
                        "warning": warnings,
                        "total": errors + warnings,
                    },
                    **structured_violations(violations, max_violations=20),
                    "full_details_report": summary["report"],
                }
                shown = diagnostics["violations"]
                total_seen = diagnostics["violation_count_seen"]
                diagnostics["details_truncated"] = diagnostics["violations_truncated"]
                diagnostics["remaining_violation_count"] = max(
                    0, total_seen - len(shown)
                )
                tool_run = report.get("tool_run") if isinstance(report, dict) else None
                raw_report = (
                    tool_run.get("raw_report")
                    if isinstance(tool_run, Mapping)
                    else None
                )
                if (
                    isinstance(raw_report, str)
                    and raw_report
                    and Path(raw_report).name == raw_report
                ):
                    diagnostics["raw_report"] = (
                        (result.report_path.parent / raw_report)
                        .relative_to(project.root)
                        .as_posix()
                    )
                summary["diagnostics"] = diagnostics
            elif isinstance(issues, list):
                summary["diagnostics"] = {
                    "issue_count_seen": len(issues),
                    "issues": issues[:20],
                    "issues_truncated": len(issues) > 20,
                }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the check was running")
            current_managed = open_managed_project(current.design_root)
            current_managed.assert_synchronized()
            if current_managed.design.content_hash() != result.design_content_hash:
                raise ValidationError("design changed while the check was running")
            current.state["last_validation"] = summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "pcb.check_complete",
                f"Completed individual PCB check {kind}",
                level="error" if result.outcome == "fail" else "info",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self._with_tool_result(
            self.open_project(project_id),
            {**summary, "revision": current.state["revision"]},
        )

    def render_pcb_output(
        self,
        project_id: str,
        kind: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Generate and retain only one requested preview family."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=kind
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        bundle = generate_preview(
            managed,
            project.root / "previews" / run_id,
            kind,
            timeout=timeout,
        )
        summary = {
            "run_id": run_id,
            "render": kind,
            "root": bundle.root.relative_to(project.root).as_posix(),
            "receipt": bundle.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": bundle.design_content_hash,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "files": {
                key: path.relative_to(project.root).as_posix()
                for key, path in bundle.files.items()
            },
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the preview was rendered")
            if (
                open_managed_project(current.design_root).design.content_hash()
                != bundle.design_content_hash
            ):
                raise ValidationError("design changed while the preview was rendered")
            current.state["last_preview"] = summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "pcb.render_complete",
                f"Completed individual PCB render {kind}",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self._with_tool_result(
            self.open_project(project_id),
            {**summary, "revision": current.state["revision"]},
        )

    def export_pcb_output(
        self,
        project_id: str,
        kind: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Generate and retain only one requested manufacturing export."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=kind
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        exported = export_manufacturing_output(
            managed,
            project.root / "releases" / run_id,
            kind,
            timeout=timeout,
        )
        summary = {
            "id": run_id,
            "export": kind,
            "root": exported.root.relative_to(project.root).as_posix(),
            "receipt": exported.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": exported.design_content_hash,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "artifacts": list(exported.artifacts),
            "production_ready": False,
            "production_claimed": False,
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the export was generated")
            if (
                open_managed_project(current.design_root).design.content_hash()
                != exported.design_content_hash
            ):
                raise ValidationError("design changed while the export was generated")
            current.state["last_release"] = summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "pcb.export_complete",
                f"Completed individual PCB export {kind}",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self._with_tool_result(
            self.open_project(project_id),
            {**summary, "revision": current.state["revision"]},
        )

    @staticmethod
    def _native_progress_sources(
        managed: Any,
        design_revision: int,
        graph: PartGraph,
    ) -> tuple[NativeConsistencyReport | None, NativeBoardProjection | None]:
        """Reuse retained native projections, failing to unknown rather than zero."""

        try:
            board = _native_board_projection(managed)
            schematic = _native_schematic_projection(managed)
            report = compare_native_consistency(
                managed.design,
                schematic,
                board,
                candidate_revision=design_revision,
                graph=graph,
            )
        except PCBDraftError:
            return None, None
        return report, board

    @staticmethod
    def _retained_check_progress(
        project: ApplicationProject,
        design_hash: str,
        kind: str,
        design_revision: int,
    ) -> tuple[MetricValue, MetricValue | None, EvidenceCheck]:
        """Read the newest check bound to the current semantic design hash."""

        retained_validation = project.state.get("last_validation")
        if isinstance(retained_validation, Mapping):
            relative_report = retained_validation.get("report")
            if (
                isinstance(relative_report, str)
                and retained_validation.get("source_design_revision") == design_revision
            ):
                report_path = project.root / relative_report
                try:
                    report_path.relative_to(project.root)
                except ValueError:
                    pass
                else:
                    aggregate = ApplicationService._aggregate_check_progress(
                        report_path.parent,
                        design_hash,
                        kind,
                        design_revision,
                    )
                    if aggregate[0].status is not EvidenceStatus.UNKNOWN:
                        return aggregate

        root = project.root / "validation"
        if not root.is_dir() or root.is_symlink():
            return (
                MetricValue.unknown(design_revision),
                None,
                EvidenceCheck.unknown(design_revision),
            )
        for directory in sorted(root.iterdir(), reverse=True)[:100]:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                receipt = load_json_limited(directory / "receipt.json", APP_FILE_LIMIT)
                if (
                    not isinstance(receipt, Mapping)
                    or receipt.get("schema") != "pcbdraft-individual-check-receipt"
                    or receipt.get("status") != "complete"
                    or receipt.get("check") != kind
                    or receipt.get("design_content_hash") != design_hash
                    or receipt.get("source_design_revision") != design_revision
                    or not isinstance(receipt.get("report"), str)
                ):
                    continue
                report = load_json_limited(
                    directory / str(receipt["report"]), GATE_JSON_LIMIT
                )
            except PCBDraftError:
                # Malformed or partial evidence cannot become a known zero.
                continue
            if (
                not isinstance(report, Mapping)
                or report.get("check") != kind
                or report.get("design_content_hash") != design_hash
                or report.get("outcome") != receipt.get("outcome")
                or report.get("state") != receipt.get("state")
            ):
                continue
            details = report.get("details")
            violations = (
                details.get("violations") if isinstance(details, Mapping) else None
            )
            if not isinstance(violations, list):
                continue
            errors, _warnings = count_severities(violations)
            outcome = report.get("outcome")
            if outcome not in {"pass", "fail"}:
                continue
            metric = MetricValue.known(errors, design_revision)
            fatal = (
                MetricValue.known(_fatal_drc_count(violations), design_revision)
                if kind == "run_drc"
                else None
            )
            return (
                metric,
                fatal,
                EvidenceCheck.known(outcome == "pass", design_revision),
            )
        return (
            MetricValue.unknown(design_revision),
            None,
            EvidenceCheck.unknown(design_revision),
        )

    @staticmethod
    def _bind_aggregate_validation_revision(
        validation_root: Path, design_hash: str, design_revision: int
    ) -> None:
        """Bind a complete aggregate receipt to the revision that produced it."""

        receipt_path = validation_root / "receipt.json"
        try:
            receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        except PCBDraftError:
            return
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-validation-receipt"
            or receipt.get("status") != "complete"
            or receipt.get("design_content_hash") != design_hash
        ):
            return
        receipt["source_design_revision"] = design_revision
        atomic_write_json(receipt_path, receipt)

    @staticmethod
    def _latest_drc_baseline(
        project: ApplicationProject,
    ) -> tuple[Path, int, str] | None:
        """Locate the newest complete application validation as a DRC baseline."""

        root = project.root / "validation"
        if not root.is_dir() or root.is_symlink():
            return None
        for directory in sorted(root.iterdir(), reverse=True)[:100]:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                receipt = load_json_limited(directory / "receipt.json", APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema") != "pcbdraft-validation-receipt"
                or receipt.get("status") != "complete"
                or not isinstance(receipt.get("source_design_revision"), int)
                or not isinstance(receipt.get("design_content_hash"), str)
                or not isinstance(receipt.get("complete_rule_evidence"), Mapping)
            ):
                continue
            name = receipt["complete_rule_evidence"].get("drc")
            if not isinstance(name, str) or Path(name).name != name:
                continue
            return (
                directory / name,
                int(receipt["source_design_revision"]),
                str(receipt["design_content_hash"]),
            )
        return None

    @staticmethod
    def _aggregate_check_progress(
        validation_root: Path,
        design_hash: str,
        kind: str,
        design_revision: int,
    ) -> tuple[MetricValue, MetricValue | None, EvidenceCheck]:
        """Read one aggregate validation's normalized ERC/DRC evidence."""

        unknown = (
            MetricValue.unknown(design_revision),
            None,
            EvidenceCheck.unknown(design_revision),
        )
        if kind not in {"run_erc", "run_drc"}:
            return unknown
        try:
            receipt = load_json_limited(
                validation_root / "receipt.json", APP_FILE_LIMIT
            )
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema") != "pcbdraft-validation-receipt"
                or receipt.get("status") != "complete"
                or receipt.get("design_content_hash") != design_hash
                or receipt.get("source_design_revision") != design_revision
                or not isinstance(receipt.get("tool_runs"), Mapping)
            ):
                return unknown
            tool_kind = "erc" if kind == "run_erc" else "drc"
            tool_run = receipt["tool_runs"].get(tool_kind)
            if (
                not isinstance(tool_run, Mapping)
                or tool_run.get("status") != "completed"
                or tool_run.get("failure") is not None
                or not isinstance(tool_run.get("normalized_report"), str)
            ):
                return unknown
            report_name = str(tool_run["normalized_report"])
            if (
                Path(report_name).name != report_name
                or report_name != f"{tool_kind}.json"
            ):
                return unknown
            document = load_json_limited(validation_root / report_name, GATE_JSON_LIMIT)
        except PCBDraftError:
            return unknown
        if not isinstance(document, Mapping) or not document:
            return unknown
        errors, _warnings = count_severities(document)
        metric = MetricValue.known(errors, design_revision)
        if kind == "run_erc":
            return metric, None, EvidenceCheck.known(errors == 0, design_revision)
        fatal = MetricValue.known(_fatal_drc_count(document), design_revision)
        return metric, fatal, EvidenceCheck.known(errors == 0, design_revision)

    @staticmethod
    def _transaction_receipts(
        project: ApplicationProject, *, strict: bool = False
    ) -> tuple[dict[str, Any], ...]:
        root = project.root / "transactions"
        if not root.is_dir() or root.is_symlink():
            return ()
        paths = tuple(root.glob("*/receipt.json"))
        if strict and len(paths) > 2_000:
            raise ValidationError("route convergence history exceeds its bound")
        records: list[tuple[str, str, dict[str, Any]]] = []
        for path in paths:
            try:
                value = load_json_limited(path, APP_FILE_LIMIT)
            except PCBDraftError as exc:
                if strict:
                    raise ValidationError(
                        "route convergence history contains an unreadable receipt"
                    ) from exc
                continue
            if isinstance(value, dict):
                created_at = value.get("created_at")
                records.append(
                    (
                        created_at if isinstance(created_at, str) else "",
                        path.parent.name,
                        value,
                    )
                )
            elif strict:
                raise ValidationError(
                    "route convergence history contains a malformed receipt"
                )
        records.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(item[2] for item in records[: 2_000 if strict else 100])

    @classmethod
    def _routing_failure_count(
        cls, project: ApplicationProject, design_revision: int, state_key: str | None
    ) -> int:
        return sum(
            receipt.get("operation") == "route_net"
            and receipt.get("status") == "failed"
            and receipt.get("baseline_design_revision") == design_revision
            and (state_key is None or receipt.get("convergence_state_key") == state_key)
            and isinstance(receipt.get("routing_failure"), Mapping)
            for receipt in cls._transaction_receipts(project)
        )

    @classmethod
    def _route_convergence_decision(
        cls,
        project: ApplicationProject,
        *,
        state_key: str,
    ) -> ConvergenceDecision:
        observations: list[ConvergenceObservation] = []
        matching_retry_key: str | None = None
        try:
            receipts = cls._transaction_receipts(project, strict=True)
            for receipt in reversed(receipts):
                schema = receipt.get("schema")
                if schema not in {
                    "pcbdraft-flat-operation-receipt",
                    "pcbdraft-kicad-part-registration-receipt",
                    "pcbdraft-agent-repair-transaction",
                }:
                    raise ValidationError(
                        "route convergence history contains an unknown receipt"
                    )
                if schema != "pcbdraft-flat-operation-receipt":
                    if receipt.get("operation") == "route_net":
                        raise ValidationError(
                            "route convergence receipt schema is inconsistent"
                        )
                    continue
                operation = receipt.get("operation")
                if not isinstance(operation, str):
                    raise ValidationError(
                        "route convergence history contains a malformed operation"
                    )
                if operation != "route_net":
                    continue
                if receipt.get("version") != 2 or receipt.get("status") not in {
                    "failed",
                    "applied",
                }:
                    raise ValidationError(
                        "route convergence history contains an incomplete route receipt"
                    )
                retained_state = _route_state_key_from_record(
                    receipt.get("convergence_state")
                )
                if receipt.get("convergence_state_key") != retained_state:
                    raise ValidationError("route convergence state key is inconsistent")
                delta = receipt.get("progress_delta")
                classification = (
                    delta.get("classification") if isinstance(delta, Mapping) else None
                )
                if (
                    not isinstance(delta, Mapping)
                    or delta.get("schema") != "pcbdraft-progress-delta"
                    or delta.get("version") != 1
                    or classification
                    not in {item.value for item in ProgressClassification}
                ):
                    raise ValidationError(
                        "route convergence progress evidence is malformed"
                    )
                failure = receipt.get("routing_failure")
                retry_key = (
                    _routing_failure_retry_key(failure) if failure is not None else None
                )
                observations.append(
                    ConvergenceObservation(
                        retained_state,
                        ProgressClassification(classification),
                        retry_key,
                    )
                )
                if retained_state == state_key and retry_key is not None:
                    matching_retry_key = retry_key
        except ValidationError:
            return ConvergenceDecision(
                False,
                "strategy_change_required",
                "convergence_history_invalid",
                0,
                0,
            )
        return evaluate_convergence(
            tuple(observations),
            state_key=state_key,
            retry_key=matching_retry_key,
            policy=DEFAULT_CONVERGENCE_POLICY,
        )

    def _current_progress_and_stage(
        self, project: ApplicationProject
    ) -> tuple[ProgressVector, StageProjection]:
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            revision = int(project.state["design_revision"])
            return (
                ProgressVector.unknown(revision),
                StageProjection(
                    EngineeringStage.NOT_STARTED,
                    False,
                    ("requirements_not_frozen",),
                ),
            )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        revision = int(project.state["design_revision"])
        progress, stage, _consistency = self._managed_progress_and_stage(
            project,
            managed,
            revision,
        )
        return progress, stage

    def inspect_engineering_stage(self, project_id: str) -> dict[str, Any]:
        """Return the evidence-derived stage bound to both live revisions.

        This internal adapter surface exists so provider schema projection can
        cache a stage only while the project and design revisions are unchanged.
        It deliberately returns no model-selectable stage input.  The evidence
        identity is the bounded retained validation run id, not an additional
        cryptographic audit digest.
        """

        project = self._open(project_id)
        _progress, stage = self._current_progress_and_stage(project)
        retained_validation = project.state.get("last_validation")
        validation_run_id = (
            retained_validation.get("run_id")
            if isinstance(retained_validation, Mapping)
            else None
        )
        evidence_source = (
            f"validation-run:{validation_run_id}"
            if isinstance(validation_run_id, str)
            and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", validation_run_id)
            else "validation-run:none"
        )
        return {
            "project_id": project_id,
            "live_revision": int(project.state["revision"]),
            "design_revision": int(project.state["design_revision"]),
            "evidence_source": evidence_source,
            **stage.to_dict(),
        }

    def _managed_progress_and_stage(
        self,
        project: ApplicationProject,
        managed: Any,
        revision: int,
        *,
        validation_root: Path | None = None,
        include_routing_failures: bool = True,
    ) -> tuple[ProgressVector, StageProjection, NativeConsistencyReport | None]:
        """Project revision progress for either the live or one staged tree."""

        graph = managed.graph.with_footprint_overrides(managed.design)
        consistency, board = self._native_progress_sources(managed, revision, graph)
        if validation_root is None:
            erc, _unused, erc_check = self._retained_check_progress(
                project, managed.design.content_hash(), "run_erc", revision
            )
            drc, fatal, drc_check = self._retained_check_progress(
                project, managed.design.content_hash(), "run_drc", revision
            )
        else:
            erc, _unused, erc_check = self._aggregate_check_progress(
                validation_root, managed.design.content_hash(), "run_erc", revision
            )
            drc, fatal, drc_check = self._aggregate_check_progress(
                validation_root, managed.design.content_hash(), "run_drc", revision
            )
        progress = _progress_vector(
            managed.design,
            graph,
            revision,
            consistency=consistency,
            board=board,
            fatal_drc=fatal,
            error_drc=drc,
            erc_error=erc,
            routing_failure_count=(
                self._routing_failure_count(project, revision, None)
                if include_routing_failures
                else 0
            ),
        )
        stage = derive_stage(
            progress,
            _progress_stage_evidence(
                managed.design,
                revision,
                requirements_frozen=managed.requirements_path.is_file(),
                consistency=consistency,
                progress=progress,
                erc_check=erc_check,
                drc_check=drc_check,
            ),
        )
        return progress, stage, consistency

    @staticmethod
    def _require_current_native_consistency(
        report: NativeConsistencyReport | None,
        revision: int,
        *,
        label: str,
    ) -> NativeConsistencyReport:
        if (
            report is None
            or report.candidate_revision != revision
            or report.schematic_status != "evaluated"
            or report.board_status != "evaluated"
            or not report.consistency_passed
        ):
            raise _PCBOperationPostconditionError(
                "native_consistency_failed",
                f"legacy modification {label} native consistency is unavailable or failing",
            )
        return report

    def record_product_session_terminal(
        self,
        project_id: str,
        *,
        session_id: str,
        turn_id: str,
        process_status: ProcessStatus | str,
        termination_reason: str | None = None,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """Write the one PCB-level outcome shared by durable and Hermes sessions."""

        try:
            process = ProcessStatus(process_status)
        except ValueError as exc:
            raise ValidationError("product session process status is invalid") from exc
        resolved_receipt_id = (
            validate_product_terminal_receipt_id(receipt_id)
            if receipt_id is not None
            else product_terminal_receipt_id(session_id, turn_id)
        )
        project = self._open(project_id)
        with ResourceLock(project.root, self.locks_root):
            project = self._open(project_id)
            existing_path = (
                project.root / "product-sessions" / f"{resolved_receipt_id}.json"
            )
            if existing_path.is_file() and not existing_path.is_symlink():
                existing = ProductSessionTerminalReceipt.from_dict(
                    load_json_limited(existing_path, APP_FILE_LIMIT)
                )
                if (
                    existing.project_id != project_id
                    or existing.session_id != session_id
                    or existing.turn_id != turn_id
                ):
                    raise ValidationError(
                        "product session receipt identity is already bound"
                    )
                retained_stage = StageProjection(
                    existing.stage_reached,
                    existing.release_gate_passed,
                    () if existing.release_gate_passed else ("retained_terminal",),
                )
                requested_outcome, requested_termination = terminal_outcome(
                    process_status=process,
                    requested_reason=termination_reason,
                    stage=retained_stage,
                )
                if (
                    existing.process_status is not process
                    or existing.task_outcome is not requested_outcome
                    or existing.termination_reason != requested_termination
                ):
                    raise ValidationError(
                        "product session terminal receipt facts conflict"
                    )
                result = existing.to_dict()
                result["artifact"] = existing_path.relative_to(project.root).as_posix()
                return result
            progress, stage = self._current_progress_and_stage(project)
            outcome, reason = terminal_outcome(
                process_status=process,
                requested_reason=termination_reason,
                stage=stage,
            )
            receipt = ProductSessionTerminalReceipt(
                resolved_receipt_id,
                project_id,
                session_id,
                turn_id,
                utc_timestamp(),
                process,
                outcome,
                reason,
                stage.stage,
                stage.release_gate_passed,
                progress.source_revision,
                progress,
            )
            path = store_product_session_terminal(project.root, receipt)
            result = receipt.to_dict()
            result["artifact"] = path.relative_to(project.root).as_posix()
            return result

    def apply_pcb_operation(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Stage, materialize, verify, and publish exactly one typed operation."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=tool_name
        )
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized design to modify")
        authoritative = open_managed_project(project.design_root)
        authoritative.assert_synchronized()
        before_graph = authoritative.graph.with_footprint_overrides(
            authoritative.design
        )
        operations = self._flat_semantic_operations(
            tool_name,
            arguments,
            authoritative.design,
            graph=before_graph,
        )
        change_set = ChangeSet.from_dict(
            {
                "schema": "pcbdraft-change-set",
                "version": 1,
                "id": f"flat_{secrets.token_hex(6)}",
                "base_hash": authoritative.design.content_hash(),
                "intent": f"Apply concrete PCB operation {tool_name}.",
                "actor": "flat-pcb-toolbox",
                "operations": operations,
                "provenance": [f"tool:{tool_name}"],
            }
        )
        candidate = apply_change_set(authoritative.design, change_set)
        request = load_generation_request(authoritative.requirements_path)
        graph = authoritative.graph.with_footprint_overrides(candidate)
        if isinstance(request, AgentDesignRequest):
            qualification = qualify_components(candidate, graph)
            if qualification.pad_mapping_failures:
                raise ValidationError(
                    "component qualification contains invalid local pad mappings"
                )
            document = candidate.to_dict()
            document["metadata"]["component_qualification_schema"] = (
                COMPONENT_QUALIFICATION_SCHEMA
            )
            document["metadata"]["component_qualification_hash"] = (
                qualification.sha256()
            )
            candidate = Design.from_dict(document)
        candidate_diff = semantic_diff(authoritative.design, candidate)
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        route_net_id = str(arguments["net_id"]) if tool_name == "route_net" else None
        baseline_design_revision = int(project.state["design_revision"])
        candidate_design_revision = baseline_design_revision + 1
        convergence_state_key = (
            _route_state_key(
                authoritative.design,
                route_net_id,
                baseline_design_revision,
            )
            if route_net_id is not None
            else f"revision={baseline_design_revision}"
        )
        convergence_state = (
            _route_state_record(
                authoritative.design,
                route_net_id,
                baseline_design_revision,
            )
            if route_net_id is not None
            else None
        )
        before_consistency, before_board = self._native_progress_sources(
            authoritative, baseline_design_revision, before_graph
        )
        before_erc, _unused_fatal, before_erc_check = self._retained_check_progress(
            project,
            authoritative.design.content_hash(),
            "run_erc",
            baseline_design_revision,
        )
        before_drc_metric, before_fatal_drc, before_drc_check = (
            self._retained_check_progress(
                project,
                authoritative.design.content_hash(),
                "run_drc",
                baseline_design_revision,
            )
        )
        before_progress = _progress_vector(
            authoritative.design,
            before_graph,
            baseline_design_revision,
            consistency=before_consistency,
            board=before_board,
            fatal_drc=before_fatal_drc,
            error_drc=before_drc_metric,
            erc_error=before_erc,
            routing_failure_count=self._routing_failure_count(
                project, baseline_design_revision, convergence_state_key
            ),
        )
        before_stage_evidence = _progress_stage_evidence(
            authoritative.design,
            baseline_design_revision,
            requirements_frozen=authoritative.requirements_path.is_file(),
            consistency=before_consistency,
            progress=before_progress,
            erc_check=before_erc_check,
            drc_check=before_drc_check,
        )
        before_stage = derive_stage(before_progress, before_stage_evidence)
        convergence = (
            self._route_convergence_decision(project, state_key=convergence_state_key)
            if route_net_id is not None
            else ConvergenceDecision(True, "continue", None, 0, 0)
        )
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-flat-operation-receipt",
            "version": 2,
            "status": "preparing",
            "operation": tool_name,
            "created_at": utc_timestamp(),
            "before_hash": authoritative.design.content_hash(),
            "after_hash": candidate.content_hash(),
            "baseline_revision": expected_revision,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": candidate_design_revision,
            "committed_revision": None,
            "committed_design_revision": None,
            "consistency_passed": False,
            "postconditions": [],
            "rollback_performed": False,
            "rollback": {
                "state": "not_required",
                "performed": False,
                "live_unchanged": True,
            },
            "artifact": {
                "transaction_id": transaction_id,
                "receipt": "receipt.json",
            },
            "convergence_state_key": convergence_state_key,
            "convergence_state": convergence_state,
            "convergence": convergence.to_dict(),
            "transaction_scope": {
                "kind": tool_name,
                "entry_count": len(operations),
            },
        }
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        atomic_write_json(receipt_path, receipt)
        atomic_write_json(transaction / "semantic-diff.json", candidate_diff)
        stage = "materialization"
        try:
            if not convergence.allowed:
                raise _PCBOperationPostconditionError(
                    convergence.action,
                    "route retry stopped until placement, layer, order, or revision state changes",
                )
            _reject_stale_copper_transform(
                tool_name,
                arguments,
                authoritative.design,
                candidate,
                before_graph=before_graph,
                candidate_graph=graph,
            )
            generated = materialize_managed_design(
                request,
                candidate,
                staged,
                graph=graph,
                plan=authoritative.plan,
                retain_failed_attempt=transaction / "failed-native",
                lock_timeout=min(10.0, timeout),
                auto_place=False,
                route_net_ids=(
                    frozenset({route_net_id})
                    if route_net_id is not None
                    else frozenset()
                ),
                allow_incomplete=True,
            )
            if tool_name == "route_net":
                stage = "routing_probe"
                candidate = self._retain_generated_route(
                    candidate,
                    route_net_id or "",
                    generated.pcb.routing,
                )
                candidate_diff = semantic_diff(authoritative.design, candidate)
                receipt["after_hash"] = candidate.content_hash()
                atomic_write_json(receipt_path, receipt)
                atomic_write_json(transaction / "semantic-diff.json", candidate_diff)
                os.replace(staged, transaction / "routing-probe")
                stage = "routing_commit"
                materialize_managed_design(
                    request,
                    candidate,
                    staged,
                    graph=graph,
                    plan=authoritative.plan,
                    retain_failed_attempt=transaction / "failed-native-final",
                    lock_timeout=min(10.0, timeout),
                    auto_place=False,
                    route_net_ids=frozenset(),
                    allow_incomplete=True,
                )
            staged_project = open_managed_project(staged)
            staged_project.assert_synchronized()
            if staged_project.design.content_hash() != candidate.content_hash():
                raise ValidationError("staged semantic design hash changed")
            stage = "native_consistency"
            consistency_failure: PCBDraftError | None = None
            try:
                consistency = inspect_native_consistency(
                    candidate,
                    staged_project.schematic_path,
                    staged_project.board_path,
                    candidate_revision=candidate_design_revision,
                    graph=graph,
                    require_routed_net_ids=(
                        frozenset({route_net_id})
                        if route_net_id is not None
                        else frozenset()
                    ),
                )
            except PCBDraftError as exc:
                consistency_failure = exc
                consistency = _unavailable_consistency_report(candidate_design_revision)
            atomic_write_json(
                transaction / "native-consistency.json", consistency.to_dict()
            )
            receipt["artifact"]["native_consistency"] = "native-consistency.json"
            receipt["consistency_passed"] = consistency.consistency_passed
            receipt["postconditions"] = _native_postconditions(tool_name, consistency)
            receipt["native_delta"] = {
                "mismatch_count": len(consistency.mismatches),
                "required_routed_net": route_net_id,
            }
            receipt["intended_delta"] = candidate_diff["summary"]
            try:
                candidate_board = _native_board_projection(staged_project)
            except PCBDraftError:
                candidate_board = None
            after_progress = _progress_vector(
                candidate,
                graph,
                candidate_design_revision,
                consistency=consistency,
                board=candidate_board,
                fatal_drc=before_progress.fatal_drc_count.for_revision(
                    candidate_design_revision
                ),
                error_drc=before_progress.error_drc_count.for_revision(
                    candidate_design_revision
                ),
                erc_error=before_progress.erc_error_count.for_revision(
                    candidate_design_revision
                ),
                routing_failure_count=0,
            )
            after_stage_evidence = _progress_stage_evidence(
                candidate,
                candidate_design_revision,
                requirements_frozen=staged_project.requirements_path.is_file(),
                consistency=consistency,
                progress=after_progress,
                erc_check=before_erc_check.for_revision(candidate_design_revision),
                drc_check=before_drc_check.for_revision(candidate_design_revision),
            )
            after_stage = derive_stage(after_progress, after_stage_evidence)
            _attach_progress(
                receipt,
                before_progress,
                after_progress,
                before_stage,
                after_stage,
            )
            atomic_write_json(receipt_path, receipt)
            if consistency_failure is not None:
                if tool_name == "route_net":
                    failure = RoutingFailure(
                        code="native_commit_failed",
                        net=route_net_id or "unknown",
                        blocking_summary="native KiCad inspection failed",
                        recommendations=("inspect_native_artifact",),
                        nearest_obstacle_class="native_artifact",
                        state_revision=candidate_design_revision,
                        state_context=_routing_failure_context(candidate, route_net_id),
                    )
                    raise _PCBOperationPostconditionError(
                        failure.code,
                        failure.diagnostic,
                        routing_failure=failure,
                    ) from consistency_failure
                raise _PCBOperationPostconditionError(
                    "native_verification_failed",
                    "native KiCad inspection failed",
                ) from consistency_failure
            if not consistency.consistency_passed:
                raise _consistency_rejection(
                    tool_name,
                    route_net_id,
                    consistency,
                    design=candidate,
                    state_revision=candidate_design_revision,
                )
            if tool_name in _NATIVE_DELTA_OPERATIONS:
                stage = "native_delta"
                native_operation_delta = compare_native_operation_delta(
                    tool_name,
                    arguments,
                    authoritative.design,
                    candidate,
                    before_board or _native_board_projection(authoritative),
                    candidate_board or _native_board_projection(staged_project),
                    before_schematic=_native_schematic_projection(authoritative),
                    after_schematic=_native_schematic_projection(staged_project),
                    graph=graph,
                )
                atomic_write_json(
                    transaction / "native-operation-delta.json",
                    native_operation_delta.to_dict(),
                )
                receipt["artifact"]["native_delta"] = "native-operation-delta.json"
                receipt["native_delta"].update(
                    {
                        "operation_checked": True,
                        "policy": native_operation_delta.policy,
                        "passed": native_operation_delta.passed,
                        "failed_checks": [
                            item.name
                            for item in native_operation_delta.checks
                            if not item.passed
                        ][:4],
                    }
                )
                receipt["postconditions"].extend(
                    _native_delta_postconditions(native_operation_delta)
                )
                atomic_write_json(receipt_path, receipt)
                if not native_operation_delta.passed:
                    error_code = (
                        "native_commit_failed"
                        if tool_name == "route_net"
                        else "native_delta_failed"
                    )
                    raise _PCBOperationPostconditionError(
                        error_code,
                        "native KiCad operation delta postcondition failed: "
                        + ", ".join(
                            item.name
                            for item in native_operation_delta.checks
                            if not item.passed
                        ),
                    )
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage=stage, tool_name=tool_name
            )
            receipt["rollback_performed"] = False
            receipt["rollback"] = {
                "state": "not_required",
                "performed": False,
                "live_unchanged": True,
            }
            if isinstance(exc, RoutingFailureError):
                receipt["routing_failure"] = exc.failure.to_dict()
            elif (
                isinstance(exc, _PCBOperationPostconditionError)
                and exc.routing_failure is not None
            ):
                receipt["routing_failure"] = exc.routing_failure.to_dict()
            elif (
                tool_name == "route_net"
                and receipt["error_code"] in ROUTING_FAILURE_CODES
            ):
                receipt["routing_failure"] = RoutingFailure(
                    code=receipt["error_code"],
                    net=route_net_id or "unknown",
                    blocking_summary=f"route transaction failed during {stage}",
                    recommendations=("inspect_native_artifact",),
                    nearest_obstacle_class="native_artifact",
                    state_revision=candidate_design_revision,
                    state_context=_routing_failure_context(candidate, route_net_id),
                ).to_dict()
            live_after_progress = before_progress
            if tool_name == "route_net" and isinstance(
                receipt.get("routing_failure"), Mapping
            ):
                failures = before_progress.routing_failure_count
                current_failures = (
                    failures.value
                    if failures.is_current(baseline_design_revision)
                    and failures.value is not None
                    else 0
                )
                live_after_progress = before_progress.replace_metric(
                    "routing_failure_count",
                    MetricValue.known(current_failures + 1, baseline_design_revision),
                )
            _attach_progress(
                receipt,
                before_progress,
                live_after_progress,
                before_stage,
                before_stage,
            )
            atomic_write_json(receipt_path, receipt)
            _bind_transaction_failure(exc, transaction_id)
            raise

        stage = "publication"
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            moved_before = False
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while PCB operation was staged"
                    )
                current_design = open_managed_project(current.design_root)
                current_design.assert_synchronized()
                if current_design.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed before publication"
                    )
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                published = open_managed_project(current.design_root)
                published.assert_synchronized()
                current.state["status"] = "generated"
                current.state["revision"] += 1
                current.state["design_revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                current.state["last_validation"] = None
                current.state["last_preview"] = None
                current.state["last_release"] = None
                current.state["last_transaction"] = transaction_id
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "pcb.operation_applied",
                    f"Applied concrete PCB operation {tool_name}",
                )
                self._write_records(current.root, current.state, current.conversation)
                # The applied receipt is the final publication write.  If it
                # fails, the surrounding handler can still restore the native
                # design, event, and project records without ever retaining a
                # durable success receipt for the rolled-back transaction.
                receipt.update(
                    {
                        "status": "applied",
                        "applied_at": utc_timestamp(),
                        "committed_revision": current.state["revision"],
                        "committed_design_revision": current.state["design_revision"],
                        "rollback": {
                            "state": "committed",
                            "performed": False,
                            "live_unchanged": False,
                        },
                    }
                )
                atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        failed_published = transaction / "failed-published"
                        if current.design_root.exists():
                            os.replace(current.design_root, failed_published)
                        if before.exists():
                            os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                    rollback_failures.append(rollback_exc)
                receipt["status"] = "failed"
                receipt["failed_at"] = utc_timestamp()
                receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
                receipt.pop("applied_at", None)
                receipt["committed_revision"] = None
                receipt["committed_design_revision"] = None
                receipt["error_code"] = _operation_failure_code(
                    exc, stage=stage, tool_name=tool_name
                )
                receipt["rollback_performed"] = moved_before and not rollback_failures
                receipt["rollback"] = {
                    "state": (
                        "restored"
                        if moved_before and not rollback_failures
                        else "incomplete"
                        if rollback_failures
                        else "not_required"
                    ),
                    "performed": moved_before and not rollback_failures,
                    "live_unchanged": not rollback_failures,
                }
                if not rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(baseline_design_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    # Preserve the publication failure; an applied receipt was
                    # never authoritative without the matching project record.
                    pass
                if rollback_failures:
                    publication_failure = PCBDraftError(
                        "PCB operation publication failed and rollback was incomplete"
                    )
                    _bind_transaction_failure(publication_failure, transaction_id)
                    raise publication_failure from exc
                _bind_transaction_failure(exc, transaction_id)
                raise
        result = {
            "operation": tool_name,
            "transaction_id": transaction_id,
            "before_hash": receipt["before_hash"],
            "after_hash": receipt["after_hash"],
            "design_revision": current.state["design_revision"],
            "revision": current.state["revision"],
            "changed": candidate_diff["summary"],
            "synchronized": True,
            "consistency_passed": receipt["consistency_passed"],
            "postconditions": receipt["postconditions"],
            "transaction_artifact": transaction_id,
            "progress_before": receipt["progress_before"],
            "progress_after": receipt["progress_after"],
            "progress_delta": receipt["progress_delta"],
            "stage": receipt["stage_after"],
            "convergence": receipt["convergence"],
            "transaction_scope": receipt["transaction_scope"],
        }
        return self._with_tool_result(self.open_project(project_id), result)

    @staticmethod
    def _retain_generated_route(design: Design, net_id: str, routing: Any) -> Design:
        """Promote generated copper for one net into deterministic native intent."""

        net = next((item for item in design.nets if item.id == net_id), None)
        if net is None:
            raise ValidationError(f"net is absent: {net_id}")
        if net.name in routing.unrouted:
            failures = tuple(getattr(routing, "failures", ()))
            failure = next(
                (item for item in failures if item.net == net.name),
                RoutingFailure(
                    code="no_legal_channel",
                    net=net.name,
                    expanded_nodes=int(getattr(routing, "expanded_nodes", 0)),
                    blocking_summary="router could not complete the selected net",
                    recommendations=("change_layer", "reposition_component"),
                    nearest_obstacle_class="unknown",
                    state_revision=design.native_intent.geometry_revision,
                    state_context=_routing_failure_context(design, net_id),
                ),
            )
            raise RoutingFailureError(failure)
        segments = [item for item in routing.segments if item.net == net.name]
        if len(net.endpoints) > 1 and not segments:
            raise RoutingFailureError(
                RoutingFailure(
                    code="native_commit_failed",
                    net=net.name,
                    expanded_nodes=int(getattr(routing, "expanded_nodes", 0)),
                    blocking_summary="router completed without materializable copper",
                    recommendations=("inspect_pad_escape",),
                    nearest_obstacle_class="native_artifact",
                    state_revision=design.native_intent.geometry_revision,
                    state_context=_routing_failure_context(design, net_id),
                )
            )
        document = design.to_dict()
        native = document["native_intent"]

        def stable_id(prefix: str, value: dict[str, Any]) -> str:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:20]}"

        native["routes"] = [
            item for item in native["routes"] if item.get("net") != net_id
        ] + [
            {
                "id": stable_id(
                    "route",
                    {
                        "net": net_id,
                        "layer": item.layer,
                        "x1_mm": item.x1_mm,
                        "y1_mm": item.y1_mm,
                        "x2_mm": item.x2_mm,
                        "y2_mm": item.y2_mm,
                        "width_mm": item.width_mm,
                    },
                ),
                "net": net_id,
                "layer": item.layer,
                "x1_mm": item.x1_mm,
                "y1_mm": item.y1_mm,
                "x2_mm": item.x2_mm,
                "y2_mm": item.y2_mm,
                "width_mm": item.width_mm,
            }
            for item in segments
        ]
        native["vias"] = [
            item for item in native["vias"] if item.get("net") != net_id
        ] + [
            {
                "id": stable_id(
                    "via",
                    {
                        "net": net_id,
                        "x_mm": item.x_mm,
                        "y_mm": item.y_mm,
                        "diameter_mm": item.diameter_mm,
                        "drill_mm": item.drill_mm,
                        "from_layer": item.from_layer,
                        "to_layer": item.to_layer,
                    },
                ),
                "net": net_id,
                "x_mm": item.x_mm,
                "y_mm": item.y_mm,
                "diameter_mm": item.diameter_mm,
                "drill_mm": item.drill_mm,
                "from_layer": item.from_layer,
                "to_layer": item.to_layer,
            }
            for item in routing.vias
            if item.net == net.name
        ]
        return Design.from_dict(document)

    @staticmethod
    def _entry_mapping(value: Any, field: str) -> dict[str, Any]:
        """Convert one strict list-of-entries object into a unique mapping."""

        if not isinstance(value, dict) or set(value) != {"entries"}:
            raise ValidationError(f"{field} has an invalid object shape")
        entries = value["entries"]
        if not isinstance(entries, list) or not entries:
            raise ValidationError(f"{field}.entries must be a non-empty array")
        result: dict[str, Any] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"field", "value"}:
                raise ValidationError(f"{field}.entries[{index}] is malformed")
            name = entry["field"]
            if not isinstance(name, str) or not name or name in result:
                raise ValidationError(f"{field} contains a duplicate or invalid field")
            result[name] = entry["value"]
        return result

    @staticmethod
    def _parameter_mapping(value: Any, field: str) -> dict[str, Any]:
        """Convert strict named parameter entries into a domain mapping."""

        if not isinstance(value, list):
            raise ValidationError(f"{field} must be an array")
        result: dict[str, Any] = {}
        for index, entry in enumerate(value):
            if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
                raise ValidationError(f"{field}[{index}] is malformed")
            name = entry["name"]
            if not isinstance(name, str) or not name or name in result:
                raise ValidationError(f"{field} contains a duplicate or invalid name")
            result[name] = entry["value"]
        return result

    @classmethod
    def _flat_semantic_operations(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        design: Design,
        *,
        graph: PartGraph,
    ) -> list[dict[str, Any]]:
        """Normalize one concrete tool into one atomic semantic change set."""

        if tool_name == "connect_group":
            connection_entries = parse_connect_group(arguments["connections"])
            cls._validate_connect_group(connection_entries, design, graph)
            return [
                cls._flat_semantic_operation(
                    "connect_pin", entry.to_tool_arguments(), design
                )
                for entry in connection_entries
            ]
        if tool_name == "place_group":
            placement_entries = parse_place_group(arguments["placements"])
            cls._validate_place_group(placement_entries, design, graph)
            return [
                cls._flat_semantic_operation(
                    "place_footprint", entry.to_tool_arguments(), design
                )
                for entry in placement_entries
            ]
        return [cls._flat_semantic_operation(tool_name, arguments, design)]

    @staticmethod
    def _validate_connect_group(
        entries: tuple[ConnectGroupEntry, ...],
        design: Design,
        graph: PartGraph,
    ) -> None:
        """Resolve every group endpoint and conflict before creating a candidate."""

        components = {item.id: item for item in design.components}
        nets = {item.id: item for item in design.nets}
        connected = {
            (endpoint.component, endpoint.pin): (net.id, endpoint.role)
            for net in design.nets
            for endpoint in net.endpoints
        }
        for entry in entries:
            net = nets.get(entry.net_id)
            if net is None:
                raise ValidationError(
                    "semantic_transaction_invalid_net: connect_group references "
                    f"absent net {entry.net_id}"
                )
            component = components.get(entry.component_id)
            if component is None:
                raise ValidationError(
                    "semantic_transaction_invalid_endpoint: connect_group references "
                    f"absent component {entry.component_id}"
                )
            part = graph.get(component.part_id)
            if part.pin(entry.pin) is None:
                raise ValidationError(
                    "semantic_transaction_invalid_endpoint: connect_group references "
                    f"absent pin {entry.component_id}.{entry.pin}"
                )
            prior = connected.get((entry.component_id, entry.pin))
            if prior == (entry.net_id, entry.role):
                raise ValidationError(
                    "semantic_transaction_duplicate: connect_group endpoint is already connected: "
                    f"{entry.component_id}.{entry.pin}"
                )
            if prior is not None:
                raise ValidationError(
                    "semantic_transaction_conflict: connect_group endpoint is already "
                    f"connected to {prior[0]} as {prior[1]}: "
                    f"{entry.component_id}.{entry.pin}"
                )

    @staticmethod
    def _validate_place_group(
        entries: tuple[PlaceGroupEntry, ...],
        design: Design,
        graph: PartGraph,
    ) -> None:
        """Resolve every absolute pose, board bound, and copper conflict first."""

        components = {item.id: item for item in design.components}
        for entry in entries:
            component = components.get(entry.component_id)
            if component is None:
                raise ValidationError(
                    "semantic_transaction_invalid_component: place_group references "
                    f"absent component {entry.component_id}"
                )
            part = graph.get(component.part_id)
            if component.attributes.get("exclude_from_board", False) or (
                part.footprint is None
            ):
                raise ValidationError(
                    "semantic_transaction_invalid_component: place_group component "
                    f"has no board footprint: {entry.component_id}"
                )
            if not (
                0.0 <= entry.x_mm <= design.board.width_mm
                and 0.0 <= entry.y_mm <= design.board.height_mm
            ):
                raise ValidationError(
                    "semantic_transaction_out_of_bounds: place_group pose lies outside "
                    f"the board: {entry.component_id}"
                )
            existing = component.placement
            if existing is not None and (
                existing.x_mm,
                existing.y_mm,
                existing.rotation_deg,
                existing.side,
            ) == (
                entry.x_mm,
                entry.y_mm,
                entry.rotation_deg,
                entry.side,
            ):
                raise ValidationError(
                    "semantic_transaction_duplicate: place_group pose is already applied: "
                    f"{entry.component_id}"
                )
            routed_nets = _routed_component_nets(design, entry.component_id)
            if routed_nets:
                raise ValidationError(
                    "semantic_transaction_conflict: place_group component retains routed "
                    f"copper on {', '.join(routed_nets)}: {entry.component_id}"
                )

    @classmethod
    def _flat_semantic_operation(
        cls, tool_name: str, arguments: dict[str, Any], design: Design
    ) -> dict[str, Any]:
        operation_id = f"op_{secrets.token_hex(6)}"
        args: dict[str, Any]
        expected: dict[str, Any] = {}
        op = tool_name
        if tool_name.startswith("add_") and tool_name in {
            "add_block",
            "add_component",
            "add_net",
        }:
            value = copy.deepcopy(arguments["value"])
            if tool_name == "add_block":
                value["components"] = []
                value["provenance"] = []
            elif tool_name == "add_net":
                value["endpoints"] = []
            args = {"value": value}
        elif tool_name in {"remove_block", "remove_component", "remove_net"}:
            key = tool_name.removeprefix("remove_") + "_id"
            if tool_name == "remove_net":
                net = next(
                    (item for item in design.nets if item.id == arguments[key]),
                    None,
                )
                if net is not None and net.name.upper() in {"GND", "GROUND", "VSS"}:
                    raise ValidationError(
                        "the required reference-plane net cannot be removed"
                    )
            args = {"id": arguments[key]}
        elif tool_name == "update_component":
            args = {
                "component_id": arguments["component_id"],
                "changes": cls._entry_mapping(arguments["changes"], "changes"),
            }
        elif tool_name in {"assign_footprint", "rename_net"}:
            args = dict(arguments)
        elif tool_name in {"connect_pin", "disconnect_pin"}:
            op = "connect" if tool_name == "connect_pin" else "disconnect"
            args = {
                "net_id": arguments["net_id"],
                "endpoint": {
                    "component": arguments["component_id"],
                    "pin": arguments["pin"],
                    "role": arguments["role"],
                },
            }
        elif any(
            tool_name.endswith(suffix)
            for suffix in ("power_domain", "interface", "constraint")
        ):
            collection = next(
                name
                for name in ("power_domain", "interface", "constraint")
                if tool_name.endswith(name)
            )
            if tool_name.startswith("remove_"):
                op = f"remove_{collection}"
                args = {"id": arguments["id"]}
            else:
                op = (
                    "upsert_constraint"
                    if collection == "constraint"
                    else f"upsert_{collection}"
                )
                value = copy.deepcopy(arguments["value"])
                if collection in {"interface", "constraint"}:
                    value["params"] = cls._parameter_mapping(
                        value["params"], f"{collection}.params"
                    )
                if collection == "constraint":
                    value["provenance"] = []
                entry_id = str(value["id"])
                exists = (
                    any(item.id == entry_id for item in design.power_domains)
                    if collection == "power_domain"
                    else any(item.id == entry_id for item in design.interfaces)
                    if collection == "interface"
                    else any(item.id == entry_id for item in design.constraints)
                )
                if tool_name.startswith("add_"):
                    if exists:
                        raise ValidationError(
                            f"cannot add existing {collection}: {entry_id}"
                        )
                    expected = {"absent": True}
                else:
                    if not exists:
                        raise ValidationError(
                            f"cannot update absent {collection}: {entry_id}"
                        )
                    expected = {"id": entry_id}
                args = {"value": value}
        elif tool_name == "update_board_rules":
            op = "update_board"
            args = {"changes": cls._entry_mapping(arguments["changes"], "changes")}
        elif tool_name == "set_board_outline":
            args = {
                "width_mm": float(arguments["width_mm"]),
                "height_mm": float(arguments["height_mm"]),
            }
        elif tool_name in {"place_footprint", "move_footprint", "rotate_footprint"}:
            op = "place_footprint"
            component_id = str(arguments["component_id"])
            component = next(
                (item for item in design.components if item.id == component_id), None
            )
            if component is None:
                raise ValidationError(f"component is absent: {component_id}")
            existing = component.placement
            args = {
                "component_id": component_id,
                "x_mm": float(
                    arguments.get("x_mm", existing.x_mm if existing else 0.0)
                ),
                "y_mm": float(
                    arguments.get("y_mm", existing.y_mm if existing else 0.0)
                ),
                "rotation_deg": float(
                    arguments.get(
                        "rotation_deg", existing.rotation_deg if existing else 0.0
                    )
                ),
                "side": str(
                    arguments.get("side", existing.side if existing else "front")
                ),
                "fixed": True,
            }
        elif tool_name == "unplace_footprint":
            args = {"component_id": arguments["component_id"]}
        elif tool_name in {"route_net", "unroute_net"}:
            net = next(
                (item for item in design.nets if item.id == arguments["net_id"]),
                None,
            )
            if net is None:
                raise ValidationError(f"net is absent: {arguments['net_id']}")
            if tool_name == "unroute_net" and net.name.upper() in {
                "GND",
                "GROUND",
                "VSS",
            }:
                raise ValidationError(
                    "the required reference-plane net cannot be unrouted"
                )
            args = {"net_id": arguments["net_id"]}
            if tool_name == "route_net":
                args.update({"segments": [], "vias": []})
        elif tool_name == "add_via":
            args = {
                "value": {
                    "id": arguments["via_id"],
                    "net": arguments["net_id"],
                    "x_mm": float(arguments["x_mm"]),
                    "y_mm": float(arguments["y_mm"]),
                    "diameter_mm": float(arguments["diameter_mm"]),
                    "drill_mm": float(arguments["drill_mm"]),
                    "from_layer": int(arguments["from_layer"]),
                    "to_layer": int(arguments["to_layer"]),
                }
            }
        elif tool_name == "remove_via":
            args = {"via_id": arguments["via_id"]}
        else:
            raise ValidationError(f"flat PCB write is not implemented: {tool_name}")
        return {
            "id": operation_id,
            "op": op,
            "args": args,
            "expected": expected,
            "reason": f"Concrete flat PCB tool {tool_name}.",
        }

    def record_progress(
        self,
        project_id: str,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        """Append a structured adapter/job event under the project write lock."""

        project = self._open(project_id)
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", kind):
            raise ValidationError("structured event kind is invalid")
        if level not in {"info", "warning", "error"}:
            raise ValidationError("structured event level is invalid")
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            # Progress events are presentation/audit state, not an engineering
            # mutation. Advancing the project revision here would immediately
            # stale a revision-bound tool call merely because the UI recorded
            # ``job.started`` or ``job.complete``.
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                kind,
                _safe_text(message, "event message", limit=4096),
                level=level,
            )
            self._write_records(current.root, current.state, current.conversation)

    def reply_message(
        self,
        project_id: str,
        text: str,
        *,
        turn_id: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        """Append one exactly-once conversational assistant reply.

        The reply is bound to its durable turn and sequence index: appending
        the same ``(turn_id, index)`` twice is a no-op, so a crash-resumed
        worker never duplicates a conversational message in the transcript.
        """

        clean = _sanitize_secret_text(
            _safe_text(text, "reply text", limit=MAX_USER_MESSAGE_BYTES)
        )
        project = self._open(project_id)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            conversation = current.conversation
            if turn_id is not None or index is not None:
                if (
                    not isinstance(turn_id, str)
                    or isinstance(index, bool)
                    or not (isinstance(index, int) and index >= 0)
                ):
                    raise ValidationError("reply delivery binding is invalid")
                for message in conversation["messages"]:
                    data = message.get("data") if isinstance(message, dict) else None
                    if not isinstance(data, dict):
                        continue
                    if data.get("turn_id") == turn_id and data.get("index") == index:
                        return self._public_project(current)
            self._append_message(
                conversation,
                "assistant",
                "reply",
                clean,
                data=(
                    {"turn_id": turn_id, "index": index}
                    if turn_id is not None and index is not None
                    else None
                ),
            )
            self._write_records(current.root, current.state, conversation)
            return self._public_project(current)

    def send_message(
        self,
        project_id: str,
        text: str,
        *,
        timeout: float = 420.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        clean = _sanitize_secret_text(
            _safe_text(text, "message", limit=MAX_USER_MESSAGE_BYTES)
        )
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="message preparation"
        )
        if project.state["status"] in _TRANSIENT_STATES:
            raise ValidationError("project already has a running operation")
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            return self.preview_modification(
                project_id,
                clean,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        prior = project.conversation.get("proposal")
        prior_decisions = prior if isinstance(prior, dict) else {}
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            state = current.state
            conversation = current.conversation
            if state["revision"] != expected_revision:
                raise ValidationError("project changed while preparing the message")
            self._append_message(conversation, "user", "request", clean)
            state["status"] = "interpreting"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._event(state, current.root, "provider.started", "Interpreting request")
            self._write_records(current.root, state, conversation)
            expected_revision = state["revision"]

        run_dir = project.root / "provider-runs" / new_run_id()
        try:
            if self.provider is None:
                raise PCBDraftError(
                    "no model provider configured; run `pcbdraft connect` or /connect to add "
                    "a model service before sending a planning request"
                )
            value = self.provider.interpret(
                ProviderContext(
                    request=clean,
                    project_name=project.state["name"],
                    prior_decisions=prior_decisions,
                ),
                project_dir=project.root,
                run_dir=run_dir,
                timeout=timeout,
            )
            proposal, agent_request = self._prepare_proposal(
                project_id, project.state["created_at"], value, prior_decisions, clean
            )
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "provider_error",
                "provider.failed",
                str(exc),
            )
            raise

        compilation = None
        planning_error: str | None = None
        if (
            agent_request is not None
            and not proposal["clarifications"]
            and proposal["scope"]["decision"] == "attempted"
        ):
            planner = getattr(self.provider, "plan", None)
            try:
                if not callable(planner) or not getattr(
                    self.provider, "supports_planning", True
                ):
                    raise PCBDraftError(
                        "the selected provider can interpret requirements but cannot produce a circuit plan"
                    )
                symbol_context = planner_symbol_context(agent_request)
                plan = planner(
                    agent_request,
                    symbol_context=symbol_context,
                    project_dir=project.root,
                    run_dir=project.root / "provider-runs" / new_run_id(),
                    timeout=timeout,
                )
                compilation = compile_agent_plan(agent_request, plan)
                proposal = self._attach_plan(proposal, compilation)
            except PCBDraftError as exc:
                planning_error = _sanitize_secret_text(str(exc))[:2048]
                proposal["planning"] = {
                    "state": "unavailable",
                    "message": planning_error,
                }

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the provider was running")
            state = current.state
            conversation = current.conversation
            conversation["proposal"] = proposal
            conversation["decisions"] = proposal.get("decisions", {})
            if compilation is not None:
                atomic_write_json(
                    current.root / PENDING_REQUEST_NAME,
                    compilation.request.to_dict(),
                )
                atomic_write_json(
                    current.root / PENDING_PLAN_NAME, compilation.plan.to_dict()
                )
                atomic_write_json(
                    current.root / PENDING_DESIGN_NAME, compilation.design.to_dict()
                )
                atomic_write_json(
                    current.root / PENDING_PARTS_NAME, compilation.graph.to_dict()
                )
            pending = proposal["clarifications"]
            attemptable = proposal["scope"]["decision"] == "attempted"
            state["status"] = (
                "generation_unavailable"
                if not attemptable
                else "needs_clarification"
                if pending
                else "planning_required"
                if compilation is None
                else "awaiting_confirmation"
            )
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            assistant_text = self._proposal_message(proposal)
            self._append_message(
                conversation,
                "assistant",
                "proposal" if attemptable and compilation is not None else "planning",
                assistant_text,
                data={
                    "status": state["status"],
                    "clarification_count": len(pending),
                    "planning_error": planning_error,
                },
            )
            self._event(
                state,
                current.root,
                (
                    "plan.ready"
                    if compilation is not None
                    else "planning.required"
                    if attemptable
                    else "generation.unavailable"
                ),
                assistant_text,
                level="warning" if planning_error else "info",
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool = True,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="generation confirmation"
        )
        if project.state["status"] not in {
            "awaiting_confirmation",
            "generation_failed",
            "interrupted",
            "generated",
        }:
            raise ValidationError("project is not awaiting generation confirmation")
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            open_managed_project(project.design_root).assert_synchronized()
            preview = self.generate_project_previews(
                project_id,
                timeout=timeout,
                expected_revision=expected_revision,
            )
            if validate:
                return self.validate_project(
                    project_id,
                    timeout=timeout,
                    expected_revision=int(preview["state"]["revision"]),
                )
            return preview
        request = AgentDesignRequest.from_dict(
            load_json_limited(project.root / PENDING_REQUEST_NAME, APP_FILE_LIMIT)
        )
        plan = CircuitPlan.from_dict(
            load_json_limited(project.root / PENDING_PLAN_NAME, APP_FILE_LIMIT)
        )
        design = Design.from_dict(
            load_json_limited(project.root / PENDING_DESIGN_NAME, APP_FILE_LIMIT)
        )
        graph = PartGraph.load(project.root / PENDING_PARTS_NAME)
        if design.design_id != request.design_id or plan.design_id != request.design_id:
            raise ValidationError(
                "pending request, plan, and semantic design identities differ"
            )
        graph.assert_design(
            design,
            check_libraries=True,
            allow_provisional=design.metadata.get("assurance") == "provisional",
        )
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before confirmation")
            if current.design_root.exists() or current.design_root.is_symlink():
                raise ValidationError("confirmed project already has a design")
            state = current.state
            state["status"] = "generating"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._event(
                state,
                current.root,
                "generation.started",
                "Generating native KiCad project",
            )
            self._write_records(current.root, state, current.conversation)
            expected_revision = state["revision"]
        attempt_dir: Path | None = None
        attempt_record: dict[str, Any] | None = None
        try:
            attempt_id = new_run_id()
            attempt_dir = make_directory(
                make_directory(project.root / "attempts") / attempt_id
            )
            attempt_record = {
                "schema": ATTEMPT_SCHEMA,
                "version": ATTEMPT_VERSION,
                "id": attempt_id,
                "status": "running",
                "phase": "native_generation",
                "runtime": "agent_plan_v1",
                "assurance": "unknown",
                "started_at": utc_timestamp(),
                "completed_at": None,
                "part_ids": [],
                "requested_parts": list(request.requested_parts),
                "files": {
                    "request": "request.json",
                    "plan": "circuit-plan.json",
                    "semantic_ir": "design.pcbir.json",
                    "part_catalog": "parts.pcbdraft.json",
                    "retained_native": None,
                },
                "error": None,
            }
            atomic_write_json(attempt_dir / "request.json", request.to_dict())
            atomic_write_json(attempt_dir / "circuit-plan.json", plan.to_dict())
            atomic_write_json(attempt_dir / "design.pcbir.json", design.to_dict())
            atomic_write_json(attempt_dir / "parts.pcbdraft.json", graph.to_dict())
            atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            attempt_record["assurance"] = str(
                design.metadata.get("assurance", "provisional")
            )
            attempt_record["part_ids"] = sorted(
                {component.part_id for component in design.components}
            )
            atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            generated = materialize_managed_design(
                request,
                design,
                project.design_root,
                graph=graph,
                plan=plan,
                retain_failed_attempt=attempt_dir / "native",
            )
        except BaseException as exc:
            if attempt_dir is not None and attempt_record is not None:
                attempt_record["status"] = "failed"
                attempt_record["phase"] = "failed"
                attempt_record["completed_at"] = utc_timestamp()
                attempt_record["error"] = _sanitize_secret_text(str(exc))[:2048]
                if (attempt_dir / "native").is_dir():
                    attempt_record["files"]["retained_native"] = "native"
                atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            self._record_failure(
                project_id,
                expected_revision,
                "generation_failed",
                "generation.failed",
                str(exc),
            )
            raise
        if attempt_dir is not None and attempt_record is not None:
            attempt_record["status"] = "completed"
            attempt_record["phase"] = "completed"
            attempt_record["completed_at"] = utc_timestamp()
            atomic_write_json(attempt_dir / "attempt.json", attempt_record)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while generation was running")
            state = current.state
            conversation = current.conversation
            state["status"] = "generated"
            state["design_revision"] = 1
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._append_message(
                conversation,
                "assistant",
                "generation",
                "Generated a native KiCad schematic and routed PCB. Validation results, when run, are reported separately.",
                data={
                    "design_content_hash": generated.project.design.content_hash(),
                    "routing_state": generated.pcb.routing.state,
                    "unrouted": list(generated.pcb.routing.unrouted),
                },
            )
            self._event(
                state,
                current.root,
                "generation.complete",
                "Native KiCad schematic and routed PCB generated",
            )
            self._write_records(current.root, state, conversation)
            expected_revision = int(state["revision"])
        preview = self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )
        if validate:
            return self.validate_project(
                project_id,
                timeout=timeout,
                expected_revision=int(preview["state"]["revision"]),
            )
        return preview

    def prepare_agent_repair(
        self,
        project_id: str,
        feedback: dict[str, Any],
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Revise a plan from bounded tool evidence and stage it transactionally.

        A project without an authoritative design receives a replacement pending
        plan.  A generated project is never edited in place: its replacement is
        generated and validated under ``transactions/`` before it can be applied.
        """

        normalized = normalize_repair_feedback(feedback)
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="plan repair"
        )
        if project.state["status"] not in {
            "generation_failed",
            "generated",
            "validated",
            "validation_failed",
            "repair_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("project is not eligible for automatic plan repair")
        if project.state["active_transaction"] is not None:
            raise ValidationError("project already has a staged semantic change")
        request = AgentDesignRequest.from_dict(
            load_json_limited(project.root / PENDING_REQUEST_NAME, APP_FILE_LIMIT)
        )
        previous_plan = CircuitPlan.from_dict(
            load_json_limited(project.root / PENDING_PLAN_NAME, APP_FILE_LIMIT)
        )
        if request.design_id != previous_plan.design_id:
            raise ValidationError("pending repair request and plan identities differ")
        authoritative = None
        baseline_design_revision = int(project.state["design_revision"])
        before_progress: ProgressVector | None = None
        before_stage: StageProjection | None = None
        before_consistency: NativeConsistencyReport | None = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            authoritative = open_managed_project(project.design_root)
            authoritative.assert_synchronized()
            if authoritative.design.design_id != request.design_id:
                raise ValidationError(
                    "authoritative design identity differs from the pending repair plan"
                )
            before_progress, before_stage, before_consistency = (
                self._managed_progress_and_stage(
                    project,
                    authoritative,
                    baseline_design_revision,
                )
            )
        prior_status = project.state["status"]
        prior_validation = project.state["last_validation"]
        prior_preview = project.state["last_preview"]
        prior_release = project.state["last_release"]
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before plan repair started")
            current.state["status"] = "repairing"
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "repair.started",
                f"Revising the circuit plan (attempt {normalized['attempt']})",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]

        reviser = getattr(self.provider, "revise_plan", None)
        try:
            if not callable(reviser) or not getattr(
                self.provider, "supports_planning", True
            ):
                raise PCBDraftError(
                    "the selected provider cannot revise a circuit plan from tool feedback"
                )
            revised_plan = reviser(
                request,
                previous_plan,
                normalized,
                symbol_context=planner_symbol_context(request),
                project_dir=project.root,
                run_dir=project.root / "provider-runs" / new_run_id(),
                timeout=timeout,
            )
            if revised_plan.canonical_bytes() == previous_plan.canonical_bytes():
                raise ValidationError(
                    "repair provider returned the unchanged circuit plan"
                )
            compilation = compile_agent_plan(request, revised_plan)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "repair_failed",
                "repair.failed",
                str(exc),
            )
            raise

        proposal = project.conversation.get("proposal")
        revised_proposal = (
            self._attach_plan(proposal, compilation)
            if isinstance(proposal, dict)
            else None
        )
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError(
                    "project changed while its plan was being revised"
                )
            atomic_write_json(
                current.root / PENDING_REQUEST_NAME, compilation.request.to_dict()
            )
            atomic_write_json(
                current.root / PENDING_PLAN_NAME, compilation.plan.to_dict()
            )
            atomic_write_json(
                current.root / PENDING_DESIGN_NAME, compilation.design.to_dict()
            )
            atomic_write_json(
                current.root / PENDING_PARTS_NAME, compilation.graph.to_dict()
            )
            if revised_proposal is not None:
                current.conversation["proposal"] = revised_proposal
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            if authoritative is None:
                current.state["status"] = "awaiting_confirmation"
                text = (
                    "A replacement circuit plan was compiled from retained tool "
                    "evidence and is ready for native KiCad generation."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_plan",
                    text,
                    data={"attempt": normalized["attempt"]},
                )
                self._event(current.state, current.root, "repair.plan_ready", text)
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]
        if authoritative is None:
            return self.open_project(project_id)

        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-agent-repair-transaction",
            "version": 2,
            "status": "preparing",
            "created_at": utc_timestamp(),
            "request": normalized["summary"],
            "feedback": normalized,
            "before_hash": authoritative.design.content_hash(),
            "after_hash": compilation.design.content_hash(),
            "prior_status": prior_status,
            "prior_validation": prior_validation,
            "prior_preview": prior_preview,
            "prior_release": prior_release,
            "validation": None,
            "result_status": None,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": baseline_design_revision + 1,
            "postconditions": [],
            "artifact": {},
        }
        if before_progress is None or before_stage is None:
            raise ValidationError("repair transaction lacks authoritative progress")
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        receipt["convergence_classification"] = receipt["progress_delta"][
            "classification"
        ]
        atomic_write_json(receipt_path, receipt)
        try:
            materialize_managed_design(
                compilation.request,
                compilation.design,
                staged,
                graph=compilation.graph,
                plan=compilation.plan,
                retain_failed_attempt=transaction / "failed-native",
            )
            candidate = open_managed_project(staged)
            candidate.assert_synchronized()
            validation_run = validate_managed_project(
                candidate,
                output=transaction / "validation",
                timeout=timeout,
                canonical_revision=expected_revision,
                design_revision=baseline_design_revision + 1,
            )
            self._bind_aggregate_validation_revision(
                transaction / "validation",
                candidate.design.content_hash(),
                baseline_design_revision + 1,
            )
            validation_report = load_json_limited(
                validation_run.report_path, APP_FILE_LIMIT
            )
            levels = validation_report["levels"]
            candidate_feedback = validation_feedback_from_levels(
                levels, attempt=normalized["attempt"]
            )
            validation_summary = {
                "report": validation_run.report_path.relative_to(
                    transaction
                ).as_posix(),
                "report_sha256": validation_run.report_sha256,
                "candidate_ready": validation_run.candidate_ready,
                "production_evidence_complete": (
                    validation_run.production_evidence_complete
                ),
                "production_ready": validation_run.production_ready,
                "production_claimed": False,
                "source_design_revision": baseline_design_revision + 1,
                "assurance": str(
                    candidate.design.metadata.get("assurance", "provisional")
                ),
            }
            atomic_write_json(
                transaction / "semantic-diff.json",
                semantic_diff(authoritative.design, candidate.design),
            )
            candidate_progress, candidate_stage, candidate_consistency = (
                self._managed_progress_and_stage(
                    project,
                    candidate,
                    baseline_design_revision + 1,
                    validation_root=transaction / "validation",
                    include_routing_failures=False,
                )
            )
            verified_candidate = self._require_current_native_consistency(
                candidate_consistency,
                baseline_design_revision + 1,
                label="staged candidate",
            )
            atomic_write_json(
                transaction / "native-consistency-before.json",
                (
                    before_consistency.to_dict()
                    if before_consistency is not None
                    else _unavailable_consistency_report(
                        baseline_design_revision
                    ).to_dict()
                ),
            )
            atomic_write_json(
                transaction / "native-consistency-candidate.json",
                verified_candidate.to_dict(),
            )
            receipt["artifact"] = {
                "semantic_diff": "semantic-diff.json",
                "native_consistency_before": "native-consistency-before.json",
                "native_consistency_candidate": "native-consistency-candidate.json",
                "validation": "validation",
            }
            receipt["candidate_progress"] = candidate_progress.to_dict()
            receipt["candidate_stage"] = candidate_stage.to_dict()
            receipt["postconditions"] = [
                {
                    "name": "candidate_native_consistency",
                    "passed": verified_candidate.consistency_passed,
                }
            ]
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage="native_consistency", tool_name="repair_candidate"
            )
            _attach_progress(
                receipt,
                before_progress,
                before_progress,
                before_stage,
                before_stage,
            )
            receipt["convergence_classification"] = receipt["progress_delta"][
                "classification"
            ]
            atomic_write_json(receipt_path, receipt)
            self._record_failure(
                project_id,
                expected_revision,
                "repair_failed",
                "repair.failed",
                str(exc),
            )
            raise

        receipt["validation"] = validation_summary
        rejected = candidate_feedback is not None
        if rejected:
            receipt["repair_feedback"] = candidate_feedback
        else:
            receipt["result_status"] = (
                "validated" if validation_run.candidate_ready else "generated"
            )
        # Candidate-only evidence may be durable while the top-level live
        # progress remains neutral.  The terminal ready/rejected fact is
        # published only after matching project records and its event.
        prepublication_receipt = copy.deepcopy(receipt)
        atomic_write_json(receipt_path, prepublication_receipt)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while a repair candidate was validated"
                    )
                current_managed = open_managed_project(current.design_root)
                current_managed.assert_synchronized()
                if current_managed.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed while a repair was staged"
                    )
                current.state["status"] = (
                    "repair_failed" if rejected else "change_ready"
                )
                if not rejected:
                    current.state["active_transaction"] = transaction_id
                current.state["revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                text = (
                    "The repair candidate retained deterministic L1-L3 failures; "
                    "the authoritative design was not changed."
                    if rejected
                    else "A replacement design passed deterministic L1-L3 repair "
                    "gates and is staged for atomic application."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_rejected" if rejected else "repair_ready",
                    text,
                    data=(
                        {
                            "transaction_id": transaction_id,
                            "repair_feedback": candidate_feedback,
                        }
                        if rejected
                        else {"transaction_id": transaction_id}
                    ),
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "repair.candidate_failed" if rejected else "repair.ready",
                    text,
                    level="error" if rejected else "info",
                )
                self._write_records(current.root, current.state, current.conversation)
                terminal = "rejected" if rejected else "ready"
                receipt["status"] = terminal
                receipt[f"{terminal}_at"] = utc_timestamp()
                receipt["publication"] = {
                    "status": "committed",
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.clear()
                receipt.update(copy.deepcopy(prepublication_receipt))
                receipt["publication"] = {
                    "status": (
                        "rollback_incomplete" if rollback_failures else "failed"
                    ),
                    "error_code": "publication_failed",
                    "failure": _sanitize_secret_text(str(exc))[:2048],
                    "rollback": {
                        "state": "incomplete" if rollback_failures else "restored",
                        "performed": not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    receipt["status"] = "rollback_incomplete"
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(before_progress.source_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                    receipt["convergence_classification"] = receipt["progress_delta"][
                        "classification"
                    ]
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "repair candidate publication failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.open_project(project_id)
        result["transaction_progress"] = _transaction_progress_projection(receipt)
        return result

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        if after < 0:
            raise ValidationError("event cursor must be non-negative")
        project = self._open(project_id)
        result: list[dict[str, Any]] = []
        for path in sorted((project.root / "events").glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                sequence = int(path.stem)
            except ValueError:
                continue
            if sequence > after:
                event = load_json_limited(path, 64 * 1024)
                if isinstance(event, dict):
                    result.append(event)
            if len(result) >= 500:
                break
        return result

    def _prepare_proposal(
        self,
        project_id: str,
        created_at: str,
        value: dict[str, Any],
        prior: dict[str, Any],
        request: str,
    ) -> tuple[dict[str, Any], AgentDesignRequest | None]:
        """Turn generic intent into a reviewable request, never a fixed board type."""

        merged = dict(value)
        layer_reply = bool(
            re.fullmatch(
                r"\s*(?:use\s+)?\d+\s*(?:[- ]?layers?|层)?\s*",
                request,
                re.IGNORECASE,
            )
        )
        prior_layers = prior.get("layers")
        if (
            merged.get("layers") is None
            and isinstance(prior_layers, int)
            and not isinstance(prior_layers, bool)
            and prior_layers >= 1
        ):
            merged["layers"] = prior_layers
        prior_board_value = prior.get("board")
        old_board: dict[str, Any] = (
            prior_board_value if isinstance(prior_board_value, dict) else {}
        )
        raw_board_value = merged.get("board")
        raw_board: dict[str, Any] = (
            raw_board_value if isinstance(raw_board_value, dict) else {}
        )
        merged["board"] = {
            key: raw_board.get(key)
            if raw_board.get(key) is not None
            else old_board.get(key)
            for key in ("width_mm", "height_mm")
        }
        if layer_reply:
            for key in ("requested_parts", "functions"):
                if not merged.get(key) and isinstance(prior.get(key), list):
                    merged[key] = list(prior[key])
            if isinstance(prior.get("request_summary"), str):
                merged["request_summary"] = prior["request_summary"]
        requested_parts = tuple(
            sorted(
                {
                    item
                    for item in merged.get("requested_parts", [])
                    if isinstance(item, str) and item.strip()
                },
                key=str.casefold,
            )
        )
        functions = tuple(
            sorted(
                {
                    item
                    for item in merged.get("functions", [])
                    if isinstance(item, str) and item.strip()
                }
            )
        )
        assumptions = [
            item
            for item in merged.get("assumptions", [])
            if isinstance(item, str) and item.strip()
        ]
        clarifications: list[dict[str, Any]] = []
        layers = merged.get("layers")
        if not isinstance(layers, int) or isinstance(layers, bool) or layers < 1:
            layers = _initial_stackup_layers(request)
            assumptions.append(
                "No usable planner stackup was returned; PCBDraft inferred an initial "
                f"{layers}-layer stackup from the stated design complexity."
            )
        merged["layers"] = layers
        width = merged["board"].get("width_mm")
        height = merged["board"].get("height_mm")
        if width is None or height is None:
            width, height = 80.0, 50.0
            assumptions.append(
                "Board envelope is assumed as 80 mm × 50 mm until changed in the reviewed plan."
            )
        merged["board"] = {"width_mm": float(width), "height_mm": float(height)}
        power_raw_value = merged.get("power")
        power_raw: dict[str, Any] = (
            power_raw_value if isinstance(power_raw_value, dict) else {}
        )
        nominal = power_raw.get("nominal_v")
        if (
            not isinstance(nominal, (int, float))
            or isinstance(nominal, bool)
            or nominal <= 0
        ):
            nominal = 3.3
            assumptions.append(
                "3.3 V logic supply is assumed until the reviewed plan specifies otherwise."
            )
        max_voltage = power_raw.get("max_voltage_v")
        if not isinstance(max_voltage, (int, float)) or isinstance(max_voltage, bool):
            max_voltage = nominal
        max_current = power_raw.get("max_current_a")
        if (
            not isinstance(max_current, (int, float))
            or isinstance(max_current, bool)
            or max_current <= 0
        ):
            max_current = 0.5
        max_power = power_raw.get("max_power_w")
        if (
            not isinstance(max_power, (int, float))
            or isinstance(max_power, bool)
            or max_power <= 0
        ):
            max_power = float(nominal) * float(max_current)
        power = {
            "nominal_v": float(nominal),
            "max_voltage_v": max(float(nominal), float(max_voltage)),
            "max_current_a": float(max_current),
            "max_power_w": float(max_power),
        }
        domains = {"simple_control"}
        words = " ".join((request, *requested_parts, *functions)).casefold()
        for token, domain in (
            ("i2c", "i2c"),
            ("i²c", "i2c"),
            ("spi", "spi"),
            ("uart", "uart"),
            ("串口", "uart"),
            ("usb", "usb2_basic"),
            ("基础usb", "usb2_basic"),
            ("buck", "simple_buck"),
            ("降压", "simple_buck"),
            ("ldo", "ldo"),
            ("稳压", "ldo"),
        ):
            if token in words:
                domains.add(domain)
        if any(
            token in words
            for token in (
                "sensor",
                "temperature",
                "humidity",
                "pressure",
                "传感器",
                "温度",
                "湿度",
                "压力",
            )
        ):
            domains.add("sensor")
        if any(
            token in words
            for token in (
                "mcu",
                "controller",
                "microcontroller",
                "embedded control",
                "单片机",
                "微控制器",
                "控制器",
                "控制板",
            )
        ):
            domains.add("low_voltage_mcu")
        for token, domain in (
            ("ddr", "ddr"),
            ("pcie", "pcie"),
            ("serdes", "serdes"),
            ("高速串行", "serdes"),
            ("rf", "rf"),
            ("antenna", "rf"),
            ("射频", "rf"),
            ("天线", "rf"),
            ("mains", "mains"),
            ("市电", "mains"),
            ("交流电", "mains"),
            ("high voltage", "high_voltage"),
            ("高压", "high_voltage"),
            ("high power", "high_power"),
            ("high-power", "high_power"),
            ("大功率", "high_power"),
            ("高功率", "high_power"),
            ("medical", "medical"),
            ("医疗", "medical"),
            ("aviation", "aviation"),
            ("航空", "aviation"),
            ("safety-critical", "safety_critical"),
            ("安全关键", "safety_critical"),
        ):
            if token in words:
                domains.add(domain)
        scope = Scope.from_dict(
            {
                "domains": sorted(domains),
                "max_voltage_v": power["max_voltage_v"],
                "max_current_a": power["max_current_a"],
                "max_power_w": power["max_power_w"],
                "layers": layers,
                "intended_use": "User-requested PCB design; no domain validation is implied.",
                "risk_class": "unspecified",
            }
        )
        scope_decision = evaluate_scope(scope)
        if not scope_decision.accepted:
            return (
                {
                    **merged,
                    "requested_parts": list(requested_parts),
                    "functions": list(functions),
                    "assurance": "provisional",
                    "scope": {
                        "decision": "generation_unavailable",
                        "errors": list(scope_decision.reasons),
                        "warnings": list(scope_decision.warnings),
                    },
                    "clarifications": [],
                    "planning": {"state": "not_started", "message": None},
                    "brief": None,
                    "decisions": {},
                },
                None,
            )
        board = BoardSpec.from_dict(
            {
                "width_mm": float(width),
                "height_mm": float(height),
                "layers": layers,
                "thickness_mm": 1.6,
                "edge_clearance_mm": 0.5,
                "min_track_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "finish": "enig",
            }
        )
        design_id = (
            f"{_slug(merged.get('design_name', 'board'))[:40]}-{project_id[-8:]}"
        )
        approved_request = AgentDesignRequest.from_dict(
            {
                "schema": "pcbdraft-agent-design-request",
                "version": 1,
                "design_id": design_id,
                "name": str(merged.get("design_name") or "PCBDraft board"),
                "revision": "A",
                "request_summary": str(merged.get("request_summary") or request),
                "scope": scope.to_dict(),
                "board": board.to_dict(),
                "assumptions": sorted(set(assumptions)),
                "requested_parts": list(requested_parts),
                "functions": list(functions),
                "power": power,
                "source": {
                    "locator": f"application/projects/{project_id}/conversation.json",
                    "date": created_at[:10],
                },
            }
        )
        proposal: dict[str, Any] = {
            **merged,
            "requested_parts": list(requested_parts),
            "functions": list(functions),
            "assumptions": list(approved_request.assumptions),
            "power": power,
            "assurance": "provisional",
            "scope": {
                "decision": "attempted",
                "warnings": list(scope_decision.warnings),
            },
            "clarifications": clarifications,
            "planning": {"state": "pending", "message": None},
            "brief": None,
            "decisions": {
                "runtime": "agent_plan_v1",
                "assurance": "provisional",
                "design_id": approved_request.design_id,
                "design_name": approved_request.name,
                "layers": approved_request.board.layers,
                "board": approved_request.board.to_dict(),
                "requested_parts": list(approved_request.requested_parts),
                "risk_class": approved_request.scope.risk_class,
            },
        }
        return proposal, None if clarifications else approved_request

    @staticmethod
    def _attach_plan(proposal: dict[str, Any], compilation: Any) -> dict[str, Any]:
        """Attach only reviewable plan/IR facts; native generation stays confirmed."""

        result = dict(proposal)
        design = compilation.design
        graph = compilation.graph
        counts: dict[tuple[str, str], int] = {}
        references: dict[tuple[str, str], list[str]] = {}
        for component in design.components:
            key = (component.part_id, component.value)
            counts[key] = counts.get(key, 0) + 1
            references.setdefault(key, []).append(component.reference)
        result["planning"] = {"state": "ready", "message": None}
        result["brief"] = {
            "purpose": compilation.request.request_summary,
            "architecture": [
                {"id": block.id, "kind": block.kind, "name": block.name}
                for block in design.blocks
            ],
            "assumptions": list(compilation.request.assumptions),
            "power": compilation.request.power,
            "interfaces": [],
            "board": compilation.request.board.to_dict(),
            "identity": {
                "requested_parts": list(compilation.request.requested_parts),
                "planned_symbols": [
                    {
                        "reference": component.reference,
                        "symbol": graph.get(component.part_id).symbol,
                        "part_id": component.part_id,
                    }
                    for component in design.components
                ],
                "preserved": True,
            },
            "bom": [
                {
                    "part_id": key[0],
                    "value": key[1],
                    "quantity": counts[key],
                    "references": sorted(references[key]),
                    "symbol": graph.get(key[0]).symbol,
                    "trust": graph.get(key[0]).trust,
                }
                for key in sorted(counts)
            ],
            "net_count": len(design.nets),
            "constraints": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "severity": item.severity,
                    "rationale": item.rationale,
                }
                for item in design.constraints
            ],
            "plan_review": compilation.review.to_dict(),
            "semantic_content_hash": design.content_hash(),
            "confirmation_required": True,
        }
        return result

    @staticmethod
    def _proposal_message(proposal: dict[str, Any]) -> str:
        decision = proposal["scope"]["decision"]
        if decision != "attempted":
            return "This request cannot reach the current KiCad backend: " + "; ".join(
                proposal["scope"].get("errors", [])
            )
        if proposal["clarifications"]:
            return proposal["clarifications"][0]["question"]
        planning = proposal.get("planning", {})
        if planning.get("state") != "ready":
            return (
                "Requirements were retained without substituting parts, but a circuit "
                "planning provider is needed before a reviewable topology can be generated: "
                + str(planning.get("message") or "planning is pending")
            )
        review = proposal.get("brief", {}).get("plan_review", {})
        summary = review.get("summary", {}) if isinstance(review, dict) else {}
        attention = summary.get("attention_required", 0)
        if isinstance(attention, int) and attention > 0:
            return (
                "The circuit plan and stock KiCad parts are ready for review. "
                f"{attention} deterministic preflight finding(s) need engineering attention; "
                "generation remains available according to the active client policy."
            )
        return (
            "The circuit plan, stock KiCad parts, and assumptions are ready. "
            "The active client policy controls whether generation continues automatically "
            "or waits for review."
        )

    def _record_failure(
        self,
        project_id: str,
        expected_revision: int,
        status: str,
        event_kind: str,
        message: str,
    ) -> None:
        project = self._open(project_id)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                return
            state = current.state
            conversation = current.conversation
            state["status"] = status
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            public = _sanitize_secret_text(message)[:2048]
            self._append_message(conversation, "assistant", "failure", public)
            self._event(state, current.root, event_kind, public, level="error")
            self._write_records(current.root, state, conversation)

    def _recover_interrupted_projects(self) -> None:
        for candidate in self.projects_root.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                project = self._open_path(candidate)
            except PCBDraftError:
                continue
            if project.state["status"] not in _TRANSIENT_STATES:
                continue
            try:
                with ResourceLock(candidate, self.locks_root, timeout=0):
                    current = self._open_path(candidate)
                    if current.state["status"] in _TRANSIENT_STATES:
                        self._interrupt_running_attempts(candidate)
                        recovered_status = "interrupted"
                        if (
                            current.design_root.is_dir()
                            and not current.design_root.is_symlink()
                        ):
                            try:
                                open_managed_project(
                                    current.design_root
                                ).assert_synchronized()
                                recovered_status = "generated"
                            except PCBDraftError:
                                recovered_status = "interrupted"
                        current.state["status"] = recovered_status
                        current.state["revision"] += 1
                        current.state["updated_at"] = utc_timestamp()
                        self._event(
                            current.state,
                            candidate,
                            "operation.interrupted",
                            (
                                "Recovered an atomically published managed project; "
                                "validation may be retried."
                                if recovered_status == "generated"
                                else "Previous operation was interrupted and may be retried."
                            ),
                            level="warning",
                        )
                        self._write_records(
                            candidate, current.state, current.conversation
                        )
            except PCBDraftError:
                continue

    def _project_path(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise ValidationError("application project id is invalid")
        path = self.projects_root / project_id
        if path.is_symlink():
            raise ValidationError("application project path is unsafe")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(
                f"application project does not exist: {project_id}"
            ) from exc
        if resolved.parent != self.projects_root or not resolved.is_dir():
            raise ValidationError("application project path escapes the workspace")
        return resolved

    def _open(self, project_id: str) -> ApplicationProject:
        return self._open_path(self._project_path(project_id))

    def _open_path(self, root: Path) -> ApplicationProject:
        state = load_json_limited(root / "project.json", APP_FILE_LIMIT)
        conversation = load_json_limited(root / "conversation.json", APP_FILE_LIMIT)
        self._validate_state(state, expected_id=root.name)
        self._validate_conversation(conversation)
        return ApplicationProject(root=root, state=state, conversation=conversation)

    @staticmethod
    def _validate_state(value: Any, *, expected_id: str) -> None:
        if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
            raise ValidationError("application project record is malformed")
        if (
            value["schema"] != APP_PROJECT_SCHEMA
            or value["version"] != APP_PROJECT_VERSION
        ):
            raise ValidationError("unsupported application project schema/version")
        if value["id"] != expected_id or not _PROJECT_ID.fullmatch(value["id"]):
            raise ValidationError("application project identity is malformed")
        for field in ("name", "created_at", "updated_at", "status", "provider"):
            if not isinstance(value[field], str) or not value[field]:
                raise ValidationError(
                    f"application project field is malformed: {field}"
                )
        for field in ("revision", "design_revision", "event_sequence"):
            if (
                isinstance(value[field], bool)
                or not isinstance(value[field], int)
                or value[field] < 0
            ):
                raise ValidationError(
                    f"application project counter is malformed: {field}"
                )

    @staticmethod
    def _validate_conversation(value: Any) -> None:
        if not isinstance(value, dict) or set(value) != _CONVERSATION_FIELDS:
            raise ValidationError("conversation record is malformed")
        if (
            value["schema"] != CONVERSATION_SCHEMA
            or value["version"] != CONVERSATION_VERSION
        ):
            raise ValidationError("unsupported conversation record schema/version")
        if (
            not isinstance(value["messages"], list)
            or len(value["messages"]) > MAX_MESSAGES
        ):
            raise ValidationError("conversation message history is malformed")
        if not isinstance(value["decisions"], dict):
            raise ValidationError("conversation decisions are malformed")

    @staticmethod
    def _append_message(
        conversation: dict[str, Any],
        role: str,
        kind: str,
        text: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        if len(conversation["messages"]) >= MAX_MESSAGES:
            raise ValidationError("conversation reached its 2000 message limit")
        conversation["messages"].append(
            {
                "id": secrets.token_hex(8),
                "role": role,
                "kind": kind,
                "text": _sanitize_secret_text(text),
                "created_at": utc_timestamp(),
                "data": data or {},
            }
        )

    @staticmethod
    def _event(
        state: dict[str, Any],
        root: Path,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        state["event_sequence"] += 1
        sequence = state["event_sequence"]
        content_hash: str | None = None
        ir_path = root / "design" / IR_NAME
        if ir_path.is_file() and not ir_path.is_symlink():
            try:
                content_hash = Design.from_dict(
                    load_json_limited(ir_path, 16 * 1024 * 1024)
                ).content_hash()
            except PCBDraftError:
                content_hash = None
        atomic_write_json(
            root / "events" / f"{sequence:08d}.json",
            {
                "schema": "pcbdraft-structured-event",
                "version": 1,
                "sequence": sequence,
                "kind": kind,
                "level": level,
                "message": _sanitize_secret_text(message)[:2048],
                "created_at": utc_timestamp(),
                "canonical_revision": state.get("revision"),
                "design_revision": state.get("design_revision"),
                "design_content_hash": content_hash,
                "binding_state": ("bound" if content_hash is not None else "no_design"),
            },
        )

    @staticmethod
    def _write_records(
        root: Path, state: dict[str, Any], conversation: dict[str, Any]
    ) -> None:
        atomic_write_json(root / "conversation.json", conversation)
        atomic_write_json(root / "project.json", state)

    @staticmethod
    def _summary(project: ApplicationProject) -> dict[str, Any]:
        return {
            "id": project.state["id"],
            "name": project.state["name"],
            "status": project.state["status"],
            "updated_at": project.state["updated_at"],
            "design_revision": project.state["design_revision"],
            "provider": project.state["provider"],
        }

    @staticmethod
    def _interrupt_running_attempts(root: Path) -> None:
        attempts = root / "attempts"
        if attempts.is_symlink() or not attempts.is_dir():
            return
        for candidate in attempts.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            record_path = candidate / "attempt.json"
            try:
                record = load_json_limited(record_path, APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if (
                not ApplicationService._valid_attempt_record(
                    record, expected_id=candidate.name
                )
                or record.get("status") != "running"
            ):
                continue
            record["status"] = "interrupted"
            record["phase"] = "interrupted"
            record["completed_at"] = utc_timestamp()
            record["error"] = "Generation process stopped before completion."
            atomic_write_json(record_path, record)

    @staticmethod
    def _attempt_records(project: ApplicationProject) -> list[dict[str, Any]]:
        attempts = project.root / "attempts"
        if attempts.is_symlink() or not attempts.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for candidate in sorted(attempts.iterdir(), reverse=True):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                record = load_json_limited(candidate / "attempt.json", APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if not ApplicationService._valid_attempt_record(
                record, expected_id=candidate.name
            ):
                continue
            public = dict(record)
            public["root"] = str(candidate)
            result.append(public)
            if len(result) >= 50:
                break
        return result

    @staticmethod
    def _valid_attempt_record(value: Any, *, expected_id: str) -> bool:
        if not isinstance(value, dict) or set(value) != _ATTEMPT_FIELDS:
            return False
        files = value.get("files")
        string_lists = (value.get("part_ids"), value.get("requested_parts"))
        return bool(
            value.get("schema") == ATTEMPT_SCHEMA
            and value.get("version") == ATTEMPT_VERSION
            and value.get("id") == expected_id
            and value.get("status") in {"running", "completed", "failed", "interrupted"}
            and isinstance(value.get("phase"), str)
            and isinstance(value.get("runtime"), str)
            and value.get("assurance") in {"unknown", "provisional"}
            and isinstance(value.get("started_at"), str)
            and (
                value.get("completed_at") is None
                or isinstance(value.get("completed_at"), str)
            )
            and all(
                isinstance(items, list)
                and len(items) <= 2_000
                and all(isinstance(item, str) for item in items)
                for items in string_lists
            )
            and isinstance(files, dict)
            and set(files)
            == {"request", "plan", "semantic_ir", "part_catalog", "retained_native"}
            and all(item is None or isinstance(item, str) for item in files.values())
            and (value.get("error") is None or isinstance(value.get("error"), str))
        )

    def _public_project(self, project: ApplicationProject) -> dict[str, Any]:
        design: dict[str, Any] | None = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            managed = open_managed_project(project.design_root)
            design = {
                "root": str(managed.root),
                "design_id": managed.design.design_id,
                "name": managed.design.name,
                "content_hash": managed.design.content_hash(),
                "drift": list(managed.drift()),
                "files": {
                    key: str(managed.root / relative)
                    for key, relative in managed.manifest["files"].items()
                },
            }
        active_change: dict[str, Any] | None = None
        transaction_id = project.state.get("active_transaction")
        if isinstance(transaction_id, str) and re.fullmatch(
            r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", transaction_id
        ):
            transaction = project.root / "transactions" / transaction_id
            receipt = load_json_limited(transaction / "receipt.json", APP_FILE_LIMIT)
            diff = load_json_limited(transaction / "semantic-diff.json", APP_FILE_LIMIT)
            active_change = {
                "transaction_id": transaction_id,
                "request": receipt.get("request"),
                "status": receipt.get("status"),
                "diff": diff,
                "validation": _public_readiness_record(receipt.get("validation")),
                "progress": _transaction_progress_projection(receipt),
            }
        public_state = dict(project.state)
        public_state["last_validation"] = _public_readiness_record(
            project.state["last_validation"]
        )
        public_state["last_release"] = _public_readiness_record(
            project.state["last_release"]
        )
        return {
            "schema": "pcbdraft-application-view",
            "version": 1,
            "project": self._summary(project),
            "state": public_state,
            "conversation": project.conversation,
            "design": design,
            "artifacts": {
                "previews": project.state["last_preview"],
                "validation": public_state["last_validation"],
                "release": public_state["last_release"],
            },
            "attempts": self._attempt_records(project),
            "active_change": active_change,
            "events": self.events(
                project.state["id"], after=max(0, project.state["event_sequence"] - 50)
            ),
        }

    def external_kicad_change_status(self, project_id: str) -> dict[str, Any]:
        """Detect native-file drift without treating desktop state as authoritative."""

        project = self._open(project_id)
        if project.design_root.is_symlink() or not project.design_root.is_dir():
            return {
                "state": "no_design",
                "requires_import": False,
                "canonical_revision": project.state["revision"],
                "design_revision": project.state["design_revision"],
                "content_hash": None,
            }
        managed = open_managed_project(project.design_root)
        drift = managed.drift()
        binding = {
            "canonical_revision": project.state["revision"],
            "design_revision": project.state["design_revision"],
            "content_hash": managed.design.content_hash(),
        }
        if not drift:
            return {
                "state": "clean",
                "requires_import": False,
                "drift": [],
                **binding,
            }
        try:
            preview = preview_kicad_import(managed)
        except PCBDraftError as exc:
            return {
                "state": "unsupported_external_change",
                "requires_import": True,
                "importable": False,
                "drift": list(drift),
                "limitation": _sanitize_secret_text(str(exc))[:1024],
                **binding,
            }
        if not preview.has_changes:
            return {
                "state": "unsupported_external_change",
                "requires_import": True,
                "importable": False,
                "drift": list(drift),
                "limitation": "native bytes changed without a supported semantic placement revision",
                **binding,
            }
        return {
            "state": "review_required",
            "requires_import": True,
            "importable": True,
            "drift": list(drift),
            "board_sha256": preview.board_sha256,
            "change_set_id": preview.change_set.id if preview.change_set else None,
            "native_changes": list(preview.native_changes[:1_000]),
            "semantic_diff": preview.diff,
            **binding,
        }

    def import_external_kicad_revision(
        self,
        project_id: str,
        *,
        expected_revision: int | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Explicitly import reviewed KiCad placement drift as a new revision."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="external KiCad import"
        )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("project is not eligible for external KiCad import")
        if project.state["active_transaction"] is not None:
            raise ValidationError("project already has a staged semantic change")
        managed = open_managed_project(project.design_root)
        preview = preview_kicad_import(managed)
        if not preview.has_changes or preview.change_set is None:
            raise ValidationError("no supported external KiCad revision is available")
        source_design_revision = int(project.state["design_revision"])
        source_hash = managed.design.content_hash()
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before external KiCad import")
            current.state["status"] = "importing_external"
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "external_revision.import_started",
                "Importing a reviewed external KiCad placement revision",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = int(current.state["revision"])
        try:
            transaction = apply_kicad_import(preview, timeout=timeout)
            imported = open_managed_project(project.design_root)
            imported.assert_synchronized()
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "interrupted",
                "external_revision.import_failed",
                str(exc),
            )
            raise
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while external KiCad import ran")
            current.state["status"] = "generated"
            current.state["revision"] += 1
            current.state["design_revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            current.state["last_validation"] = None
            current.state["last_preview"] = None
            current.state["last_release"] = None
            message = (
                "Reviewed KiCad placement changes were imported as an explicit external "
                "revision. Run final validation before release."
            )
            self._append_message(
                current.conversation,
                "assistant",
                "external_revision",
                message,
                data={
                    "source_design_revision": source_design_revision,
                    "source_content_hash": source_hash,
                    "design_content_hash": imported.design.content_hash(),
                    "transaction": transaction.name,
                },
            )
            self._event(
                current.state,
                current.root,
                "external_revision.imported",
                message,
            )
            self._write_records(current.root, current.state, current.conversation)
        result = self.open_project(project_id)
        result["external_revision"] = {
            "state": "imported",
            "source_design_revision": source_design_revision,
            "design_revision": result["state"]["design_revision"],
            "content_hash": imported.design.content_hash(),
            "transaction": transaction.name,
        }
        return result

    def validate_project(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Run the real layered runtime validation and attach its evidence."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="validation"
        )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "released",
            "interrupted",
            "release_failed",
        }:
            raise ValidationError("project must be generated before validation")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        baseline = self._latest_drc_baseline(project)
        source_design_revision = int(project.state["design_revision"])
        run_id = new_run_id()
        output = project.root / "validation" / run_id
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before validation")
            state = current.state
            state["status"] = "validating"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._event(
                state, current.root, "validation.started", "Running configured checks"
            )
            self._write_records(current.root, state, current.conversation)
            expected_revision = state["revision"]
        try:
            result = validate_managed_project(
                managed,
                output=output,
                timeout=timeout,
                canonical_revision=expected_revision,
                design_revision=source_design_revision,
                baseline_drc_evidence=baseline[0] if baseline is not None else None,
                expected_baseline_design_revision=(
                    baseline[1] if baseline is not None else None
                ),
                expected_baseline_content_hash=(
                    baseline[2] if baseline is not None else None
                ),
            )
            self._bind_aggregate_validation_revision(
                output,
                managed.design.content_hash(),
                int(project.state["design_revision"]),
            )
            report = load_json_limited(result.report_path, APP_FILE_LIMIT)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "validation_failed",
                "validation.failed",
                str(exc),
            )
            raise
        relative_report = result.report_path.relative_to(project.root).as_posix()
        summary = {
            "run_id": run_id,
            "report": relative_report,
            "report_sha256": result.report_sha256,
            "candidate_ready": result.candidate_ready,
            "production_evidence_complete": result.production_evidence_complete,
            "production_ready": result.production_ready,
            "production_claimed": False,
            "source_design_revision": source_design_revision,
            "source_content_hash": managed.design.content_hash(),
            "design_content_hash": managed.design.content_hash(),
            "completed_at": utc_timestamp(),
            "erc_evidence": result.erc_evidence_path.relative_to(
                project.root
            ).as_posix(),
            "drc_evidence": result.drc_evidence_path.relative_to(
                project.root
            ).as_posix(),
            "drc_delta": result.drc_delta_path.relative_to(project.root).as_posix(),
            "assurance": str(managed.design.metadata.get("assurance", "verified")),
            "levels": report["levels"],
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while validation was running")
            state = current.state
            conversation = current.conversation
            state["last_validation"] = summary
            provisional = summary["assurance"] == "provisional"
            state["status"] = (
                "validated"
                if result.candidate_ready
                else "generated"
                if provisional
                else "validation_failed"
            )
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            text = (
                "The configured KiCad and PCBDraft checks passed. This does not "
                "establish electrical, regulatory, or manufacturing fitness."
                if result.candidate_ready
                else (
                    "KiCad and PCBDraft checks completed and the generated files were retained. Review the reported findings; no electrical, regulatory, or manufacturing validation is implied."
                    if provisional
                    else "Checks found issues; the generated files and results were retained for review."
                )
            )
            self._append_message(
                conversation,
                "assistant",
                "validation",
                text,
                data={
                    "candidate_ready": result.candidate_ready,
                    "production_evidence_complete": (
                        result.production_evidence_complete
                    ),
                    "production_ready": result.production_ready,
                },
            )
            self._event(
                state,
                current.root,
                "validation.complete",
                text,
                level="info" if result.candidate_ready or provisional else "error",
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def generate_project_previews(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Generate browser-safe links to real KiCad exports and a 3D render."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="preview generation"
        )
        if not project.design_root.is_dir():
            raise ValidationError("project must be generated before preview export")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        output = project.root / "previews" / new_run_id()
        bundle = generate_previews(managed, output, timeout=timeout)
        preview = {
            "root": bundle.root.relative_to(project.root).as_posix(),
            "receipt": bundle.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": bundle.design_content_hash,
            "files": {
                key: path.relative_to(project.root).as_posix()
                for key, path in bundle.files.items()
            },
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while previews were generated")
            if (
                open_managed_project(current.design_root).design.content_hash()
                != bundle.design_content_hash
            ):
                raise ValidationError("design changed while previews were generated")
            current.state["last_preview"] = preview
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "preview.complete",
                "Schematic, PCB, PDF, and 3D render previews generated",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def preview_modification(
        self,
        project_id: str,
        request: str,
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Turn a follow-up message into a validated, staged replacement design.

        The planning provider receives the retained semantic plan plus a bounded
        user revision request.  It never edits native KiCad files: the replacement
        is generated and checked inside a transaction before the runtime policy or
        user can atomically apply it.
        """

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="revision staging"
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        if managed.design.metadata.get("generator") != "agent_plan_v1":
            raise ValidationError(
                "this project was not generated from a retained agent circuit plan; use the semantic patch workflow"
            )
        if project.state["active_transaction"] is not None:
            raise ValidationError(
                "review, apply, or discard the staged PCB change before requesting another revision"
            )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "repair_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("the current project state cannot accept a revision")
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before the revision was staged")
            self._append_message(current.conversation, "user", "revision", request)
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "repair.requested",
                "Preparing a transactional PCB revision from the follow-up request",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = int(current.state["revision"])
        return self.prepare_agent_repair(
            project_id,
            user_revision_feedback(request),
            timeout=timeout,
            expected_revision=expected_revision,
        )

    def apply_modification(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically publish the currently staged, confirmed semantic change."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="candidate application"
        )
        transaction_id = project.state["active_transaction"]
        if project.state["status"] != "change_ready" or not isinstance(
            transaction_id, str
        ):
            raise ValidationError(
                "project has no semantic change awaiting confirmation"
            )
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-agent-repair-transaction"
            or receipt.get("status") != "ready"
            or receipt.get("version") not in {1, 2}
        ):
            raise ValidationError("semantic change receipt is not ready")
        staged = transaction / "staged"
        before = transaction / "before"
        baseline_progress, baseline_stage = self._current_progress_and_stage(project)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            original_receipt = copy.deepcopy(receipt)
            moved_before = False
            moved_candidate = False
            event_path: Path | None = None
            before_progress = baseline_progress
            before_stage = baseline_stage
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed before candidate application"
                    )
                if current.state["active_transaction"] != transaction_id:
                    raise ValidationError("active semantic transaction changed")
                if before.exists() or before.is_symlink():
                    raise ValidationError("candidate application backup already exists")
                current_managed = open_managed_project(current.design_root)
                staged_managed = open_managed_project(staged)
                current_managed.assert_synchronized()
                staged_managed.assert_synchronized()
                if current_managed.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed after semantic preview"
                    )
                if staged_managed.design.content_hash() != receipt["after_hash"]:
                    raise ValidationError(
                        "staged design no longer matches the semantic receipt"
                    )
                semantic_delta = load_json_limited(
                    transaction / "semantic-diff.json", APP_FILE_LIMIT
                )
                if (
                    not isinstance(semantic_delta, Mapping)
                    or semantic_delta.get("schema") != "pcbdraft-semantic-diff"
                    or semantic_delta.get("before_hash") != receipt["before_hash"]
                    or semantic_delta.get("after_hash") != receipt["after_hash"]
                ):
                    raise _PCBOperationPostconditionError(
                        "native_delta_failed",
                        "legacy modification semantic replacement identity is invalid",
                    )
                before_revision = int(current.state["design_revision"])
                after_revision = before_revision + 1
                before_progress, before_stage, before_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        current_managed,
                        before_revision,
                    )
                )
                after_progress, after_stage, after_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        staged_managed,
                        after_revision,
                        validation_root=transaction / "validation",
                        include_routing_failures=False,
                    )
                )
                verified_before = self._require_current_native_consistency(
                    before_consistency,
                    before_revision,
                    label="authoritative source",
                )
                verified_after = self._require_current_native_consistency(
                    after_consistency,
                    after_revision,
                    label="staged candidate",
                )
                atomic_write_json(
                    transaction / "application-native-before.json",
                    verified_before.to_dict(),
                )
                atomic_write_json(
                    transaction / "application-native-after.json",
                    verified_after.to_dict(),
                )
                receipt["version"] = 2
                receipt["baseline_design_revision"] = before_revision
                receipt["candidate_revision"] = after_revision
                receipt.setdefault("artifact", {})
                receipt["artifact"].update(
                    {
                        "application_native_before": "application-native-before.json",
                        "application_native_after": "application-native-after.json",
                    }
                )
                receipt["postconditions"] = [
                    {
                        "name": "source_native_consistency",
                        "passed": verified_before.consistency_passed,
                    },
                    {
                        "name": "candidate_native_consistency",
                        "passed": verified_after.consistency_passed,
                    },
                    {
                        "name": "semantic_replacement_identity",
                        "passed": True,
                    },
                ]
                _attach_progress(
                    receipt,
                    before_progress,
                    after_progress,
                    before_stage,
                    after_stage,
                )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["application_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                receipt["application"] = {
                    "status": "publishing",
                    "error_code": None,
                    "rollback": {
                        "state": "not_required",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                atomic_write_json(receipt_path, receipt)
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                moved_candidate = True
                current.state["status"] = receipt.get("result_status", "validated")
                current.state["active_transaction"] = None
                current.state["last_transaction"] = transaction_id
                current.state["last_validation"] = {
                    "run_id": f"transaction:{transaction_id}",
                    "report": (
                        Path("transactions")
                        / transaction_id
                        / receipt["validation"]["report"]
                    ).as_posix(),
                    **{
                        key: receipt["validation"][key]
                        for key in (
                            "report_sha256",
                            "candidate_ready",
                            "production_evidence_complete",
                            "production_ready",
                            "production_claimed",
                            "source_design_revision",
                        )
                    },
                    "assurance": receipt["validation"].get("assurance", "provisional"),
                    "levels": load_json_limited(
                        transaction / receipt["validation"]["report"], APP_FILE_LIMIT
                    )["levels"],
                }
                current.state["last_release"] = None
                current.state["last_preview"] = None
                current.state["design_revision"] = after_revision
                current.state["revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                text = (
                    "Applied the staged replacement atomically; undo remains available."
                    if receipt.get("schema") == "pcbdraft-agent-repair-transaction"
                    else "Applied the confirmed semantic change atomically; undo remains available."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "change_applied",
                    text,
                    data={"transaction_id": transaction_id},
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(current.state, current.root, "change.applied", text)
                self._write_records(current.root, current.state, current.conversation)
                receipt["status"] = "applied"
                receipt["applied_at"] = utc_timestamp()
                receipt["application"] = {
                    "status": "committed",
                    "error_code": None,
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": False,
                    },
                }
                atomic_write_json(receipt_path, receipt)
                expected_revision = int(current.state["revision"])
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_candidate:
                    try:
                        if staged.exists() or staged.is_symlink():
                            raise ValidationError(
                                "staged rollback destination already exists"
                            )
                        os.replace(current.design_root, staged)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                if moved_before:
                    try:
                        if (
                            current.design_root.exists()
                            or current.design_root.is_symlink()
                        ):
                            raise ValidationError(
                                "live rollback destination already exists"
                            )
                        os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.pop("applied_at", None)
                receipt["status"] = (
                    "rollback_incomplete"
                    if rollback_failures
                    else str(original_receipt.get("status", "ready"))
                )
                receipt["application"] = {
                    "status": "rollback_incomplete" if rollback_failures else "failed",
                    "error_code": _operation_failure_code(
                        exc, stage="publication", tool_name="repair_candidate"
                    ),
                    "failure": _sanitize_secret_text(str(exc))[:2048],
                    "rollback": {
                        "state": (
                            "incomplete"
                            if rollback_failures
                            else "restored"
                            if moved_before or moved_candidate
                            else "not_required"
                        ),
                        "performed": (moved_before or moved_candidate)
                        and not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(before_progress.source_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["application_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "candidate application failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )
        result["transaction_progress"] = _transaction_progress_projection(receipt)
        return result

    def discard_modification(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="candidate discard"
        )
        transaction_id = project.state["active_transaction"]
        if project.state["status"] != "change_ready" or not isinstance(
            transaction_id, str
        ):
            raise ValidationError(
                "project has no semantic change awaiting confirmation"
            )
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before candidate discard")
            if current.state["active_transaction"] != transaction_id:
                raise ValidationError("active semantic transaction changed")
            receipt["status"] = "discarded"
            receipt["discarded_at"] = utc_timestamp()
            atomic_write_json(receipt_path, receipt)
            current.state["active_transaction"] = None
            current.state["status"] = receipt.get("prior_status", "generated")
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "change.discarded",
                "Staged semantic change discarded; authoritative design was untouched.",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def undo_last_modification(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="last-change undo"
        )
        transaction_id = project.state["last_transaction"]
        if not isinstance(transaction_id, str):
            raise ValidationError("project has no applied semantic change to undo")
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-agent-repair-transaction"
            or receipt.get("status") != "applied"
            or receipt.get("version") not in {1, 2}
        ):
            raise ValidationError("last semantic transaction is not undoable")
        before = transaction / "before"
        after = transaction / "after"
        baseline_progress, baseline_stage = self._current_progress_and_stage(project)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            original_receipt = copy.deepcopy(receipt)
            moved_after = False
            moved_before = False
            event_path: Path | None = None
            before_progress = baseline_progress
            before_stage = baseline_stage
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError("project changed before last-change undo")
                if current.state["last_transaction"] != transaction_id:
                    raise ValidationError("last semantic transaction changed")
                if after.exists() or after.is_symlink():
                    raise ValidationError("undo backup already exists")
                managed = open_managed_project(current.design_root)
                restored_managed = open_managed_project(before)
                managed.assert_synchronized()
                restored_managed.assert_synchronized()
                if managed.design.content_hash() != receipt["after_hash"]:
                    raise ValidationError(
                        "authoritative design changed after the last transaction"
                    )
                if restored_managed.design.content_hash() != receipt["before_hash"]:
                    raise _PCBOperationPostconditionError(
                        "native_delta_failed",
                        "undo target no longer matches the semantic replacement identity",
                    )
                semantic_delta = load_json_limited(
                    transaction / "semantic-diff.json", APP_FILE_LIMIT
                )
                if (
                    not isinstance(semantic_delta, Mapping)
                    or semantic_delta.get("schema") != "pcbdraft-semantic-diff"
                    or semantic_delta.get("before_hash") != receipt["before_hash"]
                    or semantic_delta.get("after_hash") != receipt["after_hash"]
                ):
                    raise _PCBOperationPostconditionError(
                        "native_delta_failed",
                        "undo semantic replacement identity is invalid",
                    )
                before_revision = int(current.state["design_revision"])
                after_revision = before_revision + 1
                before_progress, before_stage, before_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        managed,
                        before_revision,
                    )
                )
                prior_validation_root: Path | None = None
                prior_validation = receipt.get("prior_validation")
                if isinstance(prior_validation, Mapping) and isinstance(
                    prior_validation.get("report"), str
                ):
                    candidate = current.root / str(prior_validation["report"])
                    try:
                        candidate.relative_to(current.root)
                    except ValueError:
                        pass
                    else:
                        prior_validation_root = candidate.parent
                after_progress, after_stage, after_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        restored_managed,
                        after_revision,
                        validation_root=prior_validation_root,
                        include_routing_failures=False,
                    )
                )
                verified_before = self._require_current_native_consistency(
                    before_consistency,
                    before_revision,
                    label="applied source",
                )
                verified_after = self._require_current_native_consistency(
                    after_consistency,
                    after_revision,
                    label="undo target",
                )
                atomic_write_json(
                    transaction / "undo-native-before.json",
                    verified_before.to_dict(),
                )
                atomic_write_json(
                    transaction / "undo-native-after.json",
                    verified_after.to_dict(),
                )
                receipt["version"] = 2
                receipt.setdefault("artifact", {})
                receipt["artifact"].update(
                    {
                        "undo_native_before": "undo-native-before.json",
                        "undo_native_after": "undo-native-after.json",
                    }
                )
                receipt["postconditions"] = [
                    {
                        "name": "undo_source_native_consistency",
                        "passed": verified_before.consistency_passed,
                    },
                    {
                        "name": "undo_target_native_consistency",
                        "passed": verified_after.consistency_passed,
                    },
                    {
                        "name": "undo_semantic_replacement_identity",
                        "passed": True,
                    },
                ]
                _attach_progress(
                    receipt,
                    before_progress,
                    after_progress,
                    before_stage,
                    after_stage,
                )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["undo_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                receipt["undo"] = {
                    "status": "publishing",
                    "error_code": None,
                    "rollback": {
                        "state": "not_required",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                atomic_write_json(receipt_path, receipt)
                os.replace(current.design_root, after)
                moved_after = True
                os.replace(before, current.design_root)
                moved_before = True
                current.state["status"] = receipt.get("prior_status", "generated")
                current.state["last_transaction"] = None
                current.state["last_validation"] = receipt.get("prior_validation")
                current.state["last_preview"] = receipt.get("prior_preview")
                current.state["last_release"] = receipt.get("prior_release")
                current.state["design_revision"] = after_revision
                current.state["revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                text = "Undo restored the exact previous authoritative managed project."
                self._append_message(
                    current.conversation,
                    "assistant",
                    "change_undone",
                    text,
                    data={"transaction_id": transaction_id},
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(current.state, current.root, "change.undone", text)
                self._write_records(current.root, current.state, current.conversation)
                receipt["status"] = "undone"
                receipt["undone_at"] = utc_timestamp()
                receipt["undo"] = {
                    "status": "committed",
                    "error_code": None,
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": False,
                    },
                }
                atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        if before.exists() or before.is_symlink():
                            raise ValidationError(
                                "undo target rollback destination already exists"
                            )
                        os.replace(current.design_root, before)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                if moved_after:
                    try:
                        if (
                            current.design_root.exists()
                            or current.design_root.is_symlink()
                        ):
                            raise ValidationError(
                                "live undo rollback destination already exists"
                            )
                        os.replace(after, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.pop("undone_at", None)
                receipt["status"] = (
                    "rollback_incomplete"
                    if rollback_failures
                    else str(original_receipt.get("status", "applied"))
                )
                receipt["undo"] = {
                    "status": "rollback_incomplete" if rollback_failures else "failed",
                    "error_code": _operation_failure_code(
                        exc, stage="publication", tool_name="undo_modification"
                    ),
                    "failure": _sanitize_secret_text(str(exc))[:2048],
                    "rollback": {
                        "state": (
                            "incomplete"
                            if rollback_failures
                            else "restored"
                            if moved_after or moved_before
                            else "not_required"
                        ),
                        "performed": (moved_after or moved_before)
                        and not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(before_progress.source_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["undo_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "last-change undo failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.open_project(project_id)
        result["transaction_progress"] = _transaction_progress_projection(receipt)
        return result

    def build_release(
        self,
        project_id: str,
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="release build"
        )
        validation = project.state["last_validation"]
        if (
            project.state["status"]
            not in {"validated", "released", "release_failed", "interrupted"}
            or not isinstance(validation, dict)
            or not validation.get("candidate_ready")
        ):
            raise ValidationError(
                "release requires a passing engineering-candidate validation"
            )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        baseline_relative = validation.get("drc_evidence")
        baseline_revision = validation.get("source_design_revision")
        baseline_hash = validation.get("source_content_hash")
        if (
            not isinstance(baseline_relative, str)
            or not isinstance(baseline_revision, int)
            or not isinstance(baseline_hash, str)
            or baseline_revision != project.state["design_revision"]
            or baseline_hash != managed.design.content_hash()
        ):
            raise ValidationError(
                "release requires complete DRC baseline evidence bound to the current design; run validation again"
            )
        baseline_path = project.root / baseline_relative
        try:
            baseline_path.resolve(strict=False).relative_to(project.root.resolve())
        except ValueError as exc:
            raise ValidationError(
                "release DRC baseline path is outside the project"
            ) from exc
        release_id = new_run_id()
        output = project.root / "releases" / release_id
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before release")
            current.state["status"] = "releasing"
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "release.started",
                "Building manufacturing-candidate bundle",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]
        try:
            release = build_manufacturing_release(
                project.design_root,
                output,
                timeout=timeout,
                canonical_revision=expected_revision,
                design_revision=int(project.state["design_revision"]),
                baseline_drc_evidence=baseline_path,
                expected_baseline_design_revision=baseline_revision,
                expected_baseline_content_hash=baseline_hash,
            )
            verified = verify_manufacturing_release(release.root)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "release_failed",
                "release.failed",
                str(exc),
            )
            raise
        release_summary = {
            "id": release_id,
            "root": str(release.root),
            "manifest": str(release.manifest_path),
            "manifest_sha256": release.manifest_sha256,
            "archive": str(release.archive_path),
            "archive_sha256": release.archive_sha256,
            "candidate_ready": release.candidate_ready,
            "production_evidence_complete": release.production_evidence_complete,
            "production_ready": release.production_ready,
            "production_claimed": False,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "source_content_hash": managed.design.content_hash(),
            "offline_verification": verified.to_dict(),
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while release was running")
            current.state["status"] = "released"
            current.state["last_release"] = release_summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            text = (
                "Manufacturing-candidate bundle was built and verified offline; it is "
                "not a production or physical sign-off claim."
            )
            self._append_message(
                current.conversation,
                "assistant",
                "release",
                text,
                data={"release_id": release_id},
            )
            self._event(current.state, current.root, "release.complete", text)
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def verify_release(self, project_id: str) -> dict[str, Any]:
        project = self._open(project_id)
        release = project.state["last_release"]
        if not isinstance(release, dict) or not isinstance(release.get("id"), str):
            raise ValidationError("project has no manufacturing-candidate release")
        root = project.root / "releases" / release["id"]
        result = verify_manufacturing_release(root)
        return result.to_dict()
