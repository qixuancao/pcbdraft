"""Pure progress projections and convergence evidence for ApplicationService.

The application service remains the project and transaction authority. This
module only derives immutable progress, stage, and route-retry evidence from
values supplied by that service and never imports the application module.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.consistency import (
    NativeBoardProjection,
    NativeConsistencyReport,
)
from pcbdraft.kicad.routing import RoutingFailure
from pcbdraft.services.native_operations import _routing_failure_context
from pcbdraft.services.progress import (
    EvidenceCheck,
    MetricValue,
    ProgressVector,
    StageEvidence,
    StageProjection,
    compare_progress,
)


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
