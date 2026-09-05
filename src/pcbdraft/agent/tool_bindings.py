"""Register the flat, PCBDraft-only PCB toolbox into native PCBDraft."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import threading
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

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
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import load_json_limited

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

#: Timeout shared by the concrete model-facing flat PCB handlers.
DEFAULT_PCB_TOOL_TIMEOUT = 600.0

#: Explicit inspect responses are bounded independently of normal receipts.
_EXPLICIT_INSPECTION_BYTES = 16 * 1024
_EXPLICIT_INSPECTION_ITEMS = 24

# Generated board renders are 1200x800 PNGs in the current KiCad pipeline.
# Keep a much larger but still provider-safe ceiling so an unexpectedly huge
# artifact fails before base64 expansion can enter live conversation history.
_MODEL_BOARD_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_MODEL_BOARD_REGION_MAX_BYTES = 4 * 1024 * 1024
_MODEL_BOARD_REGION_MAX_PIXELS = 4_000_000
_PREVIEW_RECEIPT_MAX_BYTES = 128 * 1024
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_CONTENT_HASH = re.compile(r"[0-9a-f]{64}")

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
            raise PCBDraftError(
                "PCB project access requires a trusted PCBDraft session"
            )
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
                "PCB project creation requires a trusted PCBDraft session"
            )
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None and current.epoch == self._epoch:
                raise PCBDraftError(
                    "this PCBDraft session is already bound to a PCB project"
                )
            if self._trusted_project_id is not None:
                raise PCBDraftError(
                    "this PCBDraft session already has a user-selected PCB project"
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


@dataclass(frozen=True)
class ToolSession:
    service: Any
    projects: _ProjectContextStore
    permissions: PermissionBroker
    execute: Callable[[ToolCall], ToolResult] | None = None
    owns_terminal_receipt: bool = True


_tool_session: ContextVar[ToolSession | None] = ContextVar(
    "pcbdraft_tool_session", default=None
)


@contextmanager
def tool_session(
    service: Any,
    project_id: str,
    *,
    permissions: PermissionBroker,
    execute: Callable[[ToolCall], ToolResult] | None = None,
    owns_terminal_receipt: bool = True,
) -> Iterator[None]:
    """Bind one conversation's authority without changing another session."""
    projects = _ProjectContextStore()
    projects.select_trusted(project_id)
    token = _tool_session.set(
        ToolSession(service, projects, permissions, execute, owns_terminal_receipt)
    )
    try:
        yield
    finally:
        _tool_session.reset(token)


def _context_store() -> _ProjectContextStore:
    scope = _tool_session.get()
    return scope.projects if scope is not None else _project_context


def owns_terminal_receipt() -> bool:
    scope = _tool_session.get()
    return scope is None or scope.owns_terminal_receipt


def _service(*, recover_interrupted: bool = True) -> Any:
    """Return one authoritative ApplicationService for this process."""

    scope = _tool_session.get()
    if scope is not None:
        return scope.service
    global _service_cache
    if _service_cache is None:
        from pcbdraft.services.application import ApplicationService

        _service_cache = ApplicationService(recover_interrupted=recover_interrupted)
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


def get_service(*, recover_interrupted: bool = True) -> Any:
    """Return the authoritative ApplicationService for this process."""

    return _service(recover_interrupted=recover_interrupted)


def get_current_project_id() -> str | None:
    """Return the current trusted human PCB project selection, if any."""

    return _context_store().trusted_project_id()


def get_session_project_id(session_id: str) -> str | None:
    """Return the project already used by one live PCBDraft session, if any."""

    return _context_store().session_project(session_id)


def set_current_project_id(value: str | None) -> None:
    """Set or clear the process-scoped current PCB project id.

    This is a convenience cursor for the interactive surface only; durable
    state always lives in the project records under the repository.
    """

    _context_store().select_trusted(value)


