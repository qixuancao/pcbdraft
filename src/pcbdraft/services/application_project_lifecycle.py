"""Project draft and empty-project creation workflows.

The host application remains the repository and project-store authority. It
supplies adapters for identity generation, private staging, record writes,
materialization, locking, publication, and cleanup so historical patch points
remain available without a reverse import.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import BoardSpec, Design, Scope
from pcbdraft.domain.parts import PartGraph
from pcbdraft.services.application_project_store import (
    APP_PROJECT_SCHEMA,
    APP_PROJECT_VERSION,
    CONVERSATION_SCHEMA,
    CONVERSATION_VERSION,
    ApplicationProject,
)
from pcbdraft.services.managed import EmptyDesignRequest


class ApplicationProjectLifecycleMixin:
    """Create private draft records and publish new project identities."""

    @staticmethod
    def _project_lifecycle_sanitize_secret_text(value: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_safe_text(value: Any, field: str, *, limit: int) -> str:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_slug(value: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_token_hex(length: int) -> str:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_mkdtemp(*, prefix: str, dir: Path) -> str:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_chmod(path: Path, mode: int) -> None:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_timestamp() -> str:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_make_directory(path: Path) -> Path:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_atomic_write_json(path: Path, value: Any) -> None:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_rmtree(path: Path, *, ignore_errors: bool) -> None:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_replace(source: Path, destination: Path) -> None:
        raise NotImplementedError

    @staticmethod
    def _project_lifecycle_materialize_managed_design(
        request: EmptyDesignRequest,
        design: Design,
        output: Path,
        *,
        graph: PartGraph,
    ) -> Any:
        raise NotImplementedError

    def _prepare_private_draft(self, name: str) -> tuple[str, Path, ApplicationProject]:
        """Build draft records in a private directory that no project ID exposes."""

        clean_name = self._project_lifecycle_sanitize_secret_text(
            self._project_lifecycle_safe_text(name, "project name", limit=512)
        )
        project_id = f"{self._project_lifecycle_slug(clean_name)}-{self._project_lifecycle_token_hex(4)}"
        target = self.projects_root / project_id
        temporary = Path(
            self._project_lifecycle_mkdtemp(
                prefix=f".{project_id}.creating-", dir=self.projects_root
            )
        )
        self._project_lifecycle_chmod(temporary, 0o700)
        created_at = self._project_lifecycle_timestamp()
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
                self._project_lifecycle_make_directory(temporary / name_value)
            self._project_lifecycle_atomic_write_json(temporary / "project.json", state)
            self._project_lifecycle_atomic_write_json(
                temporary / "conversation.json", conversation
            )
        except BaseException:
            if temporary.exists():
                self._project_lifecycle_rmtree(temporary, ignore_errors=True)
            raise
        return (
            project_id,
            target,
            ApplicationProject(
                root=temporary,
                state=state,
                conversation=conversation,
            ),
        )

    def create_draft(self, name: str) -> dict[str, Any]:
        """Create only the local conversation record; no engineering files exist yet."""

        project_id, target, project = self._prepare_private_draft(name)
        try:
            with self._project_lifecycle_resource_lock(target, self.locks_root):
                if target.exists() or target.is_symlink():
                    raise ValidationError("application project identity collision")
                self._project_lifecycle_replace(project.root, target)
        except BaseException:
            if project.root.exists():
                self._project_lifecycle_rmtree(project.root, ignore_errors=True)
            raise
        return self.open_project(project_id)

    def create_empty_project(self, name: str) -> dict[str, Any]:
        """Create and publish an empty synchronized semantic/KiCad project."""

        project_id, target, project = self._prepare_private_draft(name)
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
            name=str(project.state["name"]),
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
            generated = self._project_lifecycle_materialize_managed_design(
                request,
                design,
                project.design_root,
                graph=PartGraph.bundled(),
            )
            project.state["status"] = "generated"
            project.state["revision"] = 1
            project.state["design_revision"] = 1
            project.state["updated_at"] = self._project_lifecycle_timestamp()
            self._event(
                project.state,
                project.root,
                "project.synchronized_empty",
                "Created an empty synchronized semantic and KiCad project",
            )
            self._write_records(project.root, project.state, project.conversation)
            with self._project_lifecycle_resource_lock(target, self.locks_root):
                if target.exists() or target.is_symlink():
                    raise ValidationError("application project identity collision")
                self._project_lifecycle_replace(project.root, target)
        except BaseException:
            # Only the private staging directory belongs to this failed creator.
            # A published identity is never recursively removed here.
            if project.root.exists():
                self._project_lifecycle_rmtree(project.root, ignore_errors=True)
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
