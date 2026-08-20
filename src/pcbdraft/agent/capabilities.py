"""Typed capability catalog for the legacy TUI action palette.

This is the small compatibility catalog behind the durable TUI job path and
its explicit shortcut commands. It maps a named capability to one constrained
application job. Model providers receive the canonical flat registry from
``pcbdraft.agent.tooling``; this human shortcut catalog is never exported.
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
