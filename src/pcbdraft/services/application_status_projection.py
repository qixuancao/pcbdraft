"""Read-only application diagnostics and engineering-stage projections.

The host application retains project evidence loading, repository state,
provider ownership, and every mutation.  This module only combines already
available observations into public status dictionaries and deliberately does
not import :mod:`pcbdraft.services.application`.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ApplicationStatusProjectionMixin:
    """Project runtime diagnostics and evidence-bound stage views."""

    def diagnostics(self) -> dict[str, Any]:
        from pcbdraft.model.tool_calls import provider_agent_protocol

        tools = self._status_doctor_report()
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
                "config": "Use `pcbdraft connect` or /connect; credentials stay in PCBDraft's private runtime directory.",
                "persistence": "Credential values are never written to project records or model receipts.",
                "kicad": (
                    "Run `pcbdraft setup` to detect a compatible KiCad 10.0.x "
                    "runtime and initialize missing stock-library tables."
                ),
            },
        }

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
            and self._status_validation_run_id_matches(validation_run_id)
            else "validation-run:none"
        )
        return {
            "project_id": project_id,
            "live_revision": int(project.state["revision"]),
            "design_revision": int(project.state["design_revision"]),
            "evidence_source": evidence_source,
            **stage.to_dict(),
        }
