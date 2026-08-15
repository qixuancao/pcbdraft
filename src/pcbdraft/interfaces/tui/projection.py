"""Pure projection of application state into terminal-friendly facts.

The Textual widgets consume this bounded model instead of learning the shape of
every application artifact.  Other clients may build a different projection
over the same durable project view and activity stream.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pcbdraft.agent.events import AgentActivity

StageState = Literal["pending", "active", "complete", "blocked", "failed"]


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    kind: str
    text: str


@dataclass(frozen=True)
class PipelineStage:
    name: str
    state: StageState


@dataclass(frozen=True)
class TuiProjection:
    project_id: str | None
    project_name: str
    status: str
    status_label: str
    purpose: str
    width_mm: float | int | None
    height_mm: float | int | None
    layers: int | None
    component_count: int | None
    net_count: int | None
    attention_required: int
    candidate_ready: bool | None
    production_evidence_complete: bool | None
    assurance: str
    messages: tuple[TranscriptMessage, ...]
    stages: tuple[PipelineStage, ...]

    @property
    def board_size(self) -> str:
        if self.width_mm is None or self.height_mm is None:
            return "Automatic"
        return f"{self.width_mm:g} × {self.height_mm:g} mm"

    @property
    def layer_label(self) -> str:
        return f"{self.layers} copper" if self.layers is not None else "Automatic"

    @property
    def readiness_label(self) -> str:
        if self.production_evidence_complete is True:
            return "External gate records complete"
        if self.candidate_ready is True:
            return "Engineering candidate"
        if self.candidate_ready is False:
            return "Checks need attention"
        return "Not checked yet"


_STATUS_LABELS = {
    "draft": "Draft",
    "interpreting": "Understanding request",
    "planning_required": "Planner required",
    "awaiting_confirmation": "Plan ready",
    "generating": "Generating KiCad",
    "generated": "Generated",
    "repairing": "Repairing",
    "repair_failed": "Repair failed",
    "change_ready": "Change ready",
    "validating": "Running checks",
    "validation_failed": "Checks failed",
    "validated": "Candidate validated",
    "releasing": "Building release",
    "released": "Release built",
    "interrupted": "Interrupted",
}

_TOOL_STAGE = {
    "pcb_requirements": 0,
    "pcb_circuit_plan": 1,
    "pcb_repair": 1,
    "kicad_generate": 2,
    "pcb_render": 3,
    "kicad_validate": 4,
}


def project_projection(
    view: Mapping[str, Any] | None,
    activities: Sequence[AgentActivity] = (),
    *,
    busy: bool = False,
) -> TuiProjection:
    """Build a stable, bounded terminal projection from one public project view."""

    root = view if isinstance(view, Mapping) else {}
    project = _mapping(root.get("project"))
    project_id = _text(project.get("id")) or None
    project_name = _text(project.get("name")) or "No project"
    status = _text(project.get("status")) or "ready"

    conversation = _mapping(root.get("conversation"))
    proposal = _mapping(conversation.get("proposal"))
    brief = _mapping(proposal.get("brief"))
    decisions = _mapping(proposal.get("decisions"))
    board = _mapping(brief.get("board")) or _mapping(decisions.get("board"))

    width = _number(board.get("width_mm"))
    height = _number(board.get("height_mm"))
    layers = board.get("layers", decisions.get("layers"))
    layers = layers if isinstance(layers, int) and layers > 0 else None

    component_count = _component_count(brief)
    nets = brief.get("nets")
    net_count = len(nets) if isinstance(nets, list) else None
    review_summary = _mapping(_mapping(brief.get("plan_review")).get("summary"))
    attention = review_summary.get("attention_required", 0)
    attention = attention if isinstance(attention, int) and attention >= 0 else 0

    validation = _mapping(_mapping(root.get("artifacts")).get("validation"))
    if not validation:
        validation = _mapping(_mapping(root.get("active_change")).get("validation"))
    candidate = _optional_bool(validation.get("candidate_ready"))
    production_evidence = _optional_bool(validation.get("production_evidence_complete"))
    assurance = _text(validation.get("assurance")) or _text(proposal.get("assurance"))
    assurance = assurance or "provisional"

    messages: list[TranscriptMessage] = []
    raw_messages = conversation.get("messages")
    if isinstance(raw_messages, list):
        for value in raw_messages[-300:]:
            item = _mapping(value)
            text = _text(item.get("text")).strip()
            if not text:
                continue
            messages.append(
                TranscriptMessage(
                    role=_text(item.get("role")) or "system",
                    kind=_text(item.get("kind")) or "message",
                    text=text,
                )
            )

    return TuiProjection(
        project_id=project_id,
        project_name=project_name,
        status=status,
        status_label=_STATUS_LABELS.get(status, status.replace("_", " ").title()),
        purpose=_text(brief.get("purpose")) or "Describe the board you want to build.",
        width_mm=width,
        height_mm=height,
        layers=layers,
        component_count=component_count,
        net_count=net_count,
        attention_required=attention,
        candidate_ready=candidate,
        production_evidence_complete=production_evidence,
        assurance=assurance,
        messages=tuple(messages),
        stages=_pipeline_stages(
            project_id=project_id,
            status=status,
            has_design=bool(_mapping(root.get("design"))),
            activities=activities,
            busy=busy,
        ),
    )


def _pipeline_stages(
    *,
    project_id: str | None,
    status: str,
    has_design: bool,
    activities: Sequence[AgentActivity],
    busy: bool,
) -> tuple[PipelineStage, ...]:
    names = ("Understand", "Plan", "Generate", "Route", "Check")
    states: list[StageState] = ["pending"] * len(names)
    if project_id is None:
        return tuple(
            PipelineStage(name, state)
            for name, state in zip(names, states, strict=True)
        )

    if status in {"draft", "interpreting"}:
        states[0] = "active"
    else:
        states[0] = "complete"

    if status == "planning_required":
        states[1] = "blocked"
    elif status in {
        "awaiting_confirmation",
        "generating",
        "generated",
        "repairing",
        "repair_failed",
        "change_ready",
        "validating",
        "validation_failed",
        "validated",
        "releasing",
        "released",
    }:
        states[1] = "complete"
    elif status not in {"draft", "interpreting"}:
        states[1] = "active"

    if has_design or status in {
        "generated",
        "change_ready",
        "validating",
        "validation_failed",
        "validated",
        "releasing",
        "released",
    }:
        states[2] = states[3] = "complete"
    elif status in {"generating", "repairing"}:
        states[2] = "active"

    if status == "validating":
        states[4] = "active"
    elif status in {"validated", "releasing", "released"}:
        states[4] = "complete"
    elif status in {"validation_failed", "repair_failed"}:
        states[4 if status == "validation_failed" else 1] = "failed"

    active_activity = next(
        (item for item in reversed(activities) if item.state in {"running", "queued"}),
        None,
    )
    if busy and active_activity is not None:
        active_index = _TOOL_STAGE.get(active_activity.tool)
        if active_index is not None:
            for index in range(active_index):
                if states[index] not in {"failed", "blocked"}:
                    states[index] = "complete"
            states[active_index] = "active"

    return tuple(
        PipelineStage(name, state) for name, state in zip(names, states, strict=True)
    )


def _component_count(brief: Mapping[str, Any]) -> int | None:
    bom = brief.get("bom")
    if not isinstance(bom, list):
        return None
    count = 0
    for entry in bom:
        quantity = _mapping(entry).get("quantity", 1)
        if isinstance(quantity, int) and quantity > 0:
            count += quantity
    return count


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float | int | None:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
