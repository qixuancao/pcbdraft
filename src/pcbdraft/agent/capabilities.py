"""Typed capability catalog shared by agent clients and the durable runtime.

This is deliberately a small boundary, not a plugin framework.  UI clients ask
for a named PCB capability; the runtime maps it to one constrained application
job.  Model providers never receive arbitrary shell or filesystem authority.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentCapability:
    """One explicit action exposed by the interactive agent runtime."""

    name: str
    tool_name: str
    label: str


AGENT_CAPABILITIES = (
    AgentCapability("confirm", "generate_candidate", "Generate reviewed PCB"),
    AgentCapability("validate", "validate", "Run PCB validation"),
    AgentCapability("apply_change", "apply_candidate", "Apply reviewed change"),
    AgentCapability("discard_change", "discard_candidate", "Discard staged change"),
    AgentCapability("undo", "undo_last_change", "Undo last semantic change"),
    AgentCapability("release", "build_release", "Build release evidence"),
    AgentCapability("previews", "render_previews", "Render board previews"),
)

_CAPABILITIES_BY_NAME = {
    capability.name: capability for capability in AGENT_CAPABILITIES
}


def agent_capability(name: str) -> AgentCapability | None:
    """Resolve an interactive capability without accepting arbitrary job names."""

    return _CAPABILITIES_BY_NAME.get(name)
