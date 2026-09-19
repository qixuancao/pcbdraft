"""Read-only PCB tool, evidence, installed-library, and part inspection.

The host application remains the project and repository authority. It installs
late-bound adapters for managed-project and bounded JSON reads so historical
``services.application`` patch points remain effective without a reverse
import.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.services.application_project_store import ApplicationProject


def _unconfigured(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("application tool-inspection hooks are not configured")


open_managed_project: Callable[..., Any] = _unconfigured
load_json_limited: Callable[..., Any] = _unconfigured
_transaction_inspection_file_limit: Callable[[], int] = _unconfigured
_transaction_inspection_item_limit: Callable[[], int] = _unconfigured
_app_file_limit: Callable[[], int] = _unconfigured
_transaction_inspection_depth_valid: Callable[[object], bool] = _unconfigured


def _configure_legacy_application_hooks(
    *,
    open_managed_project_hook: Callable[..., Any],
    load_json_limited_hook: Callable[..., Any],
    transaction_inspection_file_limit_hook: Callable[[], int],
    transaction_inspection_item_limit_hook: Callable[[], int],
    app_file_limit_hook: Callable[[], int],
    transaction_inspection_depth_valid_hook: Callable[[object], bool],
) -> None:
    """Install late-bound adapters owned by the application module."""

    global open_managed_project
    global load_json_limited
    global _transaction_inspection_file_limit
    global _transaction_inspection_item_limit
    global _app_file_limit
    global _transaction_inspection_depth_valid

    open_managed_project = open_managed_project_hook
    load_json_limited = load_json_limited_hook
    _transaction_inspection_file_limit = transaction_inspection_file_limit_hook
    _transaction_inspection_item_limit = transaction_inspection_item_limit_hook
    _app_file_limit = app_file_limit_hook
    _transaction_inspection_depth_valid = transaction_inspection_depth_valid_hook


class ApplicationToolInspectionMixin:
    """Inspect project/tool facts and register verified installed KiCad parts."""

    @staticmethod
    def _with_tool_result(
        view: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        detached = dict(view)
        detached["tool_result"] = result
        return detached

    def _inspect_pcb_tool(
        self, project_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._open(project_id)
        view = self._public_project(project)
        facts: dict[str, Any]
        if tool_name == "inspect_transaction":
            facts = self._inspect_transaction_artifact(
                project, str(arguments["artifact_id"])
            )
        elif tool_name == "inspect_events":
            facts = {"events": self.events(project_id)[-100:]}
        elif tool_name == "inspect_evidence":
            facts = {
                "artifacts": view["artifacts"],
                "attempts": view["attempts"],
                "individual_checks": self._retained_evidence(
                    project.root / "validation",
                    schema="pcbdraft-individual-check-receipt",
                ),
                "individual_renders": self._retained_evidence(
                    project.root / "previews",
                    schema="pcbdraft-preview-bundle",
                    require_single="renders",
                ),
                "individual_exports": self._retained_evidence(
                    project.root / "releases",
                    schema="pcbdraft-individual-manufacturing-export",
                ),
            }
        else:
            if not project.design_root.is_dir() or project.design_root.is_symlink():
                raise ValidationError("project has no synchronized design to inspect")
            managed = open_managed_project(project.design_root)
            managed.assert_synchronized()
            if tool_name == "inspect_design":
                facts = {
                    "design": managed.design.to_dict(),
                    "content_hash": managed.design.content_hash(),
                }
            elif tool_name == "inspect_board":
                facts = {
                    "board": managed.manifest["native_snapshots"]["board"],
                    "content_hash": managed.design.content_hash(),
                }
            elif tool_name == "inspect_component":
                component_id = str(arguments["component_id"])
                component = next(
                    (
                        item
                        for item in managed.design.components
                        if item.id == component_id
                    ),
                    None,
                )
                if component is None:
                    raise ValidationError(f"component is absent: {component_id}")
                facts = {
                    "component": component.to_dict(),
                    "nets": [
                        net.to_dict()
                        for net in managed.design.nets
                        if any(
                            endpoint.component == component_id
                            for endpoint in net.endpoints
                        )
                    ],
                }
            else:
                net_id = str(arguments["net_id"])
                net = next(
                    (item for item in managed.design.nets if item.id == net_id), None
                )
                if net is None:
                    raise ValidationError(f"net is absent: {net_id}")
                facts = {
                    "net": net.to_dict(),
                    "routes": [
                        item.to_dict()
                        for item in managed.design.native_intent.routes
                        if item.net == net_id
                    ],
                    "vias": [
                        item.to_dict()
                        for item in managed.design.native_intent.vias
                        if item.net == net_id
                    ],
                }
        facts["project_id"] = project_id
        facts["revision"] = project.state["revision"]
        return self._with_tool_result(view, facts)

    @staticmethod
    def _inspect_transaction_artifact(
        project: ApplicationProject, artifact_id: str
    ) -> dict[str, Any]:
        """Read one current-project transaction receipt through a fixed boundary."""

        match = re.fullmatch(
            r"transaction:([0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})", artifact_id
        )
        if match is None:
            raise ValidationError("transaction artifact identity is invalid")
        transaction_id = match.group(1)
        transactions_root = project.root / "transactions"
        if transactions_root.is_symlink() or not transactions_root.is_dir():
            raise ValidationError("transaction artifact is unavailable")
        transaction = transactions_root / transaction_id
        receipt_path = transaction / "receipt.json"
        if (
            transaction.is_symlink()
            or not transaction.is_dir()
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            raise ValidationError("transaction artifact is unavailable")
        receipt = load_json_limited(receipt_path, _transaction_inspection_file_limit())
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema")
            not in {
                "pcbdraft-flat-operation-receipt",
                "pcbdraft-kicad-part-registration-receipt",
            }
            or receipt.get("version") not in {1, 2}
            or receipt.get("status") not in {"preparing", "noop", "applied", "failed"}
            or not isinstance(receipt.get("operation"), str)
            or not receipt["operation"]
            or not _transaction_inspection_depth_valid(receipt)
        ):
            raise ValidationError("transaction artifact receipt is invalid")

        def bounded_list(name: str) -> list[Any]:
            values = receipt.get(name)
            if not isinstance(values, list):
                return []
            return copy.deepcopy(values[: _transaction_inspection_item_limit()])

        detail = {
            key: copy.deepcopy(receipt[key])
            for key in (
                "schema",
                "version",
                "status",
                "operation",
                "error_code",
                "failure",
                "baseline_revision",
                "baseline_design_revision",
                "candidate_revision",
                "committed_revision",
                "committed_design_revision",
                "consistency_passed",
                "intended_delta",
                "native_delta",
                "routing_failure",
                "rollback_performed",
                "rollback",
                "progress_delta",
                "stage_before",
                "stage_after",
                "convergence",
                "transaction_scope",
            )
            if key in receipt
        }
        detail["postconditions"] = bounded_list("postconditions")
        artifacts = receipt.get("artifact")
        detail["available_details"] = (
            sorted(str(key) for key in artifacts)[
                : _transaction_inspection_item_limit()
            ]
            if isinstance(artifacts, Mapping)
            else []
        )
        return {
            "artifact_id": artifact_id,
            "detail": detail,
            "detail_truncated": bool(
                isinstance(receipt.get("postconditions"), list)
                and len(receipt["postconditions"])
                > _transaction_inspection_item_limit()
            ),
        }

    @staticmethod
    def _retained_evidence(
        root: Path,
        *,
        schema: str,
        require_single: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded completed evidence receipts without trusting paths."""

        if root.is_symlink() or not root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for candidate in sorted(root.iterdir(), reverse=True):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            receipt_path = candidate / "receipt.json"
            try:
                receipt = load_json_limited(receipt_path, _app_file_limit())
            except PCBDraftError:
                continue
            if (
                not isinstance(receipt, dict)
                or receipt.get("schema") != schema
                or receipt.get("status") != "complete"
            ):
                continue
            if require_single is not None:
                selected = receipt.get(require_single)
                if not isinstance(selected, list) or len(selected) != 1:
                    continue
            record = dict(receipt)
            record["run_id"] = candidate.name
            record["receipt"] = receipt_path.relative_to(root.parent).as_posix()
            records.append(record)
            if len(records) >= 100:
                break
        return records

    def _inspect_library_tool(
        self, project_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        facts = self.inspect_installed_library(tool_name, arguments)
        return self._with_tool_result(self.open_project(project_id), facts)

    @staticmethod
    def inspect_installed_library(
        tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Read installed KiCad facts without reading or selecting a project."""

        if tool_name in {"search_symbols", "describe_symbol"}:
            from pcbdraft.agent.part_resolver import LocalKiCadPartResolver

            resolver = LocalKiCadPartResolver()
            if tool_name == "search_symbols":
                facts: dict[str, Any] = {
                    "query": arguments["query"],
                    "symbols": list(
                        resolver.find_ids(str(arguments["query"]), limit=24)
                    ),
                }
            else:
                facts = resolver.describe(str(arguments["symbol"])).to_dict()
        else:
            from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver

            footprint_resolver = LocalKiCadFootprintResolver()
            facts = (
                {
                    "query": arguments["query"],
                    "footprints": [
                        item.to_dict()
                        for item in footprint_resolver.find(
                            str(arguments["query"]), limit=24
                        )
                    ],
                }
                if tool_name == "search_footprints"
                else footprint_resolver.describe(str(arguments["footprint"])).to_dict()
            )
        return facts

    def _inspect_part_tool(
        self, project_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self._open(project_id)
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized part catalog")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        if tool_name == "search_parts":
            matches = managed.graph.search(str(arguments["query"]), limit=24)
            facts: dict[str, Any] = {
                "query": arguments["query"],
                "available_count": len(managed.graph),
                "matched_count": len(matches),
                "parts": [
                    {
                        "id": part.id,
                        "kind": part.kind,
                        "description": part.description,
                        "symbol": part.symbol,
                        "footprint": part.footprint,
                        "trust": part.trust,
                    }
                    for part in matches
                ],
            }
        else:
            part = managed.graph.get(str(arguments["part_id"]))
            facts = {
                "available_count": len(managed.graph),
                "part": part.to_dict(),
            }
        facts["project_id"] = project_id
        facts["revision"] = project.state["revision"]
        return self._with_tool_result(self._public_project(project), facts)
