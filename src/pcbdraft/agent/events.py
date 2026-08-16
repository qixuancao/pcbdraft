"""UI-neutral event types for interactive PCBDraft agent sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pcbdraft.agent.turns import ToolRunRecord, ToolRunStatus

ActivityState = Literal["queued", "running", "completed", "failed", "info"]

_TOOL_PRESENTATION = {
    "provider": ("pcb_requirements", "Understanding board requirements"),
    "plan": ("pcb_circuit_plan", "Designing the circuit topology"),
    "planning": ("pcb_circuit_plan", "Designing the circuit topology"),
    "repair": ("pcb_repair", "Revising the circuit plan"),
    "generation": ("kicad_generate", "Generating the KiCad project"),
    "preview": ("pcb_render", "Rendering board previews"),
    "validation": ("kicad_validate", "Running PCB checks"),
    "release": ("pcb_export", "Building the release bundle"),
    "operation": ("pcb_project", "Recovering project state"),
    "job": ("agent_turn", "Running agent turn"),
}

_REGISTERED_TOOL_PRESENTATION = {
    "pcb_plan_request": "Understanding the board request",
    "pcb_generate_candidate": "Generating the KiCad project",
    "pcb_validate": "Checking the PCB candidate",
    "pcb_repair_candidate": "Repairing the PCB candidate",
    "pcb_apply_candidate": "Applying the checked PCB change",
    "pcb_discard_candidate": "Discarding the staged PCB change",
    "pcb_undo_last_change": "Restoring the previous PCB design",
    "pcb_render_previews": "Rendering board previews",
    "pcb_build_release": "Building release evidence",
}


@dataclass(frozen=True)
class AgentActivity:
    """One normalized piece of agent or PCB-tool activity."""

    sequence: int
    kind: str
    tool: str
    label: str
    message: str
    state: ActivityState
    level: str
    created_at: str
    source: str | None = None
    turn_id: str | None = None
    tool_call_id: str | None = None
    effect: str | None = None
    risk: str | None = None
    arguments: Mapping[str, Any] | None = None
    args_hash: str | None = None
    before_revision: int | None = None
    after_revision: int | None = None
    result: Mapping[str, Any] | None = None

    @classmethod
    def from_project_event(cls, value: dict[str, Any]) -> AgentActivity:
        """Normalize an application event without leaking UI concerns upstream."""

        kind = str(value.get("kind", "agent.event"))
        prefix = kind.partition(".")[0]
        tool, label = _TOOL_PRESENTATION.get(
            prefix, ("pcb_project", kind.replace(".", " "))
        )
        level = str(value.get("level", "info"))
        suffix = kind.rpartition(".")[2]
        state: ActivityState
        if level == "error" or suffix == "failed":
            state = "failed"
        elif suffix in {"complete", "ready", "completed"}:
            state = "completed"
        elif suffix in {"started", "running"}:
            state = "running"
        else:
            state = "info"
        sequence = value.get("sequence", 0)
        return cls(
            sequence=(sequence if isinstance(sequence, int) else 0),
            kind=kind,
            tool=tool,
            label=label,
            message=str(value.get("message", "")),
            state=state,
            level=level,
            created_at=str(value.get("created_at", "")),
        )

    @classmethod
    def from_tool_run(cls, value: ToolRunRecord) -> AgentActivity:
        """Project one durable tool-run transition into a compact UI activity."""

        states: dict[ToolRunStatus, ActivityState] = {
            ToolRunStatus.PROPOSED: "queued",
            ToolRunStatus.WAITING_APPROVAL: "queued",
            ToolRunStatus.RUNNING: "running",
            ToolRunStatus.COMPLETED: "completed",
            ToolRunStatus.FAILED: "failed",
            ToolRunStatus.INTERRUPTED: "failed",
            ToolRunStatus.DENIED: "info",
            ToolRunStatus.CANCELLED: "info",
        }
        label = _REGISTERED_TOOL_PRESENTATION.get(
            value.tool_name, value.tool_name.replace("_", " ")
        )
        message = (
            value.error
            or {
                ToolRunStatus.PROPOSED: "Tool call queued",
                ToolRunStatus.WAITING_APPROVAL: "Waiting for approval",
                ToolRunStatus.RUNNING: "Tool call running",
                ToolRunStatus.COMPLETED: "Tool call completed",
                ToolRunStatus.DENIED: "Tool call rejected",
                ToolRunStatus.CANCELLED: "Tool call cancelled",
                ToolRunStatus.INTERRUPTED: "Tool call interrupted",
                ToolRunStatus.FAILED: "Tool call failed",
            }[value.status]
        )
        return cls(
            sequence=0,
            kind=f"tool.{value.status.value}",
            tool=value.tool_name,
            label=label,
            message=message,
            state=states[value.status],
            level=("error" if value.status is ToolRunStatus.FAILED else "info"),
            created_at=value.completed_at or value.started_at or value.created_at,
            source=value.source,
            turn_id=value.turn_id,
            tool_call_id=value.tool_call_id,
            effect=value.effect,
            risk=value.risk,
            arguments=dict(value.arguments),
            args_hash=value.args_hash,
            before_revision=value.before_revision,
            after_revision=value.after_revision,
            result=dict(value.result) if value.result is not None else None,
        )


@dataclass(frozen=True)
class AgentUpdate:
    """Incremental runtime update consumed by a TUI or another local surface."""

    project_id: str
    job_id: str
    action: str
    status: str
    activities: tuple[AgentActivity, ...] = ()
    view: dict[str, Any] | None = None
    error: str | None = None
    turn_id: str | None = None
    turn_status: str | None = None
    pending_approval: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {
            "completed",
            "completed_after_cancel",
            "failed",
            "cancelled",
            "interrupted",
        }
