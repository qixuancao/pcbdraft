"""Narrow application contracts consumed by the agent execution layer.

The concrete :class:`pcbdraft.services.application.ApplicationService` is the
product write authority.  Agent policy, durable orchestration, and model routing
depend only on these structural ports so they can be changed or tested without
creating a reverse import back into the service implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ProjectStatePort(Protocol):
    """Project state and durable-lock access needed by agent infrastructure."""

    @property
    def locks_root(self) -> Path: ...

    def project_root(self, project_id: str) -> Path: ...

    def open_project(self, project_id: str) -> dict[str, Any]: ...


class PCBToolServicePort(ProjectStatePort, Protocol):
    """The fixed application operations exposed through the PCB tool registry."""

    def send_message(
        self,
        project_id: str,
        text: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def validate_project(
        self,
        project_id: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def prepare_agent_repair(
        self,
        project_id: str,
        feedback: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def apply_modification(
        self,
        project_id: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def discard_modification(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def undo_last_modification(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def generate_project_previews(
        self,
        project_id: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    def build_release(
        self,
        project_id: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]: ...


class ModelRoutingPort(ProjectStatePort, Protocol):
    """The local state and provider configuration a model router may inspect."""

    @property
    def provider(self) -> object: ...


class AgentServicePort(PCBToolServicePort, ModelRoutingPort, Protocol):
    """Complete service boundary needed by default agent orchestration."""


class LegacyRuntimeServicePort(PCBToolServicePort, Protocol):
    """Additional progress recording used by the legacy synchronous runtime."""

    def record_progress(
        self,
        project_id: str,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None: ...
