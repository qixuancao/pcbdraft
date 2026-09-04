"""Register the flat, PCBDraft-only PCB toolbox into vendored Hermes."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pcbdraft.agent.permissions import (
    PCBToolGateway,
    PermissionBroker,
    PermissionMode,
    ToolPermissionError,
)
from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    EvidenceStage,
    PCBToolExecutor,
    PCBToolRegistry,
    ToolCall,
    ToolResult,
    ToolSpec,
    call_from_view,
)
from pcbdraft.core.errors import PCBDraftError

__all__ = (
    "get_current_project_id",
    "get_service",
    "get_session_project_id",
    "model_tool_projection",
    "refresh_service_provider",
    "register_all_pcb_tools",
    "reset_session_project_context",
    "set_current_project_id",
)

_PCB_TOOLSET = "pcbdraft"

#: Timeout shared by the concrete Hermes-facing flat PCB handlers.
DEFAULT_PCB_TOOL_TIMEOUT = 600.0

#: Explicit inspect responses are bounded independently of normal receipts.
_EXPLICIT_INSPECTION_BYTES = 16 * 1024
_EXPLICIT_INSPECTION_ITEMS = 24

_GLOBAL_LIBRARY_TOOLS = frozenset(
    {
        "search_symbols",
        "describe_symbol",
        "search_footprints",
        "describe_footprint",
    }
)

_service_cache: Any = None
_permission_mode: PermissionMode = "workspace"


@dataclass(frozen=True)
class _SessionProjectBinding:
    epoch: int
    project_id: str


@dataclass(frozen=True)
class _SessionStageBinding:
    epoch: int
    project_id: str
    live_revision: int
    design_revision: int
    evidence_source: str
    stage: EvidenceStage


class _ProjectContextStore:
    """Bind model sessions to trusted human project selections."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._epoch = 0
        self._trusted_project_id: str | None = None
        self._sessions: dict[str, _SessionProjectBinding] = {}
        self._stages: dict[str, _SessionStageBinding] = {}
        self._binding_details_sent: set[str] = set()

    def trusted_project_id(self) -> str | None:
        with self._lock:
            return self._trusted_project_id

    def select_trusted(self, project_id: str | None) -> None:
        with self._lock:
            self._epoch += 1
            self._trusted_project_id = project_id
            self._sessions.clear()
            self._stages.clear()
            self._binding_details_sent.clear()

    def bound_project(self, session_id: str) -> str | None:
        if not session_id:
            raise PCBDraftError("PCB project access requires a trusted Hermes session")
        with self._lock:
            binding = self._sessions.get(session_id)
            if binding is not None and binding.epoch == self._epoch:
                return binding.project_id
            if self._trusted_project_id is None:
                return None
            self._sessions[session_id] = _SessionProjectBinding(
                self._epoch, self._trusted_project_id
            )
            self._stages.pop(session_id, None)
            self._binding_details_sent.discard(session_id)
            return self._trusted_project_id

    def session_project(self, session_id: str) -> str | None:
        """Return an existing session binding without implicitly creating one."""

        if not session_id:
            return None
        with self._lock:
            binding = self._sessions.get(session_id)
            if binding is None or binding.epoch != self._epoch:
                return None
            return binding.project_id

    def create_and_bind(
        self,
        session_id: str,
        create: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Serialize model creation against trusted human selection changes."""

        if not session_id:
            raise PCBDraftError(
                "PCB project creation requires a trusted Hermes session"
            )
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None and current.epoch == self._epoch:
                raise PCBDraftError(
                    "this Hermes session is already bound to a PCB project"
                )
            if self._trusted_project_id is not None:
                raise PCBDraftError(
                    "this Hermes session already has a user-selected PCB project"
                )
            view = create()
            project = view.get("project")
            project_id = project.get("id") if isinstance(project, Mapping) else None
            if not isinstance(project_id, str) or not project_id:
                raise PCBDraftError("PCB project creation returned no project identity")
            self._epoch += 1
            self._trusted_project_id = project_id
            self._sessions.clear()
            self._stages.clear()
            self._binding_details_sent.clear()
            self._sessions[session_id] = _SessionProjectBinding(self._epoch, project_id)
            return view

    def claim_binding_details(self, session_id: str, project_id: str) -> bool:
        """Return true exactly once for one current session/project binding."""

        with self._lock:
            binding = self._sessions.get(session_id)
            if (
                binding is None
                or binding.epoch != self._epoch
                or binding.project_id != project_id
                or session_id in self._binding_details_sent
            ):
                return False
            self._binding_details_sent.add(session_id)
            return True

    def cached_stage(
        self,
        session_id: str,
        project_id: str,
        live_revision: int,
        design_revision: int,
        evidence_source: str | None = None,
    ) -> EvidenceStage | None:
        with self._lock:
            cached = self._stages.get(session_id)
            if (
                cached is not None
                and cached.epoch == self._epoch
                and cached.project_id == project_id
                and cached.live_revision == live_revision
                and cached.design_revision == design_revision
                and (
                    evidence_source is None or cached.evidence_source == evidence_source
                )
            ):
                return cached.stage
            self._stages.pop(session_id, None)
            return None

    def retain_stage(
        self,
        session_id: str,
        project_id: str,
        live_revision: int,
        design_revision: int,
        evidence_source: str,
        stage: EvidenceStage,
    ) -> None:
        with self._lock:
            binding = self._sessions.get(session_id)
            if (
                binding is None
                or binding.epoch != self._epoch
                or binding.project_id != project_id
            ):
                return
            self._stages[session_id] = _SessionStageBinding(
                self._epoch,
                project_id,
                live_revision,
                design_revision,
                evidence_source,
                stage,
            )

    def discard_stage(self, session_id: str) -> None:
        with self._lock:
            self._stages.pop(session_id, None)

    def reset_session(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)
            self._stages.pop(session_id, None)
            self._binding_details_sent.discard(session_id)


_project_context = _ProjectContextStore()


def _service() -> Any:
    """Return one authoritative ApplicationService for this process."""

    global _service_cache
    if _service_cache is None:
        from pcbdraft.services.application import ApplicationService

        _service_cache = ApplicationService()
    return _service_cache


def _set_service(service: Any) -> None:
    """Allow tests and the launcher to pin an isolated service workspace."""

    global _service_cache
    _service_cache = service


def refresh_service_provider() -> None:
    """Refresh a cached service after the persistent model authority changes."""

    if _service_cache is None:
        return
    from pcbdraft.model.providers import resolve_provider

    _service_cache.provider = resolve_provider("auto")


def get_service() -> Any:
    """Return the authoritative ApplicationService for this process."""

    return _service()


def get_current_project_id() -> str | None:
    """Return the current trusted human PCB project selection, if any."""

    return _project_context.trusted_project_id()


def get_session_project_id(session_id: str) -> str | None:
    """Return the project already used by one live Hermes session, if any."""

    return _project_context.session_project(session_id)


def set_current_project_id(value: str | None) -> None:
    """Set or clear the process-scoped current PCB project id.

    This is a convenience cursor for the interactive surface only; durable
    state always lives in the project records under the repository.
    """

    _project_context.select_trusted(value)


def reset_session_project_context(session_id: str) -> None:
    """Forget one ended Hermes session without changing human selection."""

    _project_context.reset_session(session_id)


@dataclass(frozen=True)
class ModelToolProjection:
    """One revision-bound model schema projection from the full registry."""

    stage: EvidenceStage | None
    project_id: str | None
    live_revision: int | None
    design_revision: int | None
    specs: tuple[ToolSpec, ...]


_EVIDENCE_STAGE_VALUES = frozenset(
    {
        "not_started",
        "requirements_frozen",
        "schematic_semantic",
        "native_schematic_confirmed",
        "footprint_net_sync",
        "placement",
        "routing",
        "native_connectivity_confirmed",
        "erc_drc",
        "release_gate",
    }
)


def _revision_pair(view: Mapping[str, Any]) -> tuple[int, int] | None:
    state = view.get("state")
    project = view.get("project")
    if not isinstance(state, Mapping) or not isinstance(project, Mapping):
        return None
    live_revision = state.get("revision")
    design_revision = state.get("design_revision", project.get("design_revision"))
    if (
        isinstance(live_revision, bool)
        or not isinstance(live_revision, int)
        or live_revision < 0
        or isinstance(design_revision, bool)
        or not isinstance(design_revision, int)
        or design_revision < 0
    ):
        return None
    return live_revision, design_revision


def _evidence_stage(
    session_id: str,
    project_id: str,
    view: Mapping[str, Any],
    *,
    refresh_evidence: bool = False,
) -> EvidenceStage | None:
    revisions = _revision_pair(view)
    if revisions is None:
        return None
    live_revision, design_revision = revisions
    if not refresh_evidence:
        cached = _project_context.cached_stage(
            session_id, project_id, live_revision, design_revision
        )
        if cached is not None:
            return cached
    inspector = getattr(_service(), "inspect_engineering_stage", None)
    if not callable(inspector):
        _project_context.discard_stage(session_id)
        return None
    projected = inspector(project_id)
    if not isinstance(projected, Mapping):
        _project_context.discard_stage(session_id)
        return None
    stage = projected.get("stage")
    evidence_source = projected.get("evidence_source")
    if (
        projected.get("project_id") != project_id
        or projected.get("live_revision") != live_revision
        or projected.get("design_revision") != design_revision
        or not isinstance(stage, str)
        or stage not in _EVIDENCE_STAGE_VALUES
        or not isinstance(evidence_source, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}", evidence_source) is None
    ):
        _project_context.discard_stage(session_id)
        return None
    cached = _project_context.cached_stage(
        session_id,
        project_id,
        live_revision,
        design_revision,
        evidence_source,
    )
    if cached == stage:
        return cached
    typed_stage = stage  # narrowed by the fixed evidence-stage membership above
    _project_context.retain_stage(
        session_id,
        project_id,
        live_revision,
        design_revision,
        evidence_source,
        typed_stage,  # type: ignore[arg-type]
    )
    return typed_stage  # type: ignore[return-value]


def model_tool_projection(session_id: str) -> ModelToolProjection:
    """Return the current evidence-derived provider schema subset.

    A stale or unavailable stage deliberately falls back to the registry's
    bounded corrective subset.  The caller cannot supply or override a stage.
    """

    project_id = _project_context.bound_project(session_id)
    if project_id is None:
        return ModelToolProjection(
            None,
            None,
            None,
            None,
            DEFAULT_PCB_TOOL_REGISTRY.projected_specs(None, project_bound=False),
        )
    try:
        view = _service().open_project(project_id)
        revisions = _revision_pair(view)
        stage = _evidence_stage(
            session_id,
            project_id,
            view,
            refresh_evidence=True,
        )
    except PCBDraftError:
        revisions = None
        stage = None
    return ModelToolProjection(
        stage,
        project_id,
        revisions[0] if revisions is not None else None,
        revisions[1] if revisions is not None else None,
        DEFAULT_PCB_TOOL_REGISTRY.projected_specs(stage, project_bound=True),
    )


def _bounded_explicit(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "<detail depth limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 2_048 else value[:2_048] + "...[truncated]"
    if isinstance(value, Mapping):
        entries = list(value.items())
        result = {
            str(key): _bounded_explicit(item, depth=depth + 1)
            for key, item in entries[:_EXPLICIT_INSPECTION_ITEMS]
        }
        if len(entries) > _EXPLICIT_INSPECTION_ITEMS:
            result["detail_truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        items = [
            _bounded_explicit(item, depth=depth + 1)
            for item in value[:_EXPLICIT_INSPECTION_ITEMS]
        ]
        if len(value) > _EXPLICIT_INSPECTION_ITEMS:
            items.append("<additional items omitted>")
        return items
    return str(value)[:2_048]


def _explicit_result(value: Mapping[str, Any]) -> dict[str, Any]:
    bounded = _bounded_explicit(value)
    if not isinstance(bounded, dict):
        return {"detail": bounded}
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _EXPLICIT_INSPECTION_BYTES:
        return bounded
    return {
        "detail_truncated": True,
        "available_keys": sorted(str(key) for key in value)[
            :_EXPLICIT_INSPECTION_ITEMS
        ],
        "detail_bytes": len(encoded.encode("utf-8")),
    }


def _compact_progress(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    summary: dict[str, Any] = {
        key: value.get(key)
        for key in ("classification", "decisive_metric")
        if value.get(key) is not None
    }
    metrics = value.get("metrics")
    if isinstance(metrics, list):
        changes = {
            str(item.get("name")): item.get("delta")
            for item in metrics
            if isinstance(item, Mapping)
            and isinstance(item.get("name"), str)
            and item.get("delta") not in {None, 0}
        }
        if changes:
            summary["changes"] = changes
    return summary or None


def _compact_convergence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = {
        key: value.get(key)
        for key in ("allowed", "action", "reason")
        if value.get(key) is not None
    }
    return result or None


def _compact_diagnostics(value: Any) -> dict[str, Any] | None:
    """Keep actionable check diagnostics in model receipts without unbounding them."""

    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    counts = value.get("counts")
    if isinstance(counts, Mapping):
        compact_counts = {
            key: counts.get(key)
            for key in ("error", "warning", "total")
            if isinstance(counts.get(key), int)
            and not isinstance(counts.get(key), bool)
        }
        if compact_counts:
            result["counts"] = compact_counts
    for key in (
        "violation_count_seen",
        "remaining_violation_count",
    ):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            result[key] = item
    for key in ("violations_truncated", "details_truncated"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    violations = value.get("violations")
    if isinstance(violations, list):
        result["violations"] = [
            dict(item) for item in violations[:20] if isinstance(item, Mapping)
        ]
    for key in ("full_details_report", "raw_report"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = item[:512]
    return result or None


def _compact_error_text(value: object) -> str:
    """Keep an already-sanitized expected failure inside a normal receipt."""

    text = str(value)
    return text if len(text) <= 512 else text[:512] + "...[truncated]"


def _model_summary(
    spec: ToolSpec,
    view: Mapping[str, Any],
    *,
    session_id: str,
    include_binding: bool,
) -> dict[str, Any]:
    """Project a full application view into one compact model receipt."""

    project = view.get("project")
    project = project if isinstance(project, Mapping) else {}
    state = view.get("state")
    state = state if isinstance(state, Mapping) else {}
    project_id = project.get("id")
    stage = (
        _evidence_stage(session_id, project_id, view)
        if isinstance(project_id, str) and project_id
        else None
    )
    revisions = _revision_pair(view)
    result: dict[str, Any] = {
        "tool": spec.external_name,
        "success": True,
        "ok": True,
        "project_id": project_id,
        "stage": stage or "unknown",
        "revision": revisions[0] if revisions is not None else state.get("revision"),
        "design_revision": revisions[1] if revisions is not None else None,
    }
    tool_result = view.get("tool_result")
    if isinstance(tool_result, Mapping):
        operation = tool_result.get("operation")
        result["operation"] = operation if isinstance(operation, str) else spec.name
        transaction_id = tool_result.get("transaction_id")
        if isinstance(transaction_id, str) and transaction_id:
            result["artifact_id"] = f"transaction:{transaction_id}"
        if spec.effect == "read":
            result["result"] = _explicit_result(tool_result)
        else:
            intended = tool_result.get("changed")
            if isinstance(intended, Mapping):
                result["intended_delta"] = dict(intended)
            native: dict[str, Any] = {}
            if isinstance(tool_result.get("consistency_passed"), bool):
                native["consistency_passed"] = tool_result["consistency_passed"]
            postconditions = tool_result.get("postconditions")
            if isinstance(postconditions, list):
                failed = [
                    item.get("name")
                    for item in postconditions
                    if isinstance(item, Mapping) and item.get("passed") is False
                ]
                native["failed_postconditions"] = failed[:8]
            if native:
                result["native_delta"] = native
            progress = _compact_progress(tool_result.get("progress_delta"))
            if progress is not None:
                result["progress_delta"] = progress
            convergence = _compact_convergence(tool_result.get("convergence"))
            if convergence is not None:
                result["convergence"] = convergence
            diagnostics = _compact_diagnostics(tool_result.get("diagnostics"))
            if diagnostics is not None:
                result["diagnostics"] = diagnostics
            for key in ("state", "outcome", "production_ready"):
                if tool_result.get(key) is not None:
                    result[key] = tool_result[key]
    if include_binding:
        binding: dict[str, Any] = {
            "project_name": project.get("name"),
            "status": project.get("status"),
        }
        design = view.get("design")
        if isinstance(design, Mapping):
            binding["design_root"] = design.get("root")
            binding["files"] = design.get("files")
        result["binding"] = binding
    return result


def _execute_tool(
    spec: ToolSpec, arguments: dict[str, Any], *, session_id: str
) -> dict[str, Any]:
    """Execute one concrete PCB tool through permissions and the executor."""

    service = _service()
    registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY
    arguments = registry.normalize_arguments(spec.name, arguments)
    executor = PCBToolExecutor(service, registry=registry)
    permissions = PermissionBroker(_permission_mode)

    if spec.name in _GLOBAL_LIBRARY_TOOLS:
        return {
            "tool": spec.external_name,
            "success": True,
            "project_id": None,
            "result": service.inspect_installed_library(spec.name, arguments),
        }
    if spec.name == "create_project":
        call = ToolCall(
            name=spec.name,
            project_id="repository",
            source="model",
            arguments=arguments,
            baseline_revision=0,
        )
        verdict = permissions.decide(call, spec)
        if verdict.action != "allow":
            raise ToolPermissionError(verdict.reason)
        view = _project_context.create_and_bind(
            session_id,
            lambda: service.create_empty_project(arguments["name"]),
        )
        project = view.get("project")
        project_id = project.get("id") if isinstance(project, Mapping) else None
        include_binding = bool(
            isinstance(project_id, str)
            and _project_context.claim_binding_details(session_id, project_id)
        )
        return _model_summary(
            spec,
            view,
            session_id=session_id,
            include_binding=include_binding,
        )

    current_project_id = _project_context.bound_project(session_id)
    if not current_project_id:
        raise PCBDraftError(
            f"{spec.external_name} requires a current project; create one or select one with a trusted user command first"
        )
    call = call_from_view(
        spec.name,
        current_project_id,
        source="model",
        arguments=arguments,
        view=service.open_project(current_project_id),
    )
    gateway = PCBToolGateway(executor, permissions)
    result: ToolResult = gateway.execute(call, timeout=DEFAULT_PCB_TOOL_TIMEOUT)
    include_binding = _project_context.claim_binding_details(
        session_id, current_project_id
    )
    return _model_summary(
        spec,
        result.view,
        session_id=session_id,
        include_binding=include_binding,
    )


def _handler(spec: ToolSpec) -> Callable[[dict[str, Any]], str]:
    def handle(args: dict[str, Any], **kwargs: Any) -> str:
        session_id = str(kwargs.get("session_id") or "")
        try:
            summary = _execute_tool(spec, dict(args or {}), session_id=session_id)
            return json.dumps(summary, ensure_ascii=False)
        except PCBDraftError as exc:
            transaction_id = getattr(exc, "transaction_id", None)
            has_transaction_artifact = (
                isinstance(transaction_id, str)
                and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", transaction_id)
                is not None
            )
            payload: dict[str, Any] = {
                "tool": spec.external_name,
                "success": False,
                "ok": False,
                "error_code": getattr(exc, "error_code", "tool_error"),
                "error": (
                    "PCB transaction failed; inspect artifact_id for bounded details."
                    if has_transaction_artifact
                    else _compact_error_text(exc)
                ),
            }
            if has_transaction_artifact:
                payload["artifact_id"] = f"transaction:{transaction_id}"
            if spec.name not in _GLOBAL_LIBRARY_TOOLS:
                try:
                    projection = model_tool_projection(session_id)
                    payload["project_id"] = projection.project_id
                    payload["stage"] = projection.stage or "unknown"
                    payload["revision"] = projection.live_revision
                    payload["design_revision"] = projection.design_revision
                except PCBDraftError:
                    pass
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - defensive Hermes boundary
            del exc
            return json.dumps(
                {
                    "tool": spec.external_name,
                    "success": False,
                    "ok": False,
                    "error_code": "internal_tool_failure",
                    "error": "internal PCB tool failure",
                },
                ensure_ascii=False,
            )

    return handle


def register_all_pcb_tools(*, permission_mode: PermissionMode = "workspace") -> None:
    """Register only concrete flat tools under the PCBDraft toolset."""

    from tools.registry import registry as hermes_registry

    global _permission_mode
    _permission_mode = permission_mode

    for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
        hermes_registry.register(
            name=spec.external_name,
            toolset=_PCB_TOOLSET,
            schema={
                "name": spec.external_name,
                "description": (spec.protocol_description),
                "parameters": spec.input_schema,
            },
            handler=_handler(spec),
            description=spec.protocol_description,
            emoji="🔌",
            max_result_size_chars=64 * 1024,
        )
