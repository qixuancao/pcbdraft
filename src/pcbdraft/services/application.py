"""Authoritative project and conversation service shared by terminal and web UIs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.agent.compiler import compile_agent_plan, planner_symbol_context
from pcbdraft.agent.plan import (
    AgentDesignRequest,
    CircuitPlan,
)
from pcbdraft.agent.repair import (
    normalize_repair_feedback,
    user_revision_feedback,
    validation_feedback_from_levels,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import (
    atomic_write_json,
    load_json_limited,
    make_directory,
)
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.repository import (
    ProjectRepository,
    configure_repository,
    current_repository,
    explicit_repository,
)
from pcbdraft.core.runs import new_run_id, utc_timestamp
from pcbdraft.domain.component_qualification import (
    COMPONENT_QUALIFICATION_SCHEMA,
    qualify_components,
)
from pcbdraft.domain.ir import BoardSpec, Design, Scope, canonical_json_bytes
from pcbdraft.domain.operations import (
    ChangeSet,
    apply_change_set,
    semantic_diff,
)
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.scope import evaluate_scope
from pcbdraft.kicad.previews import generate_preview, generate_previews
from pcbdraft.model.providers import (
    MAX_USER_MESSAGE_BYTES,
    IntentProvider,
    ProviderContext,
    resolve_provider,
)
from pcbdraft.services.doctor import doctor_report
from pcbdraft.services.managed import (
    EmptyDesignRequest,
    load_generation_request,
    materialize_managed_design,
    open_managed_project,
)
from pcbdraft.verification.release import (
    build_manufacturing_release,
    export_manufacturing_output,
    verify_manufacturing_release,
)
from pcbdraft.verification.validation import (
    run_individual_check,
    validate_managed_project,
)

APP_PROJECT_SCHEMA = "pcbdraft-application-project"
APP_PROJECT_VERSION = 1
CONVERSATION_SCHEMA = "pcbdraft-conversation-record"
CONVERSATION_VERSION = 1
ATTEMPT_SCHEMA = "pcbdraft-generation-attempt"
ATTEMPT_VERSION = 2
_ATTEMPT_FIELDS = {
    "schema",
    "version",
    "id",
    "status",
    "phase",
    "runtime",
    "assurance",
    "started_at",
    "completed_at",
    "part_ids",
    "requested_parts",
    "files",
    "error",
}
APP_FILE_LIMIT = 4 * 1024 * 1024
PENDING_REQUEST_NAME = "pending-agent-request.json"
PENDING_PLAN_NAME = "pending-circuit-plan.json"
PENDING_DESIGN_NAME = "pending-design.pcbir.json"
PENDING_PARTS_NAME = "pending-parts.pcbdraft.json"
MAX_MESSAGES = 2_000
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_TRANSIENT_STATES = {
    "interpreting",
    "generating",
    "repairing",
    "validating",
    "releasing",
    "applying_change",
}
_STATE_FIELDS = {
    "schema",
    "version",
    "id",
    "name",
    "created_at",
    "updated_at",
    "status",
    "provider",
    "revision",
    "design_revision",
    "event_sequence",
    "active_transaction",
    "last_transaction",
    "last_validation",
    "last_preview",
    "last_release",
}
_CONVERSATION_FIELDS = {
    "schema",
    "version",
    "messages",
    "proposal",
    "decisions",
}


@dataclass(frozen=True)
class ApplicationProject:
    root: Path
    state: dict[str, Any]
    conversation: dict[str, Any]

    @property
    def design_root(self) -> Path:
        return self.root / "design"


def default_application_home() -> Path:
    """Return the persistent PCB project repository for normal launches.

    ``PCBDRAFT_HOME`` remains an explicit compatibility and automation override.
    It is never inferred from the shell's current directory.
    """

    configured = os.environ.get("PCBDRAFT_HOME")
    if configured:
        return Path(configured).expanduser()
    return current_repository().root


def _safe_text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    normalized = value.replace("\x00", "").strip()
    if len(normalized.encode("utf-8")) > limit:
        raise ValidationError(f"{field} exceeds the {limit} byte limit")
    return normalized


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "project"
    return slug[:56].rstrip("-") or "project"


def _initial_stackup_layers(request: str) -> int:
    """Infer a provisional stackup only when the planner returned no selection."""

    words = request.casefold()
    if any(token in words for token in ("ddr", "pcie", "serdes", "high-speed", "高速")):
        return 6
    if any(
        token in words
        for token in (
            "rf",
            "antenna",
            "射频",
            "天线",
            "usb",
            "ethernet",
            "high-power",
            "高功率",
        )
    ):
        return 4
    return 2


# Historical internal name retained locally while public callers use the core
# utility.  Keeping redaction below the application layer prevents protocol
# adapters and durable agent records from importing this concrete service.
_sanitize_secret_text = sanitize_user_text


def _public_readiness_record(value: Any) -> Any:
    """Normalize legacy records without mutating retained audit artifacts."""

    if not isinstance(value, dict):
        return value
    result = dict(value)
    result.setdefault(
        "production_evidence_complete", value.get("production_ready") is True
    )
    result["production_ready"] = False
    result["production_claimed"] = False
    return result


class ApplicationService:
    """Single write authority for product projects and their engineering runtime."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        provider_name: str = "auto",
        provider: IntentProvider | None = None,
    ) -> None:
        # A caller-provided workspace exists for isolated automation and tests.
        # Normal product launches always resolve the persisted PCB repository;
        # neither path depends on the process working directory.
        repository: ProjectRepository
        if workspace is not None:
            repository = explicit_repository(workspace)
        elif os.environ.get("PCBDRAFT_HOME"):
            repository = explicit_repository(default_application_home())
        else:
            repository = current_repository()
        self._use_repository(repository)
        self.provider = provider or resolve_provider(provider_name)
        self._recover_interrupted_projects()

    def set_repository(self, directory: str | Path) -> ProjectRepository:
        """Persist and start using a new normal project repository.

        This is intentionally unavailable for callers that supplied an explicit
        workspace.  Those callers use an isolated automation location and must
        restart without ``--workspace`` before changing the user's persistent
        product repository.
        """

        if self.repository.source == "explicit":
            raise ValidationError(
                "this session uses an explicit workspace; restart PCBDraft without "
                "--workspace before changing the persistent project repository"
            )
        repository = configure_repository(directory)
        self._use_repository(repository)
        self._recover_interrupted_projects()
        return repository

    def _use_repository(self, repository: ProjectRepository) -> None:
        """Adopt an already validated repository without changing its pointer."""

        self.repository = repository
        self.root = repository.root
        self.repository_source = repository.source
        self.repository_configured_now = repository.configured_now
        self.projects_root = make_directory(self.root / "projects")
        self.locks_root = make_directory(self.root / "locks")

    @staticmethod
    def _bind_expected_revision(
        project: ApplicationProject,
        expected_revision: int | None,
        *,
        operation: str,
    ) -> int:
        """Bind a mutation to the caller's snapshot before any expensive work."""

        if expected_revision is None:
            return int(project.state["revision"])
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValidationError("expected project revision must be non-negative")
        current_revision = int(project.state["revision"])
        if current_revision != expected_revision:
            raise ValidationError(
                f"project changed before {operation}: expected revision "
                f"{expected_revision}, current {current_revision}"
            )
        return expected_revision

    def diagnostics(self) -> dict[str, Any]:
        from pcbdraft.model.tool_calls import provider_agent_protocol

        tools = doctor_report()
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
                "config": "Use `pcbdraft connect` or /connect; credentials stay in PCBDraft's private Hermes home.",
                "persistence": "Credential values are never written to project records or model receipts.",
                "kicad": (
                    "Run `pcbdraft setup` to detect a compatible KiCad 10.0.x "
                    "runtime and initialize missing stock-library tables."
                ),
            },
        }

    def list_projects(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for candidate in sorted(self.projects_root.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                project = self._open_path(candidate)
            except PCBDraftError:
                continue
            result.append(self._summary(project))
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def create_project(self, name: str, request: str) -> dict[str, Any]:
        draft = self.create_draft(name)
        return self.send_message(draft["project"]["id"], request)

    def create_draft(self, name: str) -> dict[str, Any]:
        """Create only the local conversation record; no engineering files exist yet."""

        clean_name = _sanitize_secret_text(_safe_text(name, "project name", limit=512))
        project_id = f"{_slug(clean_name)}-{secrets.token_hex(4)}"
        target = self.projects_root / project_id
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{project_id}.creating-", dir=self.projects_root)
        )
        os.chmod(temporary, 0o700)
        created_at = utc_timestamp()
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
                make_directory(temporary / name_value)
            atomic_write_json(temporary / "project.json", state)
            atomic_write_json(temporary / "conversation.json", conversation)
            with ResourceLock(target, self.locks_root):
                if target.exists() or target.is_symlink():
                    raise ValidationError("application project identity collision")
                os.replace(temporary, target)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
        return self.open_project(project_id)

    def create_empty_project(self, name: str) -> dict[str, Any]:
        """Create and publish an empty synchronized semantic/KiCad project."""

        draft = self.create_draft(name)
        project_id = str(draft["project"]["id"])
        project = self._open(project_id)
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
            name=str(draft["project"]["name"]),
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
            generated = materialize_managed_design(
                request,
                design,
                project.design_root,
                graph=PartGraph.bundled(),
            )
        except BaseException:
            # Creation is one atomic operation: a native-generation failure
            # must not leave a misleading draft with the requested identity.
            shutil.rmtree(project.root, ignore_errors=True)
            raise
        try:
            with ResourceLock(project.root, self.locks_root):
                current = self._open(project_id)
                current.state["status"] = "generated"
                current.state["revision"] = 1
                current.state["design_revision"] = 1
                current.state["updated_at"] = utc_timestamp()
                self._event(
                    current.state,
                    current.root,
                    "project.synchronized_empty",
                    "Created an empty synchronized semantic and KiCad project",
                )
                self._write_records(current.root, current.state, current.conversation)
        except BaseException:
            # No project identity is published until both native and
            # application records describe the same synchronized revision.
            shutil.rmtree(project.root, ignore_errors=True)
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

    def open_project(self, project_id: str) -> dict[str, Any]:
        return self._public_project(self._open(project_id))

    def try_open_project_snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        """Read a self-consistent public view without blocking live UI polling.

        Project records and managed design directories are updated under the
        project lock.  A live client must use the same lock or it can otherwise
        observe a conversation from one revision and state/design files from
        another.  Returning ``None`` when the writer is busy lets callers keep
        streaming events and retry on their next poll.
        """

        root = self._project_path(project_id)
        lock = ResourceLock(root, self.locks_root, timeout=timeout)
        try:
            lock.acquire()
        except PCBDraftError as exc:
            if "resource is locked by another runtime process" in str(exc):
                return None
            raise
        try:
            return self._public_project(self._open_path(root))
        finally:
            lock.release()

    def project_root(self, project_id: str) -> Path:
        """Return a validated application-owned root for internal adapters."""

        return self._open(project_id).root

    def execute_pcb_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Execute one registry-bound flat PCB operation.

        The model-facing name is already resolved by the closed registry. This
        method owns the final service dispatch and always returns one public
        project view augmented with a bounded, fact-only operation result.
        """

        if tool_name == "inspect_project":
            return self._with_tool_result(
                self.open_project(project_id), {"inspection": "project"}
            )
        if tool_name in {
            "inspect_design",
            "inspect_component",
            "inspect_net",
            "inspect_board",
            "inspect_events",
            "inspect_evidence",
        }:
            return self._inspect_pcb_tool(project_id, tool_name, arguments)
        if tool_name in {
            "search_symbols",
            "describe_symbol",
            "search_footprints",
            "describe_footprint",
        }:
            return self._inspect_library_tool(project_id, tool_name, arguments)
        if tool_name in {"search_parts", "describe_part"}:
            return self._inspect_part_tool(project_id, tool_name, arguments)
        if tool_name == "register_kicad_part":
            return self.register_kicad_part(
                project_id,
                arguments["value"],
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {
            "check_semantics",
            "check_connectivity",
            "run_erc",
            "run_drc",
        }:
            return self.run_pcb_check(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {"render_schematic", "render_board", "render_3d"}:
            return self.render_pcb_output(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {
            "export_gerbers",
            "export_drill",
            "export_bom",
            "export_pick_place",
            "export_step",
        }:
            return self.export_pcb_output(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        return self.apply_pcb_operation(
            project_id,
            tool_name,
            arguments,
            timeout=timeout,
            expected_revision=expected_revision,
        )

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
        if tool_name == "inspect_events":
            facts: dict[str, Any] = {"events": self.events(project_id)[-100:]}
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
                receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
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

    def register_kicad_part(
        self,
        project_id: str,
        value: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Atomically publish one inspected local KiCad part contract."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="register_kicad_part"
        )
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized design to modify")
        authoritative = open_managed_project(project.design_root)
        authoritative.assert_synchronized()
        before_catalog_hash = hashlib.sha256(
            canonical_json_bytes(authoritative.graph.to_dict())
        ).hexdigest()
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-kicad-part-registration-receipt",
            "version": 1,
            "status": "preparing",
            "operation": "register_kicad_part",
            "created_at": utc_timestamp(),
            "baseline_revision": expected_revision,
            "before_hash": authoritative.design.content_hash(),
            "before_catalog_hash": before_catalog_hash,
            "part_id": value.get("id"),
        }
        atomic_write_json(receipt_path, receipt)
        try:
            from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver
            from pcbdraft.agent.part_resolver import LocalKiCadPartResolver

            symbol = LocalKiCadPartResolver().describe(str(value.get("symbol", "")))
            footprint = LocalKiCadFootprintResolver().describe(
                str(value.get("footprint", ""))
            )
            pins = value.get("pins")
            if not isinstance(pins, list):
                raise ValidationError("part pins must be an array")
            installed_pins = {item["number"]: item for item in symbol.pins}
            supplied_pins = {
                str(item.get("number")): item for item in pins if isinstance(item, dict)
            }
            if set(supplied_pins) != set(installed_pins):
                raise ValidationError(
                    "part pins must exactly cover the installed symbol pin numbers"
                )
            available_pads = set(footprint.pad_numbers)
            for number, installed in installed_pins.items():
                supplied = supplied_pins[number]
                if supplied.get("name") != installed["name"]:
                    raise ValidationError(
                        f"part pin {number} name differs from the installed symbol"
                    )
                if supplied.get("electrical_type") != installed["electrical_type"]:
                    raise ValidationError(
                        f"part pin {number} electrical type differs from the installed symbol"
                    )
                if supplied.get("footprint_pad") not in available_pads:
                    raise ValidationError(
                        f"part pin {number} maps to a missing footprint pad"
                    )
            part = PartGraph.installed_kicad_part(
                value, footprint_sha256=footprint.sha256
            )
            existing = authoritative.graph.get_optional(part.id)
            if existing is not None:
                if existing.to_dict() != part.to_dict():
                    raise ValidationError(
                        f"part id already exists with different facts: {part.id}"
                    )
                with ResourceLock(project.root, self.locks_root):
                    current = self._open(project_id)
                    if current.state["revision"] != expected_revision:
                        raise ValidationError(
                            "project changed while part registration was inspected"
                        )
                    current_design = open_managed_project(current.design_root)
                    current_design.assert_synchronized()
                    current_catalog_hash = hashlib.sha256(
                        canonical_json_bytes(current_design.graph.to_dict())
                    ).hexdigest()
                    if current_design.design.content_hash() != receipt["before_hash"]:
                        raise ValidationError(
                            "authoritative design changed before part no-op"
                        )
                    if current_catalog_hash != receipt["before_catalog_hash"]:
                        raise ValidationError(
                            "authoritative part catalog changed before part no-op"
                        )
                receipt.update(
                    {
                        "status": "noop",
                        "completed_at": utc_timestamp(),
                        "after_hash": authoritative.design.content_hash(),
                        "after_catalog_hash": before_catalog_hash,
                    }
                )
                atomic_write_json(receipt_path, receipt)
                return self._with_tool_result(
                    self.open_project(project_id),
                    {
                        "operation": "register_kicad_part",
                        "transaction_id": transaction_id,
                        "project_id": project_id,
                        "part": part.to_dict(),
                        "changed": False,
                        "catalog_count": len(authoritative.graph),
                    },
                )
            graph = authoritative.graph.merged([part], source=f"project:{project_id}")
            document = authoritative.design.to_dict()
            document["metadata"]["assurance"] = "provisional"
            candidate = Design.from_dict(document)
            after_catalog_hash = hashlib.sha256(
                canonical_json_bytes(graph.to_dict())
            ).hexdigest()
            receipt.update(
                {
                    "after_hash": candidate.content_hash(),
                    "after_catalog_hash": after_catalog_hash,
                    "symbol": symbol.to_dict(),
                    "footprint": footprint.to_dict(),
                }
            )
            atomic_write_json(receipt_path, receipt)
            request = load_generation_request(authoritative.requirements_path)
            materialize_managed_design(
                request,
                candidate,
                staged,
                graph=graph,
                plan=authoritative.plan,
                retain_failed_attempt=transaction / "failed-native",
                lock_timeout=min(10.0, timeout),
                auto_place=False,
                route_net_ids=frozenset(),
                allow_incomplete=True,
            )
            staged_project = open_managed_project(staged)
            staged_project.assert_synchronized()
            if staged_project.design.content_hash() != candidate.content_hash():
                raise ValidationError("staged semantic design hash changed")
            if (
                hashlib.sha256(
                    canonical_json_bytes(staged_project.graph.to_dict())
                ).hexdigest()
                != after_catalog_hash
            ):
                raise ValidationError("staged part catalog hash changed")
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            atomic_write_json(receipt_path, receipt)
            raise

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            moved_before = False
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while part registration was staged"
                    )
                current_design = open_managed_project(current.design_root)
                current_design.assert_synchronized()
                current_catalog_hash = hashlib.sha256(
                    canonical_json_bytes(current_design.graph.to_dict())
                ).hexdigest()
                if current_design.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed before part publication"
                    )
                if current_catalog_hash != receipt["before_catalog_hash"]:
                    raise ValidationError(
                        "authoritative part catalog changed before publication"
                    )
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                published = open_managed_project(current.design_root)
                published.assert_synchronized()
                receipt.update(
                    {
                        "status": "applied",
                        "applied_at": utc_timestamp(),
                        "manifest_hashes": published.manifest["hashes"],
                    }
                )
                atomic_write_json(receipt_path, receipt)
                current.state["status"] = "generated"
                current.state["revision"] += 1
                current.state["design_revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                current.state["last_validation"] = None
                current.state["last_preview"] = None
                current.state["last_release"] = None
                current.state["last_transaction"] = transaction_id
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "pcb.kicad_part_registered",
                    f"Registered installed KiCad part {part.id}",
                )
                self._write_records(current.root, current.state, current.conversation)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        failed_published = transaction / "failed-published"
                        if current.design_root.exists():
                            os.replace(current.design_root, failed_published)
                        if before.exists():
                            os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                    rollback_failures.append(rollback_exc)
                receipt["status"] = "failed"
                receipt["failed_at"] = utc_timestamp()
                receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "part publication failed and rollback was incomplete"
                    ) from exc
                raise
        return self._with_tool_result(
            self.open_project(project_id),
            {
                "operation": "register_kicad_part",
                "transaction_id": transaction_id,
                "project_id": project_id,
                "part": part.to_dict(),
                "symbol": symbol.to_dict(),
                "footprint": footprint.to_dict(),
                "changed": True,
                "catalog_count": len(graph),
                "before_catalog_hash": before_catalog_hash,
                "after_catalog_hash": after_catalog_hash,
                "revision": current.state["revision"],
                "design_revision": current.state["design_revision"],
            },
        )

    def run_pcb_check(
        self,
        project_id: str,
        kind: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Run and retain exactly one source-bound flat-toolbox check."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=kind
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        output = project.root / "validation" / run_id
        result = run_individual_check(
            managed,
            kind,
            output=output,
            timeout=timeout,
        )
        summary = {
            "run_id": run_id,
            "check": kind,
            "report": result.report_path.relative_to(project.root).as_posix(),
            "report_sha256": result.report_sha256,
            "state": result.state,
            "outcome": result.outcome,
            "design_content_hash": result.design_content_hash,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "production_ready": False,
            "production_claimed": False,
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the check was running")
            current_managed = open_managed_project(current.design_root)
            current_managed.assert_synchronized()
            if current_managed.design.content_hash() != result.design_content_hash:
                raise ValidationError("design changed while the check was running")
            current.state["last_validation"] = summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "pcb.check_complete",
                f"Completed individual PCB check {kind}",
                level="error" if result.outcome == "fail" else "info",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self._with_tool_result(
            self.open_project(project_id),
            {**summary, "revision": current.state["revision"]},
        )

    def render_pcb_output(
        self,
        project_id: str,
        kind: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Generate and retain only one requested preview family."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=kind
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        bundle = generate_preview(
            managed,
            project.root / "previews" / run_id,
            kind,
            timeout=timeout,
        )
        summary = {
            "run_id": run_id,
            "render": kind,
            "root": bundle.root.relative_to(project.root).as_posix(),
            "receipt": bundle.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": bundle.design_content_hash,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "files": {
                key: path.relative_to(project.root).as_posix()
                for key, path in bundle.files.items()
            },
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the preview was rendered")
            if (
                open_managed_project(current.design_root).design.content_hash()
                != bundle.design_content_hash
            ):
                raise ValidationError("design changed while the preview was rendered")
            current.state["last_preview"] = summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "pcb.render_complete",
                f"Completed individual PCB render {kind}",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self._with_tool_result(
            self.open_project(project_id),
            {**summary, "revision": current.state["revision"]},
        )

    def export_pcb_output(
        self,
        project_id: str,
        kind: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Generate and retain only one requested manufacturing export."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=kind
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        exported = export_manufacturing_output(
            managed,
            project.root / "releases" / run_id,
            kind,
            timeout=timeout,
        )
        summary = {
            "id": run_id,
            "export": kind,
            "root": exported.root.relative_to(project.root).as_posix(),
            "receipt": exported.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": exported.design_content_hash,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "artifacts": list(exported.artifacts),
            "production_ready": False,
            "production_claimed": False,
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the export was generated")
            if (
                open_managed_project(current.design_root).design.content_hash()
                != exported.design_content_hash
            ):
                raise ValidationError("design changed while the export was generated")
            current.state["last_release"] = summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "pcb.export_complete",
                f"Completed individual PCB export {kind}",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self._with_tool_result(
            self.open_project(project_id),
            {**summary, "revision": current.state["revision"]},
        )

    def apply_pcb_operation(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Stage, materialize, verify, and publish exactly one typed operation."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=tool_name
        )
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized design to modify")
        authoritative = open_managed_project(project.design_root)
        authoritative.assert_synchronized()
        operation = self._flat_semantic_operation(
            tool_name, arguments, authoritative.design
        )
        change_set = ChangeSet.from_dict(
            {
                "schema": "pcbdraft-change-set",
                "version": 1,
                "id": f"flat_{secrets.token_hex(6)}",
                "base_hash": authoritative.design.content_hash(),
                "intent": f"Apply concrete PCB operation {tool_name}.",
                "actor": "flat-pcb-toolbox",
                "operations": [operation],
                "provenance": [f"tool:{tool_name}"],
            }
        )
        candidate = apply_change_set(authoritative.design, change_set)
        request = load_generation_request(authoritative.requirements_path)
        graph = authoritative.graph.with_footprint_overrides(candidate)
        if isinstance(request, AgentDesignRequest):
            qualification = qualify_components(candidate, graph)
            if qualification.pad_mapping_failures:
                raise ValidationError(
                    "component qualification contains invalid local pad mappings"
                )
            document = candidate.to_dict()
            document["metadata"]["component_qualification_schema"] = (
                COMPONENT_QUALIFICATION_SCHEMA
            )
            document["metadata"]["component_qualification_hash"] = (
                qualification.sha256()
            )
            candidate = Design.from_dict(document)
        candidate_diff = semantic_diff(authoritative.design, candidate)
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-flat-operation-receipt",
            "version": 1,
            "status": "preparing",
            "operation": tool_name,
            "created_at": utc_timestamp(),
            "before_hash": authoritative.design.content_hash(),
            "after_hash": candidate.content_hash(),
            "baseline_revision": expected_revision,
        }
        atomic_write_json(receipt_path, receipt)
        atomic_write_json(transaction / "semantic-diff.json", candidate_diff)
        try:
            generated = materialize_managed_design(
                request,
                candidate,
                staged,
                graph=graph,
                plan=authoritative.plan,
                retain_failed_attempt=transaction / "failed-native",
                lock_timeout=min(10.0, timeout),
                auto_place=False,
                route_net_ids=(
                    frozenset({str(arguments["net_id"])})
                    if tool_name == "route_net"
                    else frozenset()
                ),
                allow_incomplete=True,
            )
            if tool_name == "route_net":
                candidate = self._retain_generated_route(
                    candidate,
                    str(arguments["net_id"]),
                    generated.pcb.routing,
                )
                candidate_diff = semantic_diff(authoritative.design, candidate)
                receipt["after_hash"] = candidate.content_hash()
                atomic_write_json(receipt_path, receipt)
                atomic_write_json(transaction / "semantic-diff.json", candidate_diff)
                os.replace(staged, transaction / "routing-probe")
                materialize_managed_design(
                    request,
                    candidate,
                    staged,
                    graph=graph,
                    plan=authoritative.plan,
                    retain_failed_attempt=transaction / "failed-native-final",
                    lock_timeout=min(10.0, timeout),
                    auto_place=False,
                    route_net_ids=frozenset(),
                    allow_incomplete=True,
                )
            staged_project = open_managed_project(staged)
            staged_project.assert_synchronized()
            if staged_project.design.content_hash() != candidate.content_hash():
                raise ValidationError("staged semantic design hash changed")
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            atomic_write_json(receipt_path, receipt)
            raise

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            moved_before = False
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while PCB operation was staged"
                    )
                current_design = open_managed_project(current.design_root)
                current_design.assert_synchronized()
                if current_design.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed before publication"
                    )
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                published = open_managed_project(current.design_root)
                published.assert_synchronized()
                receipt.update(
                    {
                        "status": "applied",
                        "applied_at": utc_timestamp(),
                        "manifest_hashes": published.manifest["hashes"],
                    }
                )
                atomic_write_json(receipt_path, receipt)
                current.state["status"] = "generated"
                current.state["revision"] += 1
                current.state["design_revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                current.state["last_validation"] = None
                current.state["last_preview"] = None
                current.state["last_release"] = None
                current.state["last_transaction"] = transaction_id
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "pcb.operation_applied",
                    f"Applied concrete PCB operation {tool_name}",
                )
                self._write_records(current.root, current.state, current.conversation)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        failed_published = transaction / "failed-published"
                        if current.design_root.exists():
                            os.replace(current.design_root, failed_published)
                        if before.exists():
                            os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                    rollback_failures.append(rollback_exc)
                receipt["status"] = "failed"
                receipt["failed_at"] = utc_timestamp()
                receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    # Preserve the publication failure; an applied receipt was
                    # never authoritative without the matching project record.
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "PCB operation publication failed and rollback was incomplete"
                    ) from exc
                raise
        result = {
            "operation": tool_name,
            "transaction_id": transaction_id,
            "before_hash": receipt["before_hash"],
            "after_hash": receipt["after_hash"],
            "design_revision": current.state["design_revision"],
            "revision": current.state["revision"],
            "changed": candidate_diff["summary"],
            "synchronized": True,
        }
        return self._with_tool_result(self.open_project(project_id), result)

    @staticmethod
    def _retain_generated_route(design: Design, net_id: str, routing: Any) -> Design:
        """Promote generated copper for one net into deterministic native intent."""

        net = next((item for item in design.nets if item.id == net_id), None)
        if net is None:
            raise ValidationError(f"net is absent: {net_id}")
        if net.name in routing.unrouted:
            raise ValidationError(f"router could not complete net: {net_id}")
        segments = [item for item in routing.segments if item.net == net.name]
        if len(net.endpoints) > 1 and not segments:
            raise ValidationError(f"router produced no copper for net: {net_id}")
        document = design.to_dict()
        native = document["native_intent"]

        def stable_id(prefix: str, value: dict[str, Any]) -> str:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:20]}"

        native["routes"] = [
            item for item in native["routes"] if item.get("net") != net_id
        ] + [
            {
                "id": stable_id(
                    "route",
                    {
                        "net": net_id,
                        "layer": item.layer,
                        "x1_mm": item.x1_mm,
                        "y1_mm": item.y1_mm,
                        "x2_mm": item.x2_mm,
                        "y2_mm": item.y2_mm,
                        "width_mm": item.width_mm,
                    },
                ),
                "net": net_id,
                "layer": item.layer,
                "x1_mm": item.x1_mm,
                "y1_mm": item.y1_mm,
                "x2_mm": item.x2_mm,
                "y2_mm": item.y2_mm,
                "width_mm": item.width_mm,
            }
            for item in segments
        ]
        native["vias"] = [
            item for item in native["vias"] if item.get("net") != net_id
        ] + [
            {
                "id": stable_id(
                    "via",
                    {
                        "net": net_id,
                        "x_mm": item.x_mm,
                        "y_mm": item.y_mm,
                        "diameter_mm": item.diameter_mm,
                        "drill_mm": item.drill_mm,
                        "from_layer": item.from_layer,
                        "to_layer": item.to_layer,
                    },
                ),
                "net": net_id,
                "x_mm": item.x_mm,
                "y_mm": item.y_mm,
                "diameter_mm": item.diameter_mm,
                "drill_mm": item.drill_mm,
                "from_layer": item.from_layer,
                "to_layer": item.to_layer,
            }
            for item in routing.vias
            if item.net == net.name
        ]
        return Design.from_dict(document)

    @staticmethod
    def _entry_mapping(value: Any, field: str) -> dict[str, Any]:
        """Convert one strict list-of-entries object into a unique mapping."""

        if not isinstance(value, dict) or set(value) != {"entries"}:
            raise ValidationError(f"{field} has an invalid object shape")
        entries = value["entries"]
        if not isinstance(entries, list) or not entries:
            raise ValidationError(f"{field}.entries must be a non-empty array")
        result: dict[str, Any] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"field", "value"}:
                raise ValidationError(f"{field}.entries[{index}] is malformed")
            name = entry["field"]
            if not isinstance(name, str) or not name or name in result:
                raise ValidationError(f"{field} contains a duplicate or invalid field")
            result[name] = entry["value"]
        return result

    @staticmethod
    def _parameter_mapping(value: Any, field: str) -> dict[str, Any]:
        """Convert strict named parameter entries into a domain mapping."""

        if not isinstance(value, list):
            raise ValidationError(f"{field} must be an array")
        result: dict[str, Any] = {}
        for index, entry in enumerate(value):
            if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
                raise ValidationError(f"{field}[{index}] is malformed")
            name = entry["name"]
            if not isinstance(name, str) or not name or name in result:
                raise ValidationError(f"{field} contains a duplicate or invalid name")
            result[name] = entry["value"]
        return result

    @classmethod
    def _flat_semantic_operation(
        cls, tool_name: str, arguments: dict[str, Any], design: Design
    ) -> dict[str, Any]:
        operation_id = f"op_{secrets.token_hex(6)}"
        args: dict[str, Any]
        expected: dict[str, Any] = {}
        op = tool_name
        if tool_name.startswith("add_") and tool_name in {
            "add_block",
            "add_component",
            "add_net",
        }:
            value = copy.deepcopy(arguments["value"])
            if tool_name == "add_block":
                value["components"] = []
                value["provenance"] = []
            elif tool_name == "add_net":
                value["endpoints"] = []
            args = {"value": value}
        elif tool_name in {"remove_block", "remove_component", "remove_net"}:
            key = tool_name.removeprefix("remove_") + "_id"
            if tool_name == "remove_net":
                net = next(
                    (item for item in design.nets if item.id == arguments[key]),
                    None,
                )
                if net is not None and net.name.upper() in {"GND", "GROUND", "VSS"}:
                    raise ValidationError(
                        "the required reference-plane net cannot be removed"
                    )
            args = {"id": arguments[key]}
        elif tool_name == "update_component":
            args = {
                "component_id": arguments["component_id"],
                "changes": cls._entry_mapping(arguments["changes"], "changes"),
            }
        elif tool_name in {"assign_footprint", "rename_net"}:
            args = dict(arguments)
        elif tool_name in {"connect_pin", "disconnect_pin"}:
            op = "connect" if tool_name == "connect_pin" else "disconnect"
            args = {
                "net_id": arguments["net_id"],
                "endpoint": {
                    "component": arguments["component_id"],
                    "pin": arguments["pin"],
                    "role": arguments["role"],
                },
            }
        elif any(
            tool_name.endswith(suffix)
            for suffix in ("power_domain", "interface", "constraint")
        ):
            collection = next(
                name
                for name in ("power_domain", "interface", "constraint")
                if tool_name.endswith(name)
            )
            if tool_name.startswith("remove_"):
                op = f"remove_{collection}"
                args = {"id": arguments["id"]}
            else:
                op = (
                    "upsert_constraint"
                    if collection == "constraint"
                    else f"upsert_{collection}"
                )
                value = copy.deepcopy(arguments["value"])
                if collection in {"interface", "constraint"}:
                    value["params"] = cls._parameter_mapping(
                        value["params"], f"{collection}.params"
                    )
                if collection == "constraint":
                    value["provenance"] = []
                entry_id = str(value["id"])
                exists = (
                    any(item.id == entry_id for item in design.power_domains)
                    if collection == "power_domain"
                    else any(item.id == entry_id for item in design.interfaces)
                    if collection == "interface"
                    else any(item.id == entry_id for item in design.constraints)
                )
                if tool_name.startswith("add_"):
                    if exists:
                        raise ValidationError(
                            f"cannot add existing {collection}: {entry_id}"
                        )
                    expected = {"absent": True}
                else:
                    if not exists:
                        raise ValidationError(
                            f"cannot update absent {collection}: {entry_id}"
                        )
                    expected = {"id": entry_id}
                args = {"value": value}
        elif tool_name == "update_board_rules":
            op = "update_board"
            args = {"changes": cls._entry_mapping(arguments["changes"], "changes")}
        elif tool_name == "set_board_outline":
            args = {
                "width_mm": float(arguments["width_mm"]),
                "height_mm": float(arguments["height_mm"]),
            }
        elif tool_name in {"place_footprint", "move_footprint", "rotate_footprint"}:
            op = "place_footprint"
            component_id = str(arguments["component_id"])
            component = next(
                (item for item in design.components if item.id == component_id), None
            )
            if component is None:
                raise ValidationError(f"component is absent: {component_id}")
            existing = component.placement
            args = {
                "component_id": component_id,
                "x_mm": float(
                    arguments.get("x_mm", existing.x_mm if existing else 0.0)
                ),
                "y_mm": float(
                    arguments.get("y_mm", existing.y_mm if existing else 0.0)
                ),
                "rotation_deg": float(
                    arguments.get(
                        "rotation_deg", existing.rotation_deg if existing else 0.0
                    )
                ),
                "side": str(
                    arguments.get("side", existing.side if existing else "front")
                ),
                "fixed": True,
            }
        elif tool_name == "unplace_footprint":
            args = {"component_id": arguments["component_id"]}
        elif tool_name in {"route_net", "unroute_net"}:
            net = next(
                (item for item in design.nets if item.id == arguments["net_id"]),
                None,
            )
            if net is None:
                raise ValidationError(f"net is absent: {arguments['net_id']}")
            if tool_name == "unroute_net" and net.name.upper() in {
                "GND",
                "GROUND",
                "VSS",
            }:
                raise ValidationError(
                    "the required reference-plane net cannot be unrouted"
                )
            args = {"net_id": arguments["net_id"]}
            if tool_name == "route_net":
                args.update({"segments": [], "vias": []})
        elif tool_name == "add_via":
            args = {
                "value": {
                    "id": arguments["via_id"],
                    "net": arguments["net_id"],
                    "x_mm": float(arguments["x_mm"]),
                    "y_mm": float(arguments["y_mm"]),
                    "diameter_mm": float(arguments["diameter_mm"]),
                    "drill_mm": float(arguments["drill_mm"]),
                    "from_layer": int(arguments["from_layer"]),
                    "to_layer": int(arguments["to_layer"]),
                }
            }
        elif tool_name == "remove_via":
            args = {"via_id": arguments["via_id"]}
        else:
            raise ValidationError(f"flat PCB write is not implemented: {tool_name}")
        return {
            "id": operation_id,
            "op": op,
            "args": args,
            "expected": expected,
            "reason": f"Concrete flat PCB tool {tool_name}.",
        }

    def record_progress(
        self,
        project_id: str,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        """Append a structured adapter/job event under the project write lock."""

        project = self._open(project_id)
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", kind):
            raise ValidationError("structured event kind is invalid")
        if level not in {"info", "warning", "error"}:
            raise ValidationError("structured event level is invalid")
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            # Progress events are presentation/audit state, not an engineering
            # mutation. Advancing the project revision here would immediately
            # stale a revision-bound tool call merely because the UI recorded
            # ``job.started`` or ``job.complete``.
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                kind,
                _safe_text(message, "event message", limit=4096),
                level=level,
            )
            self._write_records(current.root, current.state, current.conversation)

    def reply_message(
        self,
        project_id: str,
        text: str,
        *,
        turn_id: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        """Append one exactly-once conversational assistant reply.

        The reply is bound to its durable turn and sequence index: appending
        the same ``(turn_id, index)`` twice is a no-op, so a crash-resumed
        worker never duplicates a conversational message in the transcript.
        """

        clean = _sanitize_secret_text(
            _safe_text(text, "reply text", limit=MAX_USER_MESSAGE_BYTES)
        )
        project = self._open(project_id)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            conversation = current.conversation
            if turn_id is not None or index is not None:
                if (
                    not isinstance(turn_id, str)
                    or isinstance(index, bool)
                    or not (isinstance(index, int) and index >= 0)
                ):
                    raise ValidationError("reply delivery binding is invalid")
                for message in conversation["messages"]:
                    data = message.get("data") if isinstance(message, dict) else None
                    if not isinstance(data, dict):
                        continue
                    if data.get("turn_id") == turn_id and data.get("index") == index:
                        return self._public_project(current)
            self._append_message(
                conversation,
                "assistant",
                "reply",
                clean,
                data=(
                    {"turn_id": turn_id, "index": index}
                    if turn_id is not None and index is not None
                    else None
                ),
            )
            self._write_records(current.root, current.state, conversation)
            return self._public_project(current)

    def send_message(
        self,
        project_id: str,
        text: str,
        *,
        timeout: float = 420.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        clean = _sanitize_secret_text(
            _safe_text(text, "message", limit=MAX_USER_MESSAGE_BYTES)
        )
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="message preparation"
        )
        if project.state["status"] in _TRANSIENT_STATES:
            raise ValidationError("project already has a running operation")
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            return self.preview_modification(
                project_id,
                clean,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        prior = project.conversation.get("proposal")
        prior_decisions = prior if isinstance(prior, dict) else {}
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            state = current.state
            conversation = current.conversation
            if state["revision"] != expected_revision:
                raise ValidationError("project changed while preparing the message")
            self._append_message(conversation, "user", "request", clean)
            state["status"] = "interpreting"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._event(state, current.root, "provider.started", "Interpreting request")
            self._write_records(current.root, state, conversation)
            expected_revision = state["revision"]

        run_dir = project.root / "provider-runs" / new_run_id()
        try:
            if self.provider is None:
                raise PCBDraftError(
                    "no model provider configured; run `pcbdraft connect` or /connect to add "
                    "a model service before sending a planning request"
                )
            value = self.provider.interpret(
                ProviderContext(
                    request=clean,
                    project_name=project.state["name"],
                    prior_decisions=prior_decisions,
                ),
                project_dir=project.root,
                run_dir=run_dir,
                timeout=timeout,
            )
            proposal, agent_request = self._prepare_proposal(
                project_id, project.state["created_at"], value, prior_decisions, clean
            )
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "provider_error",
                "provider.failed",
                str(exc),
            )
            raise

        compilation = None
        planning_error: str | None = None
        if (
            agent_request is not None
            and not proposal["clarifications"]
            and proposal["scope"]["decision"] == "attempted"
        ):
            planner = getattr(self.provider, "plan", None)
            try:
                if not callable(planner) or not getattr(
                    self.provider, "supports_planning", True
                ):
                    raise PCBDraftError(
                        "the selected provider can interpret requirements but cannot produce a circuit plan"
                    )
                symbol_context = planner_symbol_context(agent_request)
                plan = planner(
                    agent_request,
                    symbol_context=symbol_context,
                    project_dir=project.root,
                    run_dir=project.root / "provider-runs" / new_run_id(),
                    timeout=timeout,
                )
                compilation = compile_agent_plan(agent_request, plan)
                proposal = self._attach_plan(proposal, compilation)
            except PCBDraftError as exc:
                planning_error = _sanitize_secret_text(str(exc))[:2048]
                proposal["planning"] = {
                    "state": "unavailable",
                    "message": planning_error,
                }

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the provider was running")
            state = current.state
            conversation = current.conversation
            conversation["proposal"] = proposal
            conversation["decisions"] = proposal.get("decisions", {})
            if compilation is not None:
                atomic_write_json(
                    current.root / PENDING_REQUEST_NAME,
                    compilation.request.to_dict(),
                )
                atomic_write_json(
                    current.root / PENDING_PLAN_NAME, compilation.plan.to_dict()
                )
                atomic_write_json(
                    current.root / PENDING_DESIGN_NAME, compilation.design.to_dict()
                )
                atomic_write_json(
                    current.root / PENDING_PARTS_NAME, compilation.graph.to_dict()
                )
            pending = proposal["clarifications"]
            attemptable = proposal["scope"]["decision"] == "attempted"
            state["status"] = (
                "generation_unavailable"
                if not attemptable
                else "needs_clarification"
                if pending
                else "planning_required"
                if compilation is None
                else "awaiting_confirmation"
            )
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            assistant_text = self._proposal_message(proposal)
            self._append_message(
                conversation,
                "assistant",
                "proposal" if attemptable and compilation is not None else "planning",
                assistant_text,
                data={
                    "status": state["status"],
                    "clarification_count": len(pending),
                    "planning_error": planning_error,
                },
            )
            self._event(
                state,
                current.root,
                (
                    "plan.ready"
                    if compilation is not None
                    else "planning.required"
                    if attemptable
                    else "generation.unavailable"
                ),
                assistant_text,
                level="warning" if planning_error else "info",
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool = True,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="generation confirmation"
        )
        if project.state["status"] not in {
            "awaiting_confirmation",
            "generation_failed",
            "interrupted",
            "generated",
        }:
            raise ValidationError("project is not awaiting generation confirmation")
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            open_managed_project(project.design_root).assert_synchronized()
            preview = self.generate_project_previews(
                project_id,
                timeout=timeout,
                expected_revision=expected_revision,
            )
            if validate:
                return self.validate_project(
                    project_id,
                    timeout=timeout,
                    expected_revision=int(preview["state"]["revision"]),
                )
            return preview
        request = AgentDesignRequest.from_dict(
            load_json_limited(project.root / PENDING_REQUEST_NAME, APP_FILE_LIMIT)
        )
        plan = CircuitPlan.from_dict(
            load_json_limited(project.root / PENDING_PLAN_NAME, APP_FILE_LIMIT)
        )
        design = Design.from_dict(
            load_json_limited(project.root / PENDING_DESIGN_NAME, APP_FILE_LIMIT)
        )
        graph = PartGraph.load(project.root / PENDING_PARTS_NAME)
        if design.design_id != request.design_id or plan.design_id != request.design_id:
            raise ValidationError(
                "pending request, plan, and semantic design identities differ"
            )
        graph.assert_design(
            design,
            check_libraries=True,
            allow_provisional=design.metadata.get("assurance") == "provisional",
        )
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before confirmation")
            if current.design_root.exists() or current.design_root.is_symlink():
                raise ValidationError("confirmed project already has a design")
            state = current.state
            state["status"] = "generating"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._event(
                state,
                current.root,
                "generation.started",
                "Generating native KiCad project",
            )
            self._write_records(current.root, state, current.conversation)
            expected_revision = state["revision"]
        attempt_dir: Path | None = None
        attempt_record: dict[str, Any] | None = None
        try:
            attempt_id = new_run_id()
            attempt_dir = make_directory(
                make_directory(project.root / "attempts") / attempt_id
            )
            attempt_record = {
                "schema": ATTEMPT_SCHEMA,
                "version": ATTEMPT_VERSION,
                "id": attempt_id,
                "status": "running",
                "phase": "native_generation",
                "runtime": "agent_plan_v1",
                "assurance": "unknown",
                "started_at": utc_timestamp(),
                "completed_at": None,
                "part_ids": [],
                "requested_parts": list(request.requested_parts),
                "files": {
                    "request": "request.json",
                    "plan": "circuit-plan.json",
                    "semantic_ir": "design.pcbir.json",
                    "part_catalog": "parts.pcbdraft.json",
                    "retained_native": None,
                },
                "error": None,
            }
            atomic_write_json(attempt_dir / "request.json", request.to_dict())
            atomic_write_json(attempt_dir / "circuit-plan.json", plan.to_dict())
            atomic_write_json(attempt_dir / "design.pcbir.json", design.to_dict())
            atomic_write_json(attempt_dir / "parts.pcbdraft.json", graph.to_dict())
            atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            attempt_record["assurance"] = str(
                design.metadata.get("assurance", "provisional")
            )
            attempt_record["part_ids"] = sorted(
                {component.part_id for component in design.components}
            )
            atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            generated = materialize_managed_design(
                request,
                design,
                project.design_root,
                graph=graph,
                plan=plan,
                retain_failed_attempt=attempt_dir / "native",
            )
        except BaseException as exc:
            if attempt_dir is not None and attempt_record is not None:
                attempt_record["status"] = "failed"
                attempt_record["phase"] = "failed"
                attempt_record["completed_at"] = utc_timestamp()
                attempt_record["error"] = _sanitize_secret_text(str(exc))[:2048]
                if (attempt_dir / "native").is_dir():
                    attempt_record["files"]["retained_native"] = "native"
                atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            self._record_failure(
                project_id,
                expected_revision,
                "generation_failed",
                "generation.failed",
                str(exc),
            )
            raise
        if attempt_dir is not None and attempt_record is not None:
            attempt_record["status"] = "completed"
            attempt_record["phase"] = "completed"
            attempt_record["completed_at"] = utc_timestamp()
            atomic_write_json(attempt_dir / "attempt.json", attempt_record)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while generation was running")
            state = current.state
            conversation = current.conversation
            state["status"] = "generated"
            state["design_revision"] = 1
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._append_message(
                conversation,
                "assistant",
                "generation",
                "Generated a native KiCad schematic and routed PCB. Validation results, when run, are reported separately.",
                data={
                    "design_content_hash": generated.project.design.content_hash(),
                    "routing_state": generated.pcb.routing.state,
                    "unrouted": list(generated.pcb.routing.unrouted),
                },
            )
            self._event(
                state,
                current.root,
                "generation.complete",
                "Native KiCad schematic and routed PCB generated",
            )
            self._write_records(current.root, state, conversation)
            expected_revision = int(state["revision"])
        preview = self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )
        if validate:
            return self.validate_project(
                project_id,
                timeout=timeout,
                expected_revision=int(preview["state"]["revision"]),
            )
        return preview

    def prepare_agent_repair(
        self,
        project_id: str,
        feedback: dict[str, Any],
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Revise a plan from bounded tool evidence and stage it transactionally.

        A project without an authoritative design receives a replacement pending
        plan.  A generated project is never edited in place: its replacement is
        generated and validated under ``transactions/`` before it can be applied.
        """

        normalized = normalize_repair_feedback(feedback)
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="plan repair"
        )
        if project.state["status"] not in {
            "generation_failed",
            "generated",
            "validated",
            "validation_failed",
            "repair_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("project is not eligible for automatic plan repair")
        if project.state["active_transaction"] is not None:
            raise ValidationError("project already has a staged semantic change")
        request = AgentDesignRequest.from_dict(
            load_json_limited(project.root / PENDING_REQUEST_NAME, APP_FILE_LIMIT)
        )
        previous_plan = CircuitPlan.from_dict(
            load_json_limited(project.root / PENDING_PLAN_NAME, APP_FILE_LIMIT)
        )
        if request.design_id != previous_plan.design_id:
            raise ValidationError("pending repair request and plan identities differ")
        authoritative = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            authoritative = open_managed_project(project.design_root)
            authoritative.assert_synchronized()
            if authoritative.design.design_id != request.design_id:
                raise ValidationError(
                    "authoritative design identity differs from the pending repair plan"
                )
        prior_status = project.state["status"]
        prior_validation = project.state["last_validation"]
        prior_preview = project.state["last_preview"]
        prior_release = project.state["last_release"]
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before plan repair started")
            current.state["status"] = "repairing"
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "repair.started",
                f"Revising the circuit plan (attempt {normalized['attempt']})",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]

        reviser = getattr(self.provider, "revise_plan", None)
        try:
            if not callable(reviser) or not getattr(
                self.provider, "supports_planning", True
            ):
                raise PCBDraftError(
                    "the selected provider cannot revise a circuit plan from tool feedback"
                )
            revised_plan = reviser(
                request,
                previous_plan,
                normalized,
                symbol_context=planner_symbol_context(request),
                project_dir=project.root,
                run_dir=project.root / "provider-runs" / new_run_id(),
                timeout=timeout,
            )
            if revised_plan.canonical_bytes() == previous_plan.canonical_bytes():
                raise ValidationError(
                    "repair provider returned the unchanged circuit plan"
                )
            compilation = compile_agent_plan(request, revised_plan)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "repair_failed",
                "repair.failed",
                str(exc),
            )
            raise

        proposal = project.conversation.get("proposal")
        revised_proposal = (
            self._attach_plan(proposal, compilation)
            if isinstance(proposal, dict)
            else None
        )
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError(
                    "project changed while its plan was being revised"
                )
            atomic_write_json(
                current.root / PENDING_REQUEST_NAME, compilation.request.to_dict()
            )
            atomic_write_json(
                current.root / PENDING_PLAN_NAME, compilation.plan.to_dict()
            )
            atomic_write_json(
                current.root / PENDING_DESIGN_NAME, compilation.design.to_dict()
            )
            atomic_write_json(
                current.root / PENDING_PARTS_NAME, compilation.graph.to_dict()
            )
            if revised_proposal is not None:
                current.conversation["proposal"] = revised_proposal
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            if authoritative is None:
                current.state["status"] = "awaiting_confirmation"
                text = (
                    "A replacement circuit plan was compiled from retained tool "
                    "evidence and is ready for native KiCad generation."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_plan",
                    text,
                    data={"attempt": normalized["attempt"]},
                )
                self._event(current.state, current.root, "repair.plan_ready", text)
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]
        if authoritative is None:
            return self.open_project(project_id)

        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-agent-repair-transaction",
            "version": 1,
            "status": "preparing",
            "created_at": utc_timestamp(),
            "request": normalized["summary"],
            "feedback": normalized,
            "before_hash": authoritative.design.content_hash(),
            "after_hash": compilation.design.content_hash(),
            "prior_status": prior_status,
            "prior_validation": prior_validation,
            "prior_preview": prior_preview,
            "prior_release": prior_release,
            "validation": None,
            "result_status": None,
        }
        atomic_write_json(receipt_path, receipt)
        try:
            materialize_managed_design(
                compilation.request,
                compilation.design,
                staged,
                graph=compilation.graph,
                plan=compilation.plan,
                retain_failed_attempt=transaction / "failed-native",
            )
            candidate = open_managed_project(staged)
            candidate.assert_synchronized()
            validation_run = validate_managed_project(
                candidate,
                output=transaction / "validation",
                timeout=timeout,
            )
            validation_report = load_json_limited(
                validation_run.report_path, APP_FILE_LIMIT
            )
            levels = validation_report["levels"]
            candidate_feedback = validation_feedback_from_levels(
                levels, attempt=normalized["attempt"]
            )
            validation_summary = {
                "report": validation_run.report_path.relative_to(
                    transaction
                ).as_posix(),
                "report_sha256": validation_run.report_sha256,
                "candidate_ready": validation_run.candidate_ready,
                "production_evidence_complete": (
                    validation_run.production_evidence_complete
                ),
                "production_ready": validation_run.production_ready,
                "production_claimed": False,
                "assurance": str(
                    candidate.design.metadata.get("assurance", "provisional")
                ),
            }
            atomic_write_json(
                transaction / "semantic-diff.json",
                semantic_diff(authoritative.design, candidate.design),
            )
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            atomic_write_json(receipt_path, receipt)
            self._record_failure(
                project_id,
                expected_revision,
                "repair_failed",
                "repair.failed",
                str(exc),
            )
            raise

        receipt["validation"] = validation_summary
        if candidate_feedback is not None:
            receipt["status"] = "rejected"
            receipt["rejected_at"] = utc_timestamp()
            receipt["repair_feedback"] = candidate_feedback
            atomic_write_json(receipt_path, receipt)
            with ResourceLock(project.root, self.locks_root):
                current = self._open(project_id)
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while a repair candidate was validated"
                    )
                current.state["status"] = "repair_failed"
                current.state["revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                text = (
                    "The repair candidate retained deterministic L1-L3 failures; "
                    "the authoritative design was not changed."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_rejected",
                    text,
                    data={
                        "transaction_id": transaction_id,
                        "repair_feedback": candidate_feedback,
                    },
                )
                self._event(
                    current.state,
                    current.root,
                    "repair.candidate_failed",
                    text,
                    level="error",
                )
                self._write_records(current.root, current.state, current.conversation)
            return self.open_project(project_id)

        receipt["status"] = "ready"
        receipt["ready_at"] = utc_timestamp()
        receipt["result_status"] = (
            "validated" if validation_run.candidate_ready else "generated"
        )
        atomic_write_json(receipt_path, receipt)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError(
                    "project changed while a repair candidate was being staged"
                )
            current_managed = open_managed_project(current.design_root)
            current_managed.assert_synchronized()
            if current_managed.design.content_hash() != receipt["before_hash"]:
                raise ValidationError(
                    "authoritative design changed while a repair was staged"
                )
            current.state["active_transaction"] = transaction_id
            current.state["status"] = "change_ready"
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            text = (
                "A replacement design passed deterministic L1-L3 repair gates and "
                "is staged for atomic application."
            )
            self._append_message(
                current.conversation,
                "assistant",
                "repair_ready",
                text,
                data={"transaction_id": transaction_id},
            )
            self._event(current.state, current.root, "repair.ready", text)
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        if after < 0:
            raise ValidationError("event cursor must be non-negative")
        project = self._open(project_id)
        result: list[dict[str, Any]] = []
        for path in sorted((project.root / "events").glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                sequence = int(path.stem)
            except ValueError:
                continue
            if sequence > after:
                event = load_json_limited(path, 64 * 1024)
                if isinstance(event, dict):
                    result.append(event)
            if len(result) >= 500:
                break
        return result

    def _prepare_proposal(
        self,
        project_id: str,
        created_at: str,
        value: dict[str, Any],
        prior: dict[str, Any],
        request: str,
    ) -> tuple[dict[str, Any], AgentDesignRequest | None]:
        """Turn generic intent into a reviewable request, never a fixed board type."""

        merged = dict(value)
        layer_reply = bool(
            re.fullmatch(
                r"\s*(?:use\s+)?\d+\s*(?:[- ]?layers?|层)?\s*",
                request,
                re.IGNORECASE,
            )
        )
        prior_layers = prior.get("layers")
        if (
            merged.get("layers") is None
            and isinstance(prior_layers, int)
            and not isinstance(prior_layers, bool)
            and prior_layers >= 1
        ):
            merged["layers"] = prior_layers
        prior_board_value = prior.get("board")
        old_board: dict[str, Any] = (
            prior_board_value if isinstance(prior_board_value, dict) else {}
        )
        raw_board_value = merged.get("board")
        raw_board: dict[str, Any] = (
            raw_board_value if isinstance(raw_board_value, dict) else {}
        )
        merged["board"] = {
            key: raw_board.get(key)
            if raw_board.get(key) is not None
            else old_board.get(key)
            for key in ("width_mm", "height_mm")
        }
        if layer_reply:
            for key in ("requested_parts", "functions"):
                if not merged.get(key) and isinstance(prior.get(key), list):
                    merged[key] = list(prior[key])
            if isinstance(prior.get("request_summary"), str):
                merged["request_summary"] = prior["request_summary"]
        requested_parts = tuple(
            sorted(
                {
                    item
                    for item in merged.get("requested_parts", [])
                    if isinstance(item, str) and item.strip()
                },
                key=str.casefold,
            )
        )
        functions = tuple(
            sorted(
                {
                    item
                    for item in merged.get("functions", [])
                    if isinstance(item, str) and item.strip()
                }
            )
        )
        assumptions = [
            item
            for item in merged.get("assumptions", [])
            if isinstance(item, str) and item.strip()
        ]
        clarifications: list[dict[str, Any]] = []
        layers = merged.get("layers")
        if not isinstance(layers, int) or isinstance(layers, bool) or layers < 1:
            layers = _initial_stackup_layers(request)
            assumptions.append(
                "No usable planner stackup was returned; PCBDraft inferred an initial "
                f"{layers}-layer stackup from the stated design complexity."
            )
        merged["layers"] = layers
        width = merged["board"].get("width_mm")
        height = merged["board"].get("height_mm")
        if width is None or height is None:
            width, height = 80.0, 50.0
            assumptions.append(
                "Board envelope is assumed as 80 mm × 50 mm until changed in the reviewed plan."
            )
        merged["board"] = {"width_mm": float(width), "height_mm": float(height)}
        power_raw_value = merged.get("power")
        power_raw: dict[str, Any] = (
            power_raw_value if isinstance(power_raw_value, dict) else {}
        )
        nominal = power_raw.get("nominal_v")
        if (
            not isinstance(nominal, (int, float))
            or isinstance(nominal, bool)
            or nominal <= 0
        ):
            nominal = 3.3
            assumptions.append(
                "3.3 V logic supply is assumed until the reviewed plan specifies otherwise."
            )
        max_voltage = power_raw.get("max_voltage_v")
        if not isinstance(max_voltage, (int, float)) or isinstance(max_voltage, bool):
            max_voltage = nominal
        max_current = power_raw.get("max_current_a")
        if (
            not isinstance(max_current, (int, float))
            or isinstance(max_current, bool)
            or max_current <= 0
        ):
            max_current = 0.5
        max_power = power_raw.get("max_power_w")
        if (
            not isinstance(max_power, (int, float))
            or isinstance(max_power, bool)
            or max_power <= 0
        ):
            max_power = float(nominal) * float(max_current)
        power = {
            "nominal_v": float(nominal),
            "max_voltage_v": max(float(nominal), float(max_voltage)),
            "max_current_a": float(max_current),
            "max_power_w": float(max_power),
        }
        domains = {"simple_control"}
        words = " ".join((request, *requested_parts, *functions)).casefold()
        for token, domain in (
            ("i2c", "i2c"),
            ("i²c", "i2c"),
            ("spi", "spi"),
            ("uart", "uart"),
            ("串口", "uart"),
            ("usb", "usb2_basic"),
            ("基础usb", "usb2_basic"),
            ("buck", "simple_buck"),
            ("降压", "simple_buck"),
            ("ldo", "ldo"),
            ("稳压", "ldo"),
        ):
            if token in words:
                domains.add(domain)
        if any(
            token in words
            for token in (
                "sensor",
                "temperature",
                "humidity",
                "pressure",
                "传感器",
                "温度",
                "湿度",
                "压力",
            )
        ):
            domains.add("sensor")
        if any(
            token in words
            for token in (
                "mcu",
                "controller",
                "microcontroller",
                "embedded control",
                "单片机",
                "微控制器",
                "控制器",
                "控制板",
            )
        ):
            domains.add("low_voltage_mcu")
        for token, domain in (
            ("ddr", "ddr"),
            ("pcie", "pcie"),
            ("serdes", "serdes"),
            ("高速串行", "serdes"),
            ("rf", "rf"),
            ("antenna", "rf"),
            ("射频", "rf"),
            ("天线", "rf"),
            ("mains", "mains"),
            ("市电", "mains"),
            ("交流电", "mains"),
            ("high voltage", "high_voltage"),
            ("高压", "high_voltage"),
            ("high power", "high_power"),
            ("high-power", "high_power"),
            ("大功率", "high_power"),
            ("高功率", "high_power"),
            ("medical", "medical"),
            ("医疗", "medical"),
            ("aviation", "aviation"),
            ("航空", "aviation"),
            ("safety-critical", "safety_critical"),
            ("安全关键", "safety_critical"),
        ):
            if token in words:
                domains.add(domain)
        scope = Scope.from_dict(
            {
                "domains": sorted(domains),
                "max_voltage_v": power["max_voltage_v"],
                "max_current_a": power["max_current_a"],
                "max_power_w": power["max_power_w"],
                "layers": layers,
                "intended_use": "User-requested PCB design; no domain validation is implied.",
                "risk_class": "unspecified",
            }
        )
        scope_decision = evaluate_scope(scope)
        if not scope_decision.accepted:
            return (
                {
                    **merged,
                    "requested_parts": list(requested_parts),
                    "functions": list(functions),
                    "assurance": "provisional",
                    "scope": {
                        "decision": "generation_unavailable",
                        "errors": list(scope_decision.reasons),
                        "warnings": list(scope_decision.warnings),
                    },
                    "clarifications": [],
                    "planning": {"state": "not_started", "message": None},
                    "brief": None,
                    "decisions": {},
                },
                None,
            )
        board = BoardSpec.from_dict(
            {
                "width_mm": float(width),
                "height_mm": float(height),
                "layers": layers,
                "thickness_mm": 1.6,
                "edge_clearance_mm": 0.5,
                "min_track_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "finish": "enig",
            }
        )
        design_id = (
            f"{_slug(merged.get('design_name', 'board'))[:40]}-{project_id[-8:]}"
        )
        approved_request = AgentDesignRequest.from_dict(
            {
                "schema": "pcbdraft-agent-design-request",
                "version": 1,
                "design_id": design_id,
                "name": str(merged.get("design_name") or "PCBDraft board"),
                "revision": "A",
                "request_summary": str(merged.get("request_summary") or request),
                "scope": scope.to_dict(),
                "board": board.to_dict(),
                "assumptions": sorted(set(assumptions)),
                "requested_parts": list(requested_parts),
                "functions": list(functions),
                "power": power,
                "source": {
                    "locator": f"application/projects/{project_id}/conversation.json",
                    "date": created_at[:10],
                },
            }
        )
        proposal: dict[str, Any] = {
            **merged,
            "requested_parts": list(requested_parts),
            "functions": list(functions),
            "assumptions": list(approved_request.assumptions),
            "power": power,
            "assurance": "provisional",
            "scope": {
                "decision": "attempted",
                "warnings": list(scope_decision.warnings),
            },
            "clarifications": clarifications,
            "planning": {"state": "pending", "message": None},
            "brief": None,
            "decisions": {
                "runtime": "agent_plan_v1",
                "assurance": "provisional",
                "design_id": approved_request.design_id,
                "design_name": approved_request.name,
                "layers": approved_request.board.layers,
                "board": approved_request.board.to_dict(),
                "requested_parts": list(approved_request.requested_parts),
                "risk_class": approved_request.scope.risk_class,
            },
        }
        return proposal, None if clarifications else approved_request

    @staticmethod
    def _attach_plan(proposal: dict[str, Any], compilation: Any) -> dict[str, Any]:
        """Attach only reviewable plan/IR facts; native generation stays confirmed."""

        result = dict(proposal)
        design = compilation.design
        graph = compilation.graph
        counts: dict[tuple[str, str], int] = {}
        references: dict[tuple[str, str], list[str]] = {}
        for component in design.components:
            key = (component.part_id, component.value)
            counts[key] = counts.get(key, 0) + 1
            references.setdefault(key, []).append(component.reference)
        result["planning"] = {"state": "ready", "message": None}
        result["brief"] = {
            "purpose": compilation.request.request_summary,
            "architecture": [
                {"id": block.id, "kind": block.kind, "name": block.name}
                for block in design.blocks
            ],
            "assumptions": list(compilation.request.assumptions),
            "power": compilation.request.power,
            "interfaces": [],
            "board": compilation.request.board.to_dict(),
            "identity": {
                "requested_parts": list(compilation.request.requested_parts),
                "planned_symbols": [
                    {
                        "reference": component.reference,
                        "symbol": graph.get(component.part_id).symbol,
                        "part_id": component.part_id,
                    }
                    for component in design.components
                ],
                "preserved": True,
            },
            "bom": [
                {
                    "part_id": key[0],
                    "value": key[1],
                    "quantity": counts[key],
                    "references": sorted(references[key]),
                    "symbol": graph.get(key[0]).symbol,
                    "trust": graph.get(key[0]).trust,
                }
                for key in sorted(counts)
            ],
            "net_count": len(design.nets),
            "constraints": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "severity": item.severity,
                    "rationale": item.rationale,
                }
                for item in design.constraints
            ],
            "plan_review": compilation.review.to_dict(),
            "semantic_content_hash": design.content_hash(),
            "confirmation_required": True,
        }
        return result

    @staticmethod
    def _proposal_message(proposal: dict[str, Any]) -> str:
        decision = proposal["scope"]["decision"]
        if decision != "attempted":
            return "This request cannot reach the current KiCad backend: " + "; ".join(
                proposal["scope"].get("errors", [])
            )
        if proposal["clarifications"]:
            return proposal["clarifications"][0]["question"]
        planning = proposal.get("planning", {})
        if planning.get("state") != "ready":
            return (
                "Requirements were retained without substituting parts, but a circuit "
                "planning provider is needed before a reviewable topology can be generated: "
                + str(planning.get("message") or "planning is pending")
            )
        review = proposal.get("brief", {}).get("plan_review", {})
        summary = review.get("summary", {}) if isinstance(review, dict) else {}
        attention = summary.get("attention_required", 0)
        if isinstance(attention, int) and attention > 0:
            return (
                "The circuit plan and stock KiCad parts are ready for review. "
                f"{attention} deterministic preflight finding(s) need engineering attention; "
                "generation remains available according to the active client policy."
            )
        return (
            "The circuit plan, stock KiCad parts, and assumptions are ready. "
            "The active client policy controls whether generation continues automatically "
            "or waits for review."
        )

    def _record_failure(
        self,
        project_id: str,
        expected_revision: int,
        status: str,
        event_kind: str,
        message: str,
    ) -> None:
        project = self._open(project_id)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                return
            state = current.state
            conversation = current.conversation
            state["status"] = status
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            public = _sanitize_secret_text(message)[:2048]
            self._append_message(conversation, "assistant", "failure", public)
            self._event(state, current.root, event_kind, public, level="error")
            self._write_records(current.root, state, conversation)

    def _recover_interrupted_projects(self) -> None:
        for candidate in self.projects_root.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                project = self._open_path(candidate)
            except PCBDraftError:
                continue
            if project.state["status"] not in _TRANSIENT_STATES:
                continue
            try:
                with ResourceLock(candidate, self.locks_root, timeout=0):
                    current = self._open_path(candidate)
                    if current.state["status"] in _TRANSIENT_STATES:
                        self._interrupt_running_attempts(candidate)
                        recovered_status = "interrupted"
                        if (
                            current.design_root.is_dir()
                            and not current.design_root.is_symlink()
                        ):
                            try:
                                open_managed_project(
                                    current.design_root
                                ).assert_synchronized()
                                recovered_status = "generated"
                            except PCBDraftError:
                                recovered_status = "interrupted"
                        current.state["status"] = recovered_status
                        current.state["revision"] += 1
                        current.state["updated_at"] = utc_timestamp()
                        self._event(
                            current.state,
                            candidate,
                            "operation.interrupted",
                            (
                                "Recovered an atomically published managed project; "
                                "validation may be retried."
                                if recovered_status == "generated"
                                else "Previous operation was interrupted and may be retried."
                            ),
                            level="warning",
                        )
                        self._write_records(
                            candidate, current.state, current.conversation
                        )
            except PCBDraftError:
                continue

    def _project_path(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise ValidationError("application project id is invalid")
        path = self.projects_root / project_id
        if path.is_symlink():
            raise ValidationError("application project path is unsafe")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(
                f"application project does not exist: {project_id}"
            ) from exc
        if resolved.parent != self.projects_root or not resolved.is_dir():
            raise ValidationError("application project path escapes the workspace")
        return resolved

    def _open(self, project_id: str) -> ApplicationProject:
        return self._open_path(self._project_path(project_id))

    def _open_path(self, root: Path) -> ApplicationProject:
        state = load_json_limited(root / "project.json", APP_FILE_LIMIT)
        conversation = load_json_limited(root / "conversation.json", APP_FILE_LIMIT)
        self._validate_state(state, expected_id=root.name)
        self._validate_conversation(conversation)
        return ApplicationProject(root=root, state=state, conversation=conversation)

    @staticmethod
    def _validate_state(value: Any, *, expected_id: str) -> None:
        if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
            raise ValidationError("application project record is malformed")
        if (
            value["schema"] != APP_PROJECT_SCHEMA
            or value["version"] != APP_PROJECT_VERSION
        ):
            raise ValidationError("unsupported application project schema/version")
        if value["id"] != expected_id or not _PROJECT_ID.fullmatch(value["id"]):
            raise ValidationError("application project identity is malformed")
        for field in ("name", "created_at", "updated_at", "status", "provider"):
            if not isinstance(value[field], str) or not value[field]:
                raise ValidationError(
                    f"application project field is malformed: {field}"
                )
        for field in ("revision", "design_revision", "event_sequence"):
            if (
                isinstance(value[field], bool)
                or not isinstance(value[field], int)
                or value[field] < 0
            ):
                raise ValidationError(
                    f"application project counter is malformed: {field}"
                )

    @staticmethod
    def _validate_conversation(value: Any) -> None:
        if not isinstance(value, dict) or set(value) != _CONVERSATION_FIELDS:
            raise ValidationError("conversation record is malformed")
        if (
            value["schema"] != CONVERSATION_SCHEMA
            or value["version"] != CONVERSATION_VERSION
        ):
            raise ValidationError("unsupported conversation record schema/version")
        if (
            not isinstance(value["messages"], list)
            or len(value["messages"]) > MAX_MESSAGES
        ):
            raise ValidationError("conversation message history is malformed")
        if not isinstance(value["decisions"], dict):
            raise ValidationError("conversation decisions are malformed")

    @staticmethod
    def _append_message(
        conversation: dict[str, Any],
        role: str,
        kind: str,
        text: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        if len(conversation["messages"]) >= MAX_MESSAGES:
            raise ValidationError("conversation reached its 2000 message limit")
        conversation["messages"].append(
            {
                "id": secrets.token_hex(8),
                "role": role,
                "kind": kind,
                "text": _sanitize_secret_text(text),
                "created_at": utc_timestamp(),
                "data": data or {},
            }
        )

    @staticmethod
    def _event(
        state: dict[str, Any],
        root: Path,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        state["event_sequence"] += 1
        sequence = state["event_sequence"]
        atomic_write_json(
            root / "events" / f"{sequence:08d}.json",
            {
                "schema": "pcbdraft-structured-event",
                "version": 1,
                "sequence": sequence,
                "kind": kind,
                "level": level,
                "message": _sanitize_secret_text(message)[:2048],
                "created_at": utc_timestamp(),
            },
        )

    @staticmethod
    def _write_records(
        root: Path, state: dict[str, Any], conversation: dict[str, Any]
    ) -> None:
        atomic_write_json(root / "conversation.json", conversation)
        atomic_write_json(root / "project.json", state)

    @staticmethod
    def _summary(project: ApplicationProject) -> dict[str, Any]:
        return {
            "id": project.state["id"],
            "name": project.state["name"],
            "status": project.state["status"],
            "updated_at": project.state["updated_at"],
            "design_revision": project.state["design_revision"],
            "provider": project.state["provider"],
        }

    @staticmethod
    def _interrupt_running_attempts(root: Path) -> None:
        attempts = root / "attempts"
        if attempts.is_symlink() or not attempts.is_dir():
            return
        for candidate in attempts.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            record_path = candidate / "attempt.json"
            try:
                record = load_json_limited(record_path, APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if (
                not ApplicationService._valid_attempt_record(
                    record, expected_id=candidate.name
                )
                or record.get("status") != "running"
            ):
                continue
            record["status"] = "interrupted"
            record["phase"] = "interrupted"
            record["completed_at"] = utc_timestamp()
            record["error"] = "Generation process stopped before completion."
            atomic_write_json(record_path, record)

    @staticmethod
    def _attempt_records(project: ApplicationProject) -> list[dict[str, Any]]:
        attempts = project.root / "attempts"
        if attempts.is_symlink() or not attempts.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for candidate in sorted(attempts.iterdir(), reverse=True):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                record = load_json_limited(candidate / "attempt.json", APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if not ApplicationService._valid_attempt_record(
                record, expected_id=candidate.name
            ):
                continue
            public = dict(record)
            public["root"] = str(candidate)
            result.append(public)
            if len(result) >= 50:
                break
        return result

    @staticmethod
    def _valid_attempt_record(value: Any, *, expected_id: str) -> bool:
        if not isinstance(value, dict) or set(value) != _ATTEMPT_FIELDS:
            return False
        files = value.get("files")
        string_lists = (value.get("part_ids"), value.get("requested_parts"))
        return bool(
            value.get("schema") == ATTEMPT_SCHEMA
            and value.get("version") == ATTEMPT_VERSION
            and value.get("id") == expected_id
            and value.get("status") in {"running", "completed", "failed", "interrupted"}
            and isinstance(value.get("phase"), str)
            and isinstance(value.get("runtime"), str)
            and value.get("assurance") in {"unknown", "provisional"}
            and isinstance(value.get("started_at"), str)
            and (
                value.get("completed_at") is None
                or isinstance(value.get("completed_at"), str)
            )
            and all(
                isinstance(items, list)
                and len(items) <= 2_000
                and all(isinstance(item, str) for item in items)
                for items in string_lists
            )
            and isinstance(files, dict)
            and set(files)
            == {"request", "plan", "semantic_ir", "part_catalog", "retained_native"}
            and all(item is None or isinstance(item, str) for item in files.values())
            and (value.get("error") is None or isinstance(value.get("error"), str))
        )

    def _public_project(self, project: ApplicationProject) -> dict[str, Any]:
        design: dict[str, Any] | None = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            managed = open_managed_project(project.design_root)
            design = {
                "root": str(managed.root),
                "design_id": managed.design.design_id,
                "name": managed.design.name,
                "content_hash": managed.design.content_hash(),
                "drift": list(managed.drift()),
                "files": {
                    key: str(managed.root / relative)
                    for key, relative in managed.manifest["files"].items()
                },
            }
        active_change: dict[str, Any] | None = None
        transaction_id = project.state.get("active_transaction")
        if isinstance(transaction_id, str) and re.fullmatch(
            r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", transaction_id
        ):
            transaction = project.root / "transactions" / transaction_id
            receipt = load_json_limited(transaction / "receipt.json", APP_FILE_LIMIT)
            diff = load_json_limited(transaction / "semantic-diff.json", APP_FILE_LIMIT)
            active_change = {
                "transaction_id": transaction_id,
                "request": receipt.get("request"),
                "status": receipt.get("status"),
                "diff": diff,
                "validation": _public_readiness_record(receipt.get("validation")),
            }
        public_state = dict(project.state)
        public_state["last_validation"] = _public_readiness_record(
            project.state["last_validation"]
        )
        public_state["last_release"] = _public_readiness_record(
            project.state["last_release"]
        )
        return {
            "schema": "pcbdraft-application-view",
            "version": 1,
            "project": self._summary(project),
            "state": public_state,
            "conversation": project.conversation,
            "design": design,
            "artifacts": {
                "previews": project.state["last_preview"],
                "validation": public_state["last_validation"],
                "release": public_state["last_release"],
            },
            "attempts": self._attempt_records(project),
            "active_change": active_change,
            "events": self.events(
                project.state["id"], after=max(0, project.state["event_sequence"] - 50)
            ),
        }

    def validate_project(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Run the real layered runtime validation and attach its evidence."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="validation"
        )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "released",
            "interrupted",
            "release_failed",
        }:
            raise ValidationError("project must be generated before validation")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        run_id = new_run_id()
        output = project.root / "validation" / run_id
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before validation")
            state = current.state
            state["status"] = "validating"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._event(
                state, current.root, "validation.started", "Running configured checks"
            )
            self._write_records(current.root, state, current.conversation)
            expected_revision = state["revision"]
        try:
            result = validate_managed_project(managed, output=output, timeout=timeout)
            report = load_json_limited(result.report_path, APP_FILE_LIMIT)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "validation_failed",
                "validation.failed",
                str(exc),
            )
            raise
        relative_report = result.report_path.relative_to(project.root).as_posix()
        summary = {
            "run_id": run_id,
            "report": relative_report,
            "report_sha256": result.report_sha256,
            "candidate_ready": result.candidate_ready,
            "production_evidence_complete": result.production_evidence_complete,
            "production_ready": result.production_ready,
            "production_claimed": False,
            "assurance": str(managed.design.metadata.get("assurance", "verified")),
            "levels": report["levels"],
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while validation was running")
            state = current.state
            conversation = current.conversation
            state["last_validation"] = summary
            provisional = summary["assurance"] == "provisional"
            state["status"] = (
                "validated"
                if result.candidate_ready
                else "generated"
                if provisional
                else "validation_failed"
            )
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            text = (
                "The configured KiCad and PCBDraft checks passed. This does not "
                "establish electrical, regulatory, or manufacturing fitness."
                if result.candidate_ready
                else (
                    "KiCad and PCBDraft checks completed and the generated files were retained. Review the reported findings; no electrical, regulatory, or manufacturing validation is implied."
                    if provisional
                    else "Checks found issues; the generated files and results were retained for review."
                )
            )
            self._append_message(
                conversation,
                "assistant",
                "validation",
                text,
                data={
                    "candidate_ready": result.candidate_ready,
                    "production_evidence_complete": (
                        result.production_evidence_complete
                    ),
                    "production_ready": result.production_ready,
                },
            )
            self._event(
                state,
                current.root,
                "validation.complete",
                text,
                level="info" if result.candidate_ready or provisional else "error",
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def generate_project_previews(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Generate browser-safe links to real KiCad exports and a 3D render."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="preview generation"
        )
        if not project.design_root.is_dir():
            raise ValidationError("project must be generated before preview export")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        output = project.root / "previews" / new_run_id()
        bundle = generate_previews(managed, output, timeout=timeout)
        preview = {
            "root": bundle.root.relative_to(project.root).as_posix(),
            "receipt": bundle.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": bundle.design_content_hash,
            "files": {
                key: path.relative_to(project.root).as_posix()
                for key, path in bundle.files.items()
            },
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while previews were generated")
            if (
                open_managed_project(current.design_root).design.content_hash()
                != bundle.design_content_hash
            ):
                raise ValidationError("design changed while previews were generated")
            current.state["last_preview"] = preview
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "preview.complete",
                "Schematic, PCB, PDF, and 3D render previews generated",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def preview_modification(
        self,
        project_id: str,
        request: str,
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Turn a follow-up message into a validated, staged replacement design.

        The planning provider receives the retained semantic plan plus a bounded
        user revision request.  It never edits native KiCad files: the replacement
        is generated and checked inside a transaction before the runtime policy or
        user can atomically apply it.
        """

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="revision staging"
        )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        if managed.design.metadata.get("generator") != "agent_plan_v1":
            raise ValidationError(
                "this project was not generated from a retained agent circuit plan; use the semantic patch workflow"
            )
        if project.state["active_transaction"] is not None:
            raise ValidationError(
                "review, apply, or discard the staged PCB change before requesting another revision"
            )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "repair_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("the current project state cannot accept a revision")
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before the revision was staged")
            self._append_message(current.conversation, "user", "revision", request)
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "repair.requested",
                "Preparing a transactional PCB revision from the follow-up request",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = int(current.state["revision"])
        return self.prepare_agent_repair(
            project_id,
            user_revision_feedback(request),
            timeout=timeout,
            expected_revision=expected_revision,
        )

    def apply_modification(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically publish the currently staged, confirmed semantic change."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="candidate application"
        )
        transaction_id = project.state["active_transaction"]
        if project.state["status"] != "change_ready" or not isinstance(
            transaction_id, str
        ):
            raise ValidationError(
                "project has no semantic change awaiting confirmation"
            )
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        if not isinstance(receipt, dict) or receipt.get("status") != "ready":
            raise ValidationError("semantic change receipt is not ready")
        staged = transaction / "staged"
        before = transaction / "before"
        current_managed = open_managed_project(project.design_root)
        staged_managed = open_managed_project(staged)
        if current_managed.design.content_hash() != receipt["before_hash"]:
            raise ValidationError("authoritative design changed after semantic preview")
        if staged_managed.design.content_hash() != receipt["after_hash"]:
            raise ValidationError(
                "staged design no longer matches the semantic receipt"
            )
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before candidate application")
            if current.state["active_transaction"] != transaction_id:
                raise ValidationError("active semantic transaction changed")
            state = current.state
            conversation = current.conversation
            moved_before = False
            try:
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
            except BaseException:
                if (
                    moved_before
                    and before.exists()
                    and not current.design_root.exists()
                ):
                    os.replace(before, current.design_root)
                raise
            receipt["status"] = "applied"
            receipt["applied_at"] = utc_timestamp()
            atomic_write_json(receipt_path, receipt)
            state["status"] = receipt.get("result_status", "validated")
            state["active_transaction"] = None
            state["last_transaction"] = transaction_id
            state["last_validation"] = {
                "run_id": f"transaction:{transaction_id}",
                "report": (
                    Path("transactions")
                    / transaction_id
                    / receipt["validation"]["report"]
                ).as_posix(),
                **{
                    key: receipt["validation"][key]
                    for key in (
                        "report_sha256",
                        "candidate_ready",
                        "production_evidence_complete",
                        "production_ready",
                        "production_claimed",
                    )
                },
                "assurance": receipt["validation"].get("assurance", "provisional"),
                "levels": load_json_limited(
                    transaction / receipt["validation"]["report"], APP_FILE_LIMIT
                )["levels"],
            }
            state["last_release"] = None
            state["last_preview"] = None
            state["design_revision"] += 1
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            text = (
                "Applied the staged replacement atomically; undo remains available."
                if receipt.get("schema") == "pcbdraft-agent-repair-transaction"
                else "Applied the confirmed semantic change atomically; undo remains available."
            )
            self._append_message(
                conversation,
                "assistant",
                "change_applied",
                text,
                data={"transaction_id": transaction_id},
            )
            self._event(state, current.root, "change.applied", text)
            self._write_records(current.root, state, conversation)
            expected_revision = int(state["revision"])
        return self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )

    def discard_modification(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="candidate discard"
        )
        transaction_id = project.state["active_transaction"]
        if project.state["status"] != "change_ready" or not isinstance(
            transaction_id, str
        ):
            raise ValidationError(
                "project has no semantic change awaiting confirmation"
            )
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before candidate discard")
            if current.state["active_transaction"] != transaction_id:
                raise ValidationError("active semantic transaction changed")
            receipt["status"] = "discarded"
            receipt["discarded_at"] = utc_timestamp()
            atomic_write_json(receipt_path, receipt)
            current.state["active_transaction"] = None
            current.state["status"] = receipt.get("prior_status", "generated")
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "change.discarded",
                "Staged semantic change discarded; authoritative design was untouched.",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def undo_last_modification(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="last-change undo"
        )
        transaction_id = project.state["last_transaction"]
        if not isinstance(transaction_id, str):
            raise ValidationError("project has no applied semantic change to undo")
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        if receipt.get("status") != "applied":
            raise ValidationError("last semantic transaction is not undoable")
        managed = open_managed_project(project.design_root)
        if managed.design.content_hash() != receipt["after_hash"]:
            raise ValidationError(
                "authoritative design changed after the last transaction"
            )
        before = transaction / "before"
        after = transaction / "after"
        open_managed_project(before).assert_synchronized()
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before last-change undo")
            if current.state["last_transaction"] != transaction_id:
                raise ValidationError("last semantic transaction changed")
            moved_after = False
            try:
                os.replace(current.design_root, after)
                moved_after = True
                os.replace(before, current.design_root)
            except BaseException:
                if moved_after and after.exists() and not current.design_root.exists():
                    os.replace(after, current.design_root)
                raise
            receipt["status"] = "undone"
            receipt["undone_at"] = utc_timestamp()
            atomic_write_json(receipt_path, receipt)
            current.state["status"] = receipt.get("prior_status", "generated")
            current.state["last_transaction"] = None
            current.state["last_validation"] = receipt.get("prior_validation")
            current.state["last_preview"] = receipt.get("prior_preview")
            current.state["last_release"] = receipt.get("prior_release")
            current.state["design_revision"] += 1
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            text = "Undo restored the exact previous authoritative managed project."
            self._append_message(
                current.conversation,
                "assistant",
                "change_undone",
                text,
                data={"transaction_id": transaction_id},
            )
            self._event(current.state, current.root, "change.undone", text)
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def build_release(
        self,
        project_id: str,
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="release build"
        )
        validation = project.state["last_validation"]
        if (
            project.state["status"]
            not in {"validated", "released", "release_failed", "interrupted"}
            or not isinstance(validation, dict)
            or not validation.get("candidate_ready")
        ):
            raise ValidationError(
                "release requires a passing engineering-candidate validation"
            )
        release_id = new_run_id()
        output = project.root / "releases" / release_id
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before release")
            current.state["status"] = "releasing"
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                "release.started",
                "Building manufacturing-candidate bundle",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]
        try:
            release = build_manufacturing_release(
                project.design_root, output, timeout=timeout
            )
            verified = verify_manufacturing_release(release.root)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "release_failed",
                "release.failed",
                str(exc),
            )
            raise
        release_summary = {
            "id": release_id,
            "root": str(release.root),
            "manifest": str(release.manifest_path),
            "manifest_sha256": release.manifest_sha256,
            "archive": str(release.archive_path),
            "archive_sha256": release.archive_sha256,
            "candidate_ready": release.candidate_ready,
            "production_evidence_complete": release.production_evidence_complete,
            "production_ready": release.production_ready,
            "production_claimed": False,
            "offline_verification": verified.to_dict(),
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while release was running")
            current.state["status"] = "released"
            current.state["last_release"] = release_summary
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            text = (
                "Manufacturing-candidate bundle was built and verified offline; it is "
                "not a production or physical sign-off claim."
            )
            self._append_message(
                current.conversation,
                "assistant",
                "release",
                text,
                data={"release_id": release_id},
            )
            self._event(current.state, current.root, "release.complete", text)
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def verify_release(self, project_id: str) -> dict[str, Any]:
        project = self._open(project_id)
        release = project.state["last_release"]
        if not isinstance(release, dict) or not isinstance(release.get("id"), str):
            raise ValidationError("project has no manufacturing-candidate release")
        root = project.root / "releases" / release["id"]
        result = verify_manufacturing_release(root)
        return result.to_dict()