def reset_session_project_context(session_id: str) -> None:
    """Forget one ended PCBDraft session without changing human selection."""

    _context_store().reset_session(session_id)


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
        cached = _context_store().cached_stage(
            session_id, project_id, live_revision, design_revision
        )
        if cached is not None:
            return cached
    inspector = getattr(_service(), "inspect_engineering_stage", None)
    if not callable(inspector):
        _context_store().discard_stage(session_id)
        return None
    projected = inspector(project_id)
    if not isinstance(projected, Mapping):
        _context_store().discard_stage(session_id)
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
        _context_store().discard_stage(session_id)
        return None
    cached = _context_store().cached_stage(
        session_id,
        project_id,
        live_revision,
        design_revision,
        evidence_source,
    )
    if cached == stage:
        return cached
    typed_stage = stage  # narrowed by the fixed evidence-stage membership above
    _context_store().retain_stage(
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

    project_id = _context_store().bound_project(session_id)
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
    if spec.name == "render_board":
        return _board_render_feedback(_service(), view, result)
    if spec.name == "observe_board_region":
        return _board_region_feedback(_service(), view, result)
    return result


def _board_render_feedback(
    service: Any,
    view: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one receipt-verified, revision-bound board PNG to the tool result."""

    project = view.get("project")
    state = view.get("state")
    design = view.get("design")
    tool_result = view.get("tool_result")
    if not all(
        isinstance(value, Mapping) for value in (project, state, design, tool_result)
    ):
        raise ValidationError("PCB board render result is incomplete")
    project_id = project.get("id")
    revision = state.get("revision")
    design_revision = state.get("design_revision", project.get("design_revision"))
    content_hash = design.get("content_hash")
    source_revision = tool_result.get("source_revision")
    source_design_revision = tool_result.get("source_design_revision")
    result_revision = tool_result.get("revision")
    run_id = tool_result.get("run_id")
    if (
        not isinstance(project_id, str)
        or not project_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or isinstance(design_revision, bool)
        or not isinstance(design_revision, int)
        or design_revision < 0
        or not isinstance(content_hash, str)
        or _CONTENT_HASH.fullmatch(content_hash) is None
        or isinstance(source_revision, bool)
        or not isinstance(source_revision, int)
        or source_revision < 0
        or isinstance(source_design_revision, bool)
        or not isinstance(source_design_revision, int)
        or source_design_revision < 0
        or isinstance(result_revision, bool)
        or not isinstance(result_revision, int)
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
    ):
        raise ValidationError("PCB board render revision binding is malformed")
    if (
        tool_result.get("render") != "render_board"
        or tool_result.get("design_content_hash") != content_hash
        or source_design_revision != design_revision
        or result_revision != revision
        or source_revision + 1 != revision
    ):
        raise ValidationError(
            "PCB board render is stale for the current project revision"
        )

    # Re-open through the authoritative service after rendering.  Another
    # tool may have advanced the project between handler return and image
    # materialization; in that case the old pixels must not be sent as current.
    latest = service.open_project(project_id)
    latest_project = latest.get("project") if isinstance(latest, Mapping) else None
    latest_state = latest.get("state") if isinstance(latest, Mapping) else None
    latest_design = latest.get("design") if isinstance(latest, Mapping) else None
    if (
        not isinstance(latest_project, Mapping)
        or latest_project.get("id") != project_id
        or not isinstance(latest_state, Mapping)
        or latest_state.get("revision") != revision
        or latest_state.get("design_revision", latest_project.get("design_revision"))
        != design_revision
        or not isinstance(latest_design, Mapping)
        or latest_design.get("content_hash") != content_hash
    ):
        raise ValidationError("PCB board render became stale before visual feedback")

    expected_root = f"previews/{run_id}"
    expected_receipt = f"{expected_root}/receipt.json"
    expected_image = f"{expected_root}/board-top.png"
    files = tool_result.get("files")
    if (
        tool_result.get("root") != expected_root
        or tool_result.get("receipt") != expected_receipt
        or not isinstance(files, Mapping)
        or files.get("board_render") != expected_image
    ):
        raise ValidationError("PCB visual artifact does not match its preview bundle")

    project_root = Path(service.project_root(project_id))
    try:
        if project_root.is_symlink() or not project_root.is_dir():
            raise ValidationError("PCB project root is unsafe")
        project_root = project_root.resolve(strict=True)
        preview_candidate = project_root / "previews" / run_id
        preview_info = preview_candidate.lstat()
        if preview_candidate.is_symlink() or not stat.S_ISDIR(preview_info.st_mode):
            raise ValidationError("PCB preview bundle path is unsafe")
        preview_root = preview_candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("PCB preview bundle is unavailable") from exc
    if preview_root.parent != project_root / "previews" or not preview_root.is_dir():
        raise ValidationError("PCB preview bundle path is unsafe")

    receipt_path = preview_root / "receipt.json"
    image_path = preview_root / "board-top.png"
    receipt = _load_preview_receipt(receipt_path)
    inventory = receipt.get("files")
    image_inventory = (
        inventory.get("board_render") if isinstance(inventory, Mapping) else None
    )
    if (
        receipt.get("schema") != "pcbdraft-preview-bundle"
        or receipt.get("version") != 1
        or receipt.get("renders") != ["render_board"]
        or receipt.get("design_content_hash") != content_hash
        or not isinstance(image_inventory, Mapping)
        or image_inventory.get("path") != "board-top.png"
    ):
        raise ValidationError("PCB preview receipt is invalid or stale")

    image_bytes = _read_preview_png(image_path)
    expected_size = image_inventory.get("bytes")
    expected_hash = image_inventory.get("sha256")
    actual_hash = hashlib.sha256(image_bytes).hexdigest()
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size != len(image_bytes)
        or not isinstance(expected_hash, str)
        or _CONTENT_HASH.fullmatch(expected_hash) is None
        or expected_hash != actual_hash
    ):
        raise ValidationError("PCB board PNG does not match its preview receipt")
    width, height = _validate_png(image_bytes)

    model_summary = dict(summary)
    model_summary.pop("binding", None)
    model_summary.update(
        {
            "source_revision": source_revision,
            "design_content_hash": content_hash,
            "image_sha256": actual_hash,
            "image_bytes": len(image_bytes),
            "image_width": width,
            "image_height": height,
            "image_scope": "live_tool_result_only",
            "rerender_after_resume": True,
        }
    )
    summary_text = json.dumps(
        model_summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": summary_text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "text_summary": summary_text,
        "meta": {
            "project_id": project_id,
            "revision": revision,
            "design_revision": design_revision,
            "design_content_hash": content_hash,
            "image_sha256": actual_hash,
        },
    }


def _board_region_feedback(
    service: Any,
    view: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Crop exact pixels from the current receipt-verified board render."""

    state = view.get("state")
    tool_result = view.get("tool_result")
    if not isinstance(state, Mapping) or not isinstance(tool_result, Mapping):
        raise ValidationError("PCB board region observation result is incomplete")
    if tool_result.get("operation") != "observe_board_region":
        raise ValidationError("PCB board region observation result is malformed")
    last_preview = state.get("last_preview")
    if not isinstance(last_preview, Mapping):
        raise ValidationError(
            "PCB board region observation requires a current pcb_render_board result; "
            "call pcb_render_board first"
        )
    if last_preview.get("render") != "render_board":
        raise ValidationError(
            "current preview is not a board render; call pcb_render_board first"
        )
    revision = state.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValidationError("PCB board region revision binding is malformed")

    # Reuse the whole-board verifier rather than introducing another artifact
    # path. last_preview is application-owned; callers can name only its hash
    # and a pixel rectangle, never a filesystem location.
    render_view = dict(view)
    render_view["tool_result"] = {**last_preview, "revision": revision}
    try:
        verified = _board_render_feedback(
            service,
            render_view,
            {"tool": "pcb_render_board", "project_id": summary.get("project_id")},
        )
    except ValidationError as exc:
        raise ValidationError(
            f"current board render is unavailable or stale; call pcb_render_board again ({exc})"
        ) from exc

    source_text = verified["content"][0]["text"]
    source_summary = json.loads(source_text)
    source_url = verified["content"][1]["image_url"]["url"]
    try:
        source_bytes = base64.b64decode(source_url.partition(",")[2], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError("verified PCB board PNG could not be decoded") from exc

    requested_hash = tool_result.get("source_image_sha256")
    source_hash = source_summary.get("image_sha256")
    if requested_hash != source_hash:
        raise ValidationError(
            "source image hash does not match the current board render; use "
            "image_sha256 from the latest pcb_render_board result"
        )

    x_px = tool_result.get("x_px")
    y_px = tool_result.get("y_px")
    width_px = tool_result.get("width_px")
    height_px = tool_result.get("height_px")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (x_px, y_px, width_px, height_px)
    ):
        raise ValidationError("PCB board region coordinates are malformed")
    x_px = int(x_px)
    y_px = int(y_px)
    width_px = int(width_px)
    height_px = int(height_px)
    source_width = source_summary.get("image_width")
    source_height = source_summary.get("image_height")
    if not isinstance(source_width, int) or not isinstance(source_height, int):
        raise ValidationError("verified PCB board dimensions are malformed")
    right_px = x_px + width_px
    bottom_px = y_px + height_px
    if (
        x_px < 0
        or y_px < 0
        or width_px <= 0
        or height_px <= 0
        or right_px > source_width
        or bottom_px > source_height
    ):
        raise ValidationError(
            "PCB board region is outside source image bounds "
            f"{source_width}x{source_height}"
        )
    if width_px * height_px > _MODEL_BOARD_REGION_MAX_PIXELS:
        raise ValidationError(
            "PCB board region exceeds the 4000000-pixel observation budget"
        )

    try:
        with Image.open(io.BytesIO(source_bytes)) as source_image:
            source_image.load()
            region_image = source_image.crop((x_px, y_px, right_px, bottom_px))
            output = io.BytesIO()
            region_image.save(output, format="PNG")
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValidationError("PCB board region could not be cropped") from exc
    image_bytes = output.getvalue()
    if not image_bytes or len(image_bytes) > _MODEL_BOARD_REGION_MAX_BYTES:
        raise ValidationError(
            "PCB board region exceeds the 4194304-byte PNG observation budget"
        )
    image_hash = hashlib.sha256(image_bytes).hexdigest()

    model_summary = dict(summary)
    model_summary.pop("binding", None)
    model_summary.pop("result", None)
    model_summary.update(
        {
            "source_revision": source_summary.get("source_revision"),
            "source_render_run_id": last_preview.get("run_id"),
            "source_image_kind": "current-pcb-render-board-png",
            "source_preview_receipt": last_preview.get("receipt"),
            "design_content_hash": source_summary.get("design_content_hash"),
            "source_image_sha256": source_hash,
            "source_image_bytes": source_summary.get("image_bytes"),
            "source_image_width": source_width,
            "source_image_height": source_height,
            "crop_box_px": {
                "left": x_px,
                "top": y_px,
                "right": right_px,
                "bottom": bottom_px,
            },
            "coordinate_system": {
                "units": "source-image-pixels",
                "origin": "top-left",
                "x_axis": "right",
                "y_axis": "down",
                "bounds": "right-bottom-exclusive",
            },
            "pixel_to_board_mm_calibrated": False,
            "adds_detail": False,
            "resampling": "none",
            "image_sha256": image_hash,
            "image_bytes": len(image_bytes),
            "image_width": width_px,
            "image_height": height_px,
            "region_limits": {
                "max_pixels": _MODEL_BOARD_REGION_MAX_PIXELS,
                "max_png_bytes": _MODEL_BOARD_REGION_MAX_BYTES,
            },
            "image_scope": "live_tool_result_only",
            "rerender_after_resume": True,
        }
    )
    summary_text = json.dumps(
        model_summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": summary_text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "text_summary": summary_text,
        "meta": {
            "project_id": model_summary.get("project_id"),
            "revision": model_summary.get("revision"),
            "design_revision": model_summary.get("design_revision"),
            "design_content_hash": model_summary.get("design_content_hash"),
            "source_image_sha256": source_hash,
            "image_sha256": image_hash,
            "crop_box_px": model_summary["crop_box_px"],
        },
    }


def _load_preview_receipt(path: Path) -> Mapping[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError("PCB preview receipt is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValidationError("PCB preview receipt path is unsafe")
    value = load_json_limited(path, _PREVIEW_RECEIPT_MAX_BYTES)
    if not isinstance(value, Mapping):
        raise ValidationError("PCB preview receipt is malformed")
    return value


def _read_preview_png(path: Path) -> bytes:
    """Read exactly one fixed bundle member without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationError("PCB board PNG is missing or unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size <= 0
            or info.st_size > _MODEL_BOARD_IMAGE_MAX_BYTES
        ):
            raise ValidationError("PCB board PNG size or file type is unsafe")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        image_bytes = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(image_bytes) != info.st_size:
        raise ValidationError("PCB board PNG changed while it was read")
    return image_bytes


def _validate_png(data: bytes) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise ValidationError("PCB board render is not a valid PNG")
                width, height = image.size
                image.verify()
    except ValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ValidationError("PCB board render is not a valid PNG") from exc
    if width <= 0 or height <= 0:
        raise ValidationError("PCB board render is not a valid PNG")
    return width, height


def _execute_tool(
    spec: ToolSpec, arguments: dict[str, Any], *, session_id: str
) -> dict[str, Any]:
    """Execute one concrete PCB tool through permissions and the executor."""

    service = _service()
    registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY
    arguments = registry.normalize_arguments(spec.name, arguments)
    executor = PCBToolExecutor(service, registry=registry)
    scope = _tool_session.get()
    permissions = scope.permissions if scope else PermissionBroker(_permission_mode)

    if scope is not None and scope.execute is not None:
        project_id = scope.projects.bound_project(session_id)
        if project_id is None or spec.name == "create_project":
            raise ToolPermissionError(
                "this conversation is bound to its selected project"
            )
        call = call_from_view(
            spec.name,
            project_id,
            source="model",
            arguments=arguments,
            view=service.open_project(project_id),
        )
        scoped_result = scope.execute(call)
        return _model_summary(
            spec,
            scoped_result.view,
            session_id=session_id,
            include_binding=scope.projects.claim_binding_details(
                session_id, project_id
            ),
        )

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
        view = _context_store().create_and_bind(
            session_id,
            lambda: service.create_empty_project(arguments["name"]),
        )
        project = view.get("project")
        project_id = project.get("id") if isinstance(project, Mapping) else None
        include_binding = bool(
            isinstance(project_id, str)
            and _context_store().claim_binding_details(session_id, project_id)
        )
        return _model_summary(
            spec,
            view,
            session_id=session_id,
            include_binding=include_binding,
        )

    current_project_id = _context_store().bound_project(session_id)
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
    include_binding = _context_store().claim_binding_details(
        session_id, current_project_id
    )
    return _model_summary(
        spec,
        result.view,
        session_id=session_id,
        include_binding=include_binding,
    )


def _handler(spec: ToolSpec) -> Callable[[dict[str, Any]], Any]:
    def handle(args: dict[str, Any], **kwargs: Any) -> Any:
        session_id = str(kwargs.get("session_id") or "")
        try:
            summary = _execute_tool(spec, dict(args or {}), session_id=session_id)
            if summary.get("_multimodal") is True:
                return summary
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
        except Exception as exc:  # noqa: BLE001 - defensive PCBDraft boundary
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

    from pcbdraft.tools.registry import registry

    global _permission_mode
    if _tool_session.get() is None:
        _permission_mode = permission_mode

    for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
        registry.register(
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
