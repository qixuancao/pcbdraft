"""Central autonomy and approval policy for PCB tool calls.

Call producers may propose work, but they do not decide whether it is allowed.
This module keeps that decision local and transport-independent so a model,
deterministic policy, TUI command, or MCP client receives identical treatment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pcbdraft.agent.tooling import PCBToolExecutor, ToolCall, ToolResult, ToolSpec
from pcbdraft.core.errors import ValidationError

PermissionMode = Literal["workspace", "review", "read_only"]
PermissionAction = Literal["allow", "ask", "deny"]


class ToolPermissionError(ValidationError):
    """A policy stop that must not be mistaken for an engineering failure."""


@dataclass(frozen=True)
class PermissionVerdict:
    """One auditable policy decision made before local tool dispatch."""

    action: PermissionAction
    reason: str


class PermissionBroker:
    """Decide tool authority without relying on presentation-layer state.

    ``workspace`` mirrors coding-agent ergonomics: requested operations confined
    to the current local PCB project may proceed, including reversible writes.
    ``review`` pauses autonomous authoritative writes. A trusted client may bind
    an explicit user action to one call; the untrusted ``call.source`` label is
    never sufficient by itself. ``read_only`` refuses every current PCB tool
    because all of them retain at least conversation or evidence state.
    """

    def __init__(self, mode: PermissionMode = "workspace") -> None:
        if not isinstance(mode, str) or mode not in {
            "workspace",
            "review",
            "read_only",
        }:
            raise ValidationError("unknown PCB agent permission mode")
        self.mode = mode

    def decide(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        trusted_user_action: bool = False,
    ) -> PermissionVerdict:
        """Return the centralized decision for one revision-bound call."""

        if call.name not in {spec.name, spec.external_name}:
            raise ValidationError(
                "PCB permission decision received a mismatched tool spec"
            )
        if trusted_user_action and call.source != "user":
            raise ValidationError(
                "only a trusted user action may satisfy explicit PCB approval"
            )
        if self.mode == "read_only":
            return PermissionVerdict(
                "deny",
                "read-only mode does not permit durable PCB project changes",
            )
        if trusted_user_action:
            return PermissionVerdict(
                "allow",
                "a trusted client explicitly bound this bounded PCB tool to the user action",
            )
        if self.mode == "review" and (
            spec.effect == "authoritative_write" or spec.risk == "high"
        ):
            return PermissionVerdict(
                "ask",
                "review mode requires approval before an authoritative PCB write",
            )
        return PermissionVerdict(
            "allow",
            "the call is an in-scope operation confined to the current PCB project",
        )


class PCBToolGateway:
    """The authorized product entrypoint around the policy-independent executor.

    Durable approval workflows may persist an ``ask`` verdict before invoking
    the low-level executor.  Simpler clients should use this gateway so policy
    cannot accidentally be omitted from their dispatch path.
    """

    def __init__(
        self, executor: PCBToolExecutor, permissions: PermissionBroker
    ) -> None:
        self.executor = executor
        self.permissions = permissions

    def execute(
        self,
        call: ToolCall,
        *,
        timeout: float,
        observed_view: Mapping[str, Any] | None = None,
        trusted_user_action: bool = False,
    ) -> ToolResult:
        spec = self.executor.registry.resolve(call.name)
        verdict = self.permissions.decide(
            call,
            spec,
            trusted_user_action=trusted_user_action,
        )
        if verdict.action != "allow":
            raise ToolPermissionError(verdict.reason)
        return self.executor.execute(
            call,
            timeout=timeout,
            observed_view=observed_view,
        )
