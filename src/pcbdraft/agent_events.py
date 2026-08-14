"""UI-neutral event types for interactive PCBDraft agent sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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

    @property
    def terminal(self) -> bool:
        return self.status in {
            "completed",
            "completed_after_cancel",
            "failed",
            "cancelled",
            "interrupted",
        }
