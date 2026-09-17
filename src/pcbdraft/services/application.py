"""Authoritative project and conversation service shared by terminal and web UIs."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pcbdraft.agent.compiler import compile_agent_plan, planner_symbol_context
from pcbdraft.agent.plan import AgentDesignRequest, CircuitPlan
from pcbdraft.agent.repair import (
    normalize_repair_feedback,
    user_revision_feedback,
    validation_feedback_from_levels,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, make_directory
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.repository import (
    ProjectRepository,
    configure_repository,
    current_repository,
    explicit_repository,
)
from pcbdraft.core.runs import new_run_id, utc_timestamp
from pcbdraft.domain.component_qualification import qualify_components
from pcbdraft.domain.ir import BoardSpec, Design, Scope, canonical_json_bytes
from pcbdraft.domain.operations import (
    ConnectGroupEntry,
    PlaceGroupEntry,
    apply_change_set,
    parse_connect_group,
    parse_place_group,
    semantic_diff,
)
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.scope import evaluate_scope
from pcbdraft.kicad.consistency import (
    NativeBoardProjection,
    NativeConsistencyReport,
    NativeSchematicProjection,
    compare_native_consistency,
    compare_native_operation_delta,
    inspect_native_consistency,
)
from pcbdraft.kicad.pcb import inspect_native_board
from pcbdraft.kicad.previews import generate_preview, generate_previews
from pcbdraft.kicad.schematic import inspect_native_schematic
from pcbdraft.kicad.sync import apply_kicad_import, preview_kicad_import
from pcbdraft.model.providers import (
    MAX_USER_MESSAGE_BYTES,
    IntentProvider,
    ProviderContext,
    resolve_provider,
)
from pcbdraft.services import application_agent_repair as _application_agent_repair
from pcbdraft.services import application_native_outputs as _application_native_outputs
from pcbdraft.services import application_pcb_operations as _application_pcb_operations
from pcbdraft.services import application_project_store as _application_project_store
from pcbdraft.services import (
    application_semantic_operations as _application_semantic_operations,
)
from pcbdraft.services import (
    application_tool_inspection as _application_tool_inspection,
)
from pcbdraft.services.application_agent_repair import ApplicationAgentRepairMixin
from pcbdraft.services.application_external_revision import (
    ApplicationExternalRevisionMixin,
)
from pcbdraft.services.application_message_inputs import ApplicationMessageInputMixin
from pcbdraft.services.application_modification_preview import (
    ApplicationModificationPreviewMixin,
)
from pcbdraft.services.application_modification_revert import (
    ApplicationModificationRevertMixin,
)
from pcbdraft.services.application_native_outputs import (
    ApplicationNativeOutputsMixin,
)
from pcbdraft.services.application_pcb_operations import (
    ApplicationPCBOperationsMixin,
)
from pcbdraft.services.application_product_session import (
    ApplicationProductSessionMixin,
)
from pcbdraft.services.application_progress import (
    _attach_progress,
    _progress_stage_evidence,
    _progress_vector,
    _route_state_key,
    _route_state_key_from_record,
    _route_state_record,
    _routing_failure_retry_key,
    _transaction_progress_projection,
)
from pcbdraft.services.application_project_lifecycle import (
    ApplicationProjectLifecycleMixin,
)
from pcbdraft.services.application_project_queries import (
    ApplicationProjectQueriesMixin,
)
from pcbdraft.services.application_release import ApplicationReleaseMixin
from pcbdraft.services.application_status_projection import (
    ApplicationStatusProjectionMixin,
)
from pcbdraft.services.application_tool_inspection import (
    ApplicationToolInspectionMixin,
)
from pcbdraft.services.application_validation import (
    ApplicationValidationMixin,
)
from pcbdraft.services.application_validation import (
    _latest_drc_baseline as _latest_drc_baseline_impl,
)
from pcbdraft.services.doctor import doctor_report
from pcbdraft.services.managed import (
    EmptyDesignRequest,
    load_generation_request,
    materialize_managed_design,
    open_managed_project,
)
from pcbdraft.services.native_operations import (
    _bind_transaction_failure,
    _consistency_rejection,
    _fatal_drc_count,
    _native_delta_postconditions,
    _native_postconditions,
    _operation_failure_code,
    _PCBOperationPostconditionError,
    _reject_stale_copper_transform,
    _routed_component_nets,
    _routing_failure_context,
    _unavailable_consistency_report,
)
from pcbdraft.services.native_operations import (
    _native_board_projection as _native_board_projection_impl,
)
from pcbdraft.services.native_operations import (
    _native_board_projection_complete as _native_board_projection_complete_impl,
)
from pcbdraft.services.native_operations import (
    _native_schematic_projection as _native_schematic_projection_impl,
)
from pcbdraft.services.native_operations import (
    _native_schematic_projection_complete as _native_schematic_projection_complete_impl,
)
from pcbdraft.services.progress import (
    DEFAULT_CONVERGENCE_POLICY,
    ConvergenceDecision,
    ConvergenceObservation,
    EngineeringStage,
    EvidenceCheck,
    EvidenceStatus,
    MetricValue,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    ProgressClassification,
    ProgressVector,
    StageProjection,
    derive_stage,
    evaluate_convergence,
    product_terminal_receipt_id,
    store_product_session_terminal,
    terminal_outcome,
    validate_product_terminal_receipt_id,
)
from pcbdraft.verification.gates import (
    GATE_JSON_LIMIT,
    count_severities,
    structured_violations,
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

APP_FILE_LIMIT = _application_project_store.APP_FILE_LIMIT
APP_PROJECT_SCHEMA = _application_project_store.APP_PROJECT_SCHEMA
APP_PROJECT_VERSION = _application_project_store.APP_PROJECT_VERSION
ATTEMPT_SCHEMA = _application_project_store.ATTEMPT_SCHEMA
ATTEMPT_VERSION = _application_project_store.ATTEMPT_VERSION
CONVERSATION_SCHEMA = _application_project_store.CONVERSATION_SCHEMA
CONVERSATION_VERSION = _application_project_store.CONVERSATION_VERSION
MAX_MESSAGES = _application_project_store.MAX_MESSAGES
ApplicationProject = _application_project_store.ApplicationProject
ApplicationProjectStoreMixin = _application_project_store.ApplicationProjectStoreMixin
_ATTEMPT_FIELDS = _application_project_store._ATTEMPT_FIELDS
_CONVERSATION_FIELDS = _application_project_store._CONVERSATION_FIELDS
_PROJECT_ID = _application_project_store._PROJECT_ID
_STATE_FIELDS = _application_project_store._STATE_FIELDS
_public_readiness_record = _application_project_store._public_readiness_record

TRANSACTION_INSPECTION_FILE_LIMIT = 256 * 1024
TRANSACTION_INSPECTION_ITEM_LIMIT = 16
TRANSACTION_INSPECTION_DEPTH_LIMIT = 8
PENDING_REQUEST_NAME = "pending-agent-request.json"
PENDING_PLAN_NAME = "pending-circuit-plan.json"
PENDING_DESIGN_NAME = "pending-design.pcbir.json"
PENDING_PARTS_NAME = "pending-parts.pcbdraft.json"
_TRANSIENT_STATES = {
    "interpreting",
    "generating",
    "repairing",
    "validating",
    "releasing",
    "applying_change",
    "importing_external",
}


def _transaction_inspection_depth_is_valid(value: object, *, depth: int = 0) -> bool:
    """Reject pathological receipt nesting before projecting explicit detail."""

    if depth > TRANSACTION_INSPECTION_DEPTH_LIMIT:
        return False
    if isinstance(value, Mapping):
        return all(
            _transaction_inspection_depth_is_valid(item, depth=depth + 1)
            for item in value.values()
        )
    if isinstance(value, list):
        return all(
            _transaction_inspection_depth_is_valid(item, depth=depth + 1)
            for item in value
        )
    return value is None or isinstance(value, (bool, int, float, str))


def _native_board_projection_complete(
    snapshot: Any,
    projection: NativeBoardProjection,
) -> bool:
    """Retain the historical helper boundary while delegating its policy."""

    return _native_board_projection_complete_impl(snapshot, projection)


def _native_board_projection(managed: Any) -> NativeBoardProjection:
    """Compatibility wrapper preserving the historical inspector patch point."""

    return _native_board_projection_impl(
        managed,
        inspector=inspect_native_board,
        is_complete=_native_board_projection_complete,
    )


def _native_schematic_projection_complete(
    projection: NativeSchematicProjection,
) -> bool:
    """Retain the historical helper boundary while delegating its policy."""

    return _native_schematic_projection_complete_impl(projection)


def _native_schematic_projection(managed: Any) -> NativeSchematicProjection:
    """Compatibility wrapper preserving the historical inspector patch point."""

    return _native_schematic_projection_impl(
        managed,
        inspector=inspect_native_schematic,
        is_complete=_native_schematic_projection_complete,
    )


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


_application_tool_inspection._configure_legacy_application_hooks(
    open_managed_project_hook=lambda *args, **kwargs: open_managed_project(
        *args, **kwargs
    ),
    load_json_limited_hook=lambda *args, **kwargs: load_json_limited(*args, **kwargs),
    transaction_inspection_file_limit_hook=lambda: TRANSACTION_INSPECTION_FILE_LIMIT,
    transaction_inspection_item_limit_hook=lambda: TRANSACTION_INSPECTION_ITEM_LIMIT,
    app_file_limit_hook=lambda: APP_FILE_LIMIT,
    transaction_inspection_depth_valid_hook=lambda value: (
        _transaction_inspection_depth_is_valid(value)
    ),
)

_application_agent_repair._configure_legacy_application_hooks(
    load_json_limited_hook=lambda *args, **kwargs: load_json_limited(*args, **kwargs),
    validation_error_hook=lambda message: ValidationError(message),
    initial_stackup_layers_hook=lambda request: _initial_stackup_layers(request),
    scope_from_dict_hook=lambda value: Scope.from_dict(value),
    evaluate_scope_hook=lambda value: evaluate_scope(value),
    board_spec_from_dict_hook=lambda value: BoardSpec.from_dict(value),
    slug_hook=lambda value: _slug(value),
    agent_design_request_from_dict_hook=lambda value: AgentDesignRequest.from_dict(
        value
    ),
)

_application_native_outputs._configure_legacy_application_hooks(
    open_managed_project_hook=lambda *args, **kwargs: open_managed_project(
        *args, **kwargs
    ),
    new_run_id_hook=lambda *args, **kwargs: new_run_id(*args, **kwargs),
    run_individual_check_hook=lambda *args, **kwargs: run_individual_check(
        *args, **kwargs
    ),
    load_json_limited_hook=lambda *args, **kwargs: load_json_limited(*args, **kwargs),
    atomic_write_json_hook=lambda *args, **kwargs: atomic_write_json(*args, **kwargs),
    count_severities_hook=lambda *args, **kwargs: count_severities(*args, **kwargs),
    structured_violations_hook=lambda *args, **kwargs: structured_violations(
        *args, **kwargs
    ),
    resource_lock_hook=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
    utc_timestamp_hook=lambda *args, **kwargs: utc_timestamp(*args, **kwargs),
    generate_preview_hook=lambda *args, **kwargs: generate_preview(*args, **kwargs),
    export_manufacturing_output_hook=lambda *args, **kwargs: (
        export_manufacturing_output(*args, **kwargs)
    ),
    app_file_limit_hook=lambda: APP_FILE_LIMIT,
    gate_json_limit_hook=lambda: GATE_JSON_LIMIT,
)

_application_pcb_operations._configure_legacy_application_hooks(
    apply_change_set_hook=lambda *args, **kwargs: apply_change_set(*args, **kwargs),
    qualify_components_hook=lambda *args, **kwargs: qualify_components(*args, **kwargs),
    semantic_diff_hook=lambda *args, **kwargs: semantic_diff(*args, **kwargs),
    new_run_id_hook=lambda *args, **kwargs: new_run_id(*args, **kwargs),
    make_directory_hook=lambda *args, **kwargs: make_directory(*args, **kwargs),
    load_generation_request_hook=lambda *args, **kwargs: load_generation_request(
        *args, **kwargs
    ),
    utc_timestamp_hook=lambda *args, **kwargs: utc_timestamp(*args, **kwargs),
    route_state_key_hook=lambda *args, **kwargs: _route_state_key(*args, **kwargs),
    route_state_record_hook=lambda *args, **kwargs: _route_state_record(
        *args, **kwargs
    ),
    progress_vector_hook=lambda *args, **kwargs: _progress_vector(*args, **kwargs),
    progress_stage_evidence_hook=lambda *args, **kwargs: _progress_stage_evidence(
        *args, **kwargs
    ),
    derive_stage_hook=lambda *args, **kwargs: derive_stage(*args, **kwargs),
    attach_progress_hook=lambda *args, **kwargs: _attach_progress(*args, **kwargs),
    atomic_write_json_hook=lambda *args, **kwargs: atomic_write_json(*args, **kwargs),
    reject_stale_copper_transform_hook=lambda *args, **kwargs: (
        _reject_stale_copper_transform(*args, **kwargs)
    ),
    materialize_managed_design_hook=lambda *args, **kwargs: materialize_managed_design(
        *args, **kwargs
    ),
    open_managed_project_hook=lambda *args, **kwargs: open_managed_project(
        *args, **kwargs
    ),
    inspect_native_consistency_hook=lambda *args, **kwargs: inspect_native_consistency(
        *args, **kwargs
    ),
    unavailable_consistency_report_hook=lambda *args, **kwargs: (
        _unavailable_consistency_report(*args, **kwargs)
    ),
    native_postconditions_hook=lambda *args, **kwargs: _native_postconditions(
        *args, **kwargs
    ),
    native_board_projection_hook=lambda *args, **kwargs: _native_board_projection(
        *args, **kwargs
    ),
    consistency_rejection_hook=lambda *args, **kwargs: _consistency_rejection(
        *args, **kwargs
    ),
    compare_native_operation_delta_hook=lambda *args, **kwargs: (
        compare_native_operation_delta(*args, **kwargs)
    ),
    native_schematic_projection_hook=lambda *args, **kwargs: (
        _native_schematic_projection(*args, **kwargs)
    ),
    native_delta_postconditions_hook=lambda *args, **kwargs: (
        _native_delta_postconditions(*args, **kwargs)
    ),
    sanitize_secret_text_hook=lambda *args, **kwargs: _sanitize_secret_text(
        *args, **kwargs
    ),
    operation_failure_code_hook=lambda *args, **kwargs: _operation_failure_code(
        *args, **kwargs
    ),
    routing_failure_context_hook=lambda *args, **kwargs: _routing_failure_context(
        *args, **kwargs
    ),
    bind_transaction_failure_hook=lambda *args, **kwargs: _bind_transaction_failure(
        *args, **kwargs
    ),
    resource_lock_hook=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
)


class ApplicationService(
    ApplicationAgentRepairMixin,
    ApplicationMessageInputMixin,
    ApplicationNativeOutputsMixin,
    ApplicationPCBOperationsMixin,
    ApplicationToolInspectionMixin,
    ApplicationProductSessionMixin,
    ApplicationProjectQueriesMixin,
    ApplicationProjectLifecycleMixin,
    ApplicationReleaseMixin,
    ApplicationStatusProjectionMixin,
    ApplicationModificationRevertMixin,
    ApplicationModificationPreviewMixin,
    ApplicationValidationMixin,
    ApplicationExternalRevisionMixin,
    ApplicationProjectStoreMixin,
):
    """Single write authority for product projects and their engineering runtime."""

    @staticmethod
    def _status_doctor_report() -> dict[str, Any]:
        """Preserve the historical application.doctor_report patch point."""

        return doctor_report()

    @staticmethod
    def _status_validation_run_id_matches(value: str) -> bool:
        """Preserve the historical application.re patch point."""

        return bool(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", value))

    @staticmethod
    def _message_input_safe_text(value: Any, field: str, *, limit: int) -> str:
        """Preserve the historical application._safe_text patch point."""

        return _safe_text(value, field, limit=limit)

    @staticmethod
    def _message_input_sanitize_secret_text(value: str) -> str:
        """Preserve the historical application sanitizer patch point."""

        return _sanitize_secret_text(value)

    @staticmethod
    def _message_input_max_bytes() -> int:
        """Resolve the historical application message limit dynamically."""

        return MAX_USER_MESSAGE_BYTES

    @staticmethod
    def _message_input_validation_error(message: str) -> Exception:
        """Preserve the historical application.ValidationError patch point."""

        return ValidationError(message)

    @staticmethod
    def _project_query_error_type() -> type[BaseException]:
        """Preserve the historical application.PCBDraftError patch point."""

        return PCBDraftError

    @staticmethod
    def _project_query_resource_lock(*args: Any, **kwargs: Any) -> Any:
        """Preserve the historical application.ResourceLock patch point."""

        return ResourceLock(*args, **kwargs)

    @staticmethod
    def _project_store_sanitize_secret_text(value: str) -> str:
        """Preserve the historical application sanitizer patch point."""

        return _sanitize_secret_text(value)

    @staticmethod
    def _project_store_open_managed_project(design_root: Path) -> Any:
        """Preserve the historical application.open_managed_project patch point."""

        return open_managed_project(design_root)

    @staticmethod
    def _project_store_public_readiness(value: Any) -> Any:
        """Preserve the historical application readiness projection patch point."""

        return _public_readiness_record(value)

    @staticmethod
    def _project_store_transaction_progress(
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Preserve the historical application progress projection patch point."""

        return _transaction_progress_projection(receipt)

    @staticmethod
    def _external_revision_open_managed_project(design_root: Path) -> Any:
        """Preserve the historical managed-project patch point."""

        return open_managed_project(design_root)

    @staticmethod
    def _external_revision_preview_kicad_import(managed: Any) -> Any:
        """Preserve the historical external-preview patch point."""

        return preview_kicad_import(managed)

    @staticmethod
    def _external_revision_apply_kicad_import(
        preview: Any,
        *,
        timeout: float,
    ) -> Path:
        """Preserve the historical external-apply patch point."""

        return apply_kicad_import(preview, timeout=timeout)

    @staticmethod
    def _external_revision_sanitize_secret_text(value: str) -> str:
        """Preserve the historical application sanitizer patch point."""

        return _sanitize_secret_text(value)

    @staticmethod
    def _external_revision_preview_token_is_valid(value: Any) -> bool:
        """Preserve the historical preview-token validation behavior."""

        return (
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )

    @staticmethod
    def _external_revision_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical application resource-lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _external_revision_timestamp() -> str:
        """Preserve the historical application timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _validation_open_managed_project(design_root: Path) -> Any:
        """Preserve the historical validation managed-project patch point."""

        return open_managed_project(design_root)

    @staticmethod
    def _validation_validate_managed_project(managed: Any, **kwargs: Any) -> Any:
        """Preserve the historical aggregate-validation patch point."""

        return validate_managed_project(managed, **kwargs)

    @staticmethod
    def _validation_generate_previews(
        managed: Any,
        output: Path,
        *,
        timeout: float,
    ) -> Any:
        """Preserve the historical project-preview patch point."""

        return generate_previews(managed, output, timeout=timeout)

    @staticmethod
    def _validation_load_json_limited(path: Path) -> Any:
        """Preserve the historical bounded receipt-loader patch point."""

        return load_json_limited(path, APP_FILE_LIMIT)

    @staticmethod
    def _validation_new_run_id() -> str:
        """Preserve the historical run-identity patch point."""

        return new_run_id()

    @staticmethod
    def _validation_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical validation resource-lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _validation_timestamp() -> str:
        """Preserve the historical validation timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _modification_preview_open_managed_project(design_root: Path) -> Any:
        """Preserve the historical managed-project patch point."""

        return open_managed_project(design_root)

    @staticmethod
    def _modification_preview_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical application resource-lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _modification_preview_timestamp() -> str:
        """Preserve the historical application timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _modification_preview_feedback(request: str) -> dict[str, Any]:
        """Preserve the historical repair-feedback patch point."""

        return user_revision_feedback(request)

    @staticmethod
    def _modification_revert_load_json(path: Path, limit: int) -> Any:
        """Preserve the historical bounded JSON-loader patch point."""

        return load_json_limited(path, limit)

    @staticmethod
    def _modification_revert_atomic_write_json(path: Path, value: Any) -> None:
        """Preserve the historical atomic JSON-writer patch point."""

        atomic_write_json(path, value)

    @staticmethod
    def _modification_revert_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical application resource-lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _modification_revert_timestamp() -> str:
        """Preserve the historical application timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _modification_revert_open_managed_project(path: Path) -> Any:
        """Preserve the historical managed-project patch point."""

        return open_managed_project(path)

    @staticmethod
    def _modification_revert_replace(source: Path, destination: Path) -> None:
        """Preserve the historical atomic directory-replacement patch point."""

        os.replace(source, destination)

    @staticmethod
    def _modification_revert_operation_failure_code(
        error: BaseException,
        *,
        stage: str,
        tool_name: str,
    ) -> str:
        """Preserve the historical native-operation error classifier."""

        return _operation_failure_code(error, stage=stage, tool_name=tool_name)

    @staticmethod
    def _modification_revert_sanitize_secret_text(value: str) -> str:
        """Preserve the historical application sanitizer patch point."""

        return _sanitize_secret_text(value)

    @staticmethod
    def _modification_revert_attach_progress(
        receipt: dict[str, Any],
        before: ProgressVector,
        after: ProgressVector,
        before_stage: StageProjection,
        after_stage: StageProjection,
    ) -> None:
        """Preserve the historical progress attachment patch point."""

        _attach_progress(receipt, before, after, before_stage, after_stage)

    @staticmethod
    def _modification_revert_transaction_progress(
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Preserve the historical public progress projection patch point."""

        return _transaction_progress_projection(receipt)

    @staticmethod
    def _modification_revert_postcondition_error(
        code: str,
        message: str,
    ) -> BaseException:
        """Preserve the historical native postcondition-error patch point."""

        return _PCBOperationPostconditionError(code, message)

    @staticmethod
    def _release_open_managed_project(design_root: Path) -> Any:
        """Preserve the historical release managed-project patch point."""

        return open_managed_project(design_root)

    @staticmethod
    def _release_new_run_id() -> str:
        """Preserve the historical release run-identity patch point."""

        return new_run_id()

    @staticmethod
    def _release_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical release resource-lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _release_timestamp() -> str:
        """Preserve the historical release timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _release_build_manufacturing(
        design_root: Path,
        output: Path,
        **kwargs: Any,
    ) -> Any:
        """Preserve the historical signed-release builder patch point."""

        return build_manufacturing_release(design_root, output, **kwargs)

    @staticmethod
    def _release_verify_manufacturing(root: Path) -> Any:
        """Preserve the historical offline-verification patch point."""

        return verify_manufacturing_release(root)

    @staticmethod
    def _project_lifecycle_sanitize_secret_text(value: str) -> str:
        """Preserve the historical application sanitizer patch point."""

        return _sanitize_secret_text(value)

    @staticmethod
    def _project_lifecycle_safe_text(
        value: Any,
        field: str,
        *,
        limit: int,
    ) -> str:
        """Preserve the historical bounded-text patch point."""

        return _safe_text(value, field, limit=limit)

    @staticmethod
    def _project_lifecycle_slug(value: str) -> str:
        """Preserve the historical project-slug patch point."""

        return _slug(value)

    @staticmethod
    def _project_lifecycle_token_hex(length: int) -> str:
        """Preserve the historical project-identity token patch point."""

        return secrets.token_hex(length)

    @staticmethod
    def _project_lifecycle_mkdtemp(*, prefix: str, dir: Path) -> str:
        """Preserve the historical private-staging patch point."""

        return tempfile.mkdtemp(prefix=prefix, dir=dir)

    @staticmethod
    def _project_lifecycle_chmod(path: Path, mode: int) -> None:
        """Preserve the historical private-directory mode patch point."""

        os.chmod(path, mode)

    @staticmethod
    def _project_lifecycle_timestamp() -> str:
        """Preserve the historical project timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _project_lifecycle_make_directory(path: Path) -> Path:
        """Preserve the historical project-directory patch point."""

        return make_directory(path)

    @staticmethod
    def _project_lifecycle_atomic_write_json(path: Path, value: Any) -> None:
        """Preserve the historical project-record writer patch point."""

        atomic_write_json(path, value)

    @staticmethod
    def _project_lifecycle_rmtree(path: Path, *, ignore_errors: bool) -> None:
        """Preserve the historical private-stage cleanup patch point."""

        shutil.rmtree(path, ignore_errors=ignore_errors)

    @staticmethod
    def _project_lifecycle_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical project-publication lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _project_lifecycle_replace(source: Path, destination: Path) -> None:
        """Preserve the historical atomic project-publication patch point."""

        os.replace(source, destination)

    @staticmethod
    def _project_lifecycle_materialize_managed_design(
        request: EmptyDesignRequest,
        design: Design,
        output: Path,
        *,
        graph: PartGraph,
    ) -> Any:
        """Preserve the historical empty-project materializer patch point."""

        return materialize_managed_design(
            request,
            design,
            output,
            graph=graph,
        )

    @staticmethod
    def _product_session_process_status(
        value: ProcessStatus | str,
    ) -> ProcessStatus:
        """Preserve the historical process-status conversion patch point."""

        return ProcessStatus(value)

    @staticmethod
    def _product_session_validate_receipt_id(value: str) -> str:
        """Preserve the historical explicit receipt-ID patch point."""

        return validate_product_terminal_receipt_id(value)

    @staticmethod
    def _product_session_receipt_id(session_id: str, turn_id: str) -> str:
        """Preserve the historical derived receipt-ID patch point."""

        return product_terminal_receipt_id(session_id, turn_id)

    @staticmethod
    def _product_session_resource_lock(root: Path, locks_root: Path) -> Any:
        """Preserve the historical product-session lock patch point."""

        return ResourceLock(root, locks_root)

    @staticmethod
    def _product_session_load_json(path: Path, limit: int) -> Any:
        """Preserve the historical retained-receipt loader patch point."""

        return load_json_limited(path, limit)

    @staticmethod
    def _product_session_parse_receipt(value: Any) -> ProductSessionTerminalReceipt:
        """Preserve the historical terminal-receipt parser patch point."""

        return ProductSessionTerminalReceipt.from_dict(value)

    @staticmethod
    def _product_session_stage_projection(
        stage: EngineeringStage,
        release_gate_passed: bool,
        blockers: tuple[str, ...],
    ) -> StageProjection:
        """Preserve the historical retained-stage projection patch point."""

        return StageProjection(stage, release_gate_passed, blockers)

    @staticmethod
    def _product_session_terminal_outcome(
        *,
        process_status: ProcessStatus,
        requested_reason: str | None,
        stage: StageProjection,
    ) -> tuple[Any, str]:
        """Preserve the historical terminal-classification patch point."""

        return terminal_outcome(
            process_status=process_status,
            requested_reason=requested_reason,
            stage=stage,
        )

    @staticmethod
    def _product_session_timestamp() -> str:
        """Preserve the historical terminal-receipt timestamp patch point."""

        return utc_timestamp()

    @staticmethod
    def _product_session_receipt(*args: Any) -> ProductSessionTerminalReceipt:
        """Preserve the historical terminal-receipt constructor patch point."""

        return ProductSessionTerminalReceipt(*args)

    @staticmethod
    def _product_session_store(
        project_root: Path,
        receipt: ProductSessionTerminalReceipt,
    ) -> Path:
        """Preserve the historical immutable receipt-store patch point."""

        return store_product_session_terminal(project_root, receipt)

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        provider_name: str = "auto",
        provider: IntentProvider | None = None,
        recover_interrupted: bool = True,
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
        if recover_interrupted:
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

    def create_project(self, name: str, request: str) -> dict[str, Any]:
        draft = self.create_draft(name)
        return self.send_message(draft["project"]["id"], request)

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
            "inspect_transaction",
        }:
            return self._inspect_pcb_tool(project_id, tool_name, arguments)
        if tool_name == "observe_board_region":
            view = self.open_project(project_id)
            state = view.get("state")
            if (
                not isinstance(state, Mapping)
                or state.get("revision") != expected_revision
            ):
                raise ValidationError(
                    "project changed before the board region could be observed"
                )
            return self._with_tool_result(
                view,
                {"operation": "observe_board_region", **arguments},
            )
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
        baseline_design_revision = int(project.state["design_revision"])
        before_progress, before_stage = self._current_progress_and_stage(project)
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-kicad-part-registration-receipt",
            "version": 2,
            "status": "preparing",
            "operation": "register_kicad_part",
            "native_scope": "catalog_plus_native_rematerialization",
            "created_at": utc_timestamp(),
            "baseline_revision": expected_revision,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": None,
            "committed_revision": None,
            "committed_design_revision": None,
            "before_hash": authoritative.design.content_hash(),
            "before_catalog_hash": before_catalog_hash,
            "part_id": value.get("id"),
            "rollback_performed": False,
            "rollback": {
                "state": "not_required",
                "performed": False,
                "live_unchanged": True,
            },
            "artifact": {
                "transaction_id": transaction_id,
                "receipt": "receipt.json",
            },
        }
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        atomic_write_json(receipt_path, receipt)
        stage = "inspection"
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
                        "native_scope": "catalog_noop_no_native_write",
                        "completed_at": utc_timestamp(),
                        "candidate_revision": baseline_design_revision,
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
                    "candidate_revision": baseline_design_revision + 1,
                    "after_hash": candidate.content_hash(),
                    "after_catalog_hash": after_catalog_hash,
                    "symbol": symbol.to_dict(),
                    "footprint": footprint.to_dict(),
                }
            )
            atomic_write_json(receipt_path, receipt)
            request = load_generation_request(authoritative.requirements_path)
            stage = "materialization"
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
            candidate_revision = baseline_design_revision + 1
            stage = "native_consistency"
            consistency_failure: PCBDraftError | None = None
            try:
                consistency = inspect_native_consistency(
                    candidate,
                    staged_project.schematic_path,
                    staged_project.board_path,
                    candidate_revision=candidate_revision,
                    graph=graph,
                )
            except PCBDraftError as exc:
                consistency_failure = exc
                consistency = _unavailable_consistency_report(candidate_revision)
            atomic_write_json(
                transaction / "native-consistency.json", consistency.to_dict()
            )
            receipt["consistency_passed"] = consistency.consistency_passed
            receipt["postconditions"] = _native_postconditions(
                "register_kicad_part", consistency
            )
            receipt["artifact"]["native_consistency"] = "native-consistency.json"
            try:
                candidate_board = _native_board_projection(staged_project)
            except PCBDraftError:
                candidate_board = None
            after_progress = _progress_vector(
                candidate,
                graph,
                candidate_revision,
                consistency=consistency,
                board=candidate_board,
                fatal_drc=before_progress.fatal_drc_count.for_revision(
                    candidate_revision
                ),
                error_drc=before_progress.error_drc_count.for_revision(
                    candidate_revision
                ),
                erc_error=before_progress.erc_error_count.for_revision(
                    candidate_revision
                ),
            )
            after_stage = derive_stage(
                after_progress,
                _progress_stage_evidence(
                    candidate,
                    candidate_revision,
                    requirements_frozen=staged_project.requirements_path.is_file(),
                    consistency=consistency,
                    progress=after_progress,
                    erc_check=EvidenceCheck.unknown(candidate_revision),
                    drc_check=EvidenceCheck.unknown(candidate_revision),
                ),
            )
            _attach_progress(
                receipt,
                before_progress,
                after_progress,
                before_stage,
                after_stage,
            )
            if consistency_failure is not None:
                raise _PCBOperationPostconditionError(
                    "native_verification_failed",
                    "native KiCad inspection failed",
                ) from consistency_failure
            if not consistency.consistency_passed:
                raise _consistency_rejection("register_kicad_part", None, consistency)
            stage = "native_delta"
            native_delta = compare_native_operation_delta(
                "register_kicad_part",
                {},
                authoritative.design,
                candidate,
                _native_board_projection(authoritative),
                _native_board_projection(staged_project),
                before_schematic=_native_schematic_projection(authoritative),
                after_schematic=_native_schematic_projection(staged_project),
                graph=graph,
            )
            atomic_write_json(
                transaction / "native-operation-delta.json", native_delta.to_dict()
            )
            receipt["artifact"]["native_delta"] = "native-operation-delta.json"
            receipt["postconditions"].extend(_native_delta_postconditions(native_delta))
            receipt["native_delta"] = {
                "operation_checked": True,
                "policy": native_delta.policy,
                "passed": native_delta.passed,
                "failed_checks": [
                    item.name for item in native_delta.checks if not item.passed
                ][:4],
            }
            atomic_write_json(receipt_path, receipt)
            if not native_delta.passed:
                raise _PCBOperationPostconditionError(
                    "native_delta_failed",
                    "part registration changed unrelated native project state",
                )
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage=stage, tool_name="register_kicad_part"
            )
            _attach_progress(
                receipt,
                before_progress,
                before_progress,
                before_stage,
                before_stage,
            )
            atomic_write_json(receipt_path, receipt)
            _bind_transaction_failure(exc, transaction_id)
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
                # Publish success last.  A receipt write failure must still be
                # able to restore the live design, event, and project records
                # without leaving a durable applied claim for a rolled-back part.
                receipt.update(
                    {
                        "status": "applied",
                        "applied_at": utc_timestamp(),
                        "committed_revision": current.state["revision"],
                        "committed_design_revision": current.state["design_revision"],
                        "rollback": {
                            "state": "committed",
                            "performed": False,
                            "live_unchanged": False,
                        },
                    }
                )
                atomic_write_json(receipt_path, receipt)
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
                receipt.pop("applied_at", None)
                receipt["committed_revision"] = None
                receipt["committed_design_revision"] = None
                receipt["error_code"] = _operation_failure_code(
                    exc, stage="publication", tool_name="register_kicad_part"
                )
                receipt["rollback_performed"] = moved_before and not rollback_failures
                receipt["rollback"] = {
                    "state": (
                        "restored"
                        if moved_before and not rollback_failures
                        else "incomplete"
                        if rollback_failures
                        else "not_required"
                    ),
                    "performed": moved_before and not rollback_failures,
                    "live_unchanged": not rollback_failures,
                }
                if not rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(baseline_design_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    failure = PCBDraftError(
                        "part publication failed and rollback was incomplete"
                    )
                    _bind_transaction_failure(failure, transaction_id)
                    raise failure from exc
                _bind_transaction_failure(exc, transaction_id)
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
                "progress_before": receipt["progress_before"],
                "progress_after": receipt["progress_after"],
                "progress_delta": receipt["progress_delta"],
                "stage": receipt["stage_after"],
            },
        )

    @staticmethod
    def _native_progress_sources(
        managed: Any,
        design_revision: int,
        graph: PartGraph,
    ) -> tuple[NativeConsistencyReport | None, NativeBoardProjection | None]:
        """Reuse retained native projections, failing to unknown rather than zero."""

        try:
            board = _native_board_projection(managed)
            schematic = _native_schematic_projection(managed)
            report = compare_native_consistency(
                managed.design,
                schematic,
                board,
                candidate_revision=design_revision,
                graph=graph,
            )
        except PCBDraftError:
            return None, None
        return report, board

    @staticmethod
    def _retained_check_progress(
        project: ApplicationProject,
        design_hash: str,
        kind: str,
        design_revision: int,
    ) -> tuple[MetricValue, MetricValue | None, EvidenceCheck]:
        """Read the newest check bound to the current semantic design hash."""

        retained_validation = project.state.get("last_validation")
        if isinstance(retained_validation, Mapping):
            relative_report = retained_validation.get("report")
            if (
                isinstance(relative_report, str)
                and retained_validation.get("source_design_revision") == design_revision
            ):
                report_path = project.root / relative_report
                try:
                    report_path.relative_to(project.root)
                except ValueError:
                    pass
                else:
                    aggregate = ApplicationService._aggregate_check_progress(
                        report_path.parent,
                        design_hash,
                        kind,
                        design_revision,
                    )
                    if aggregate[0].status is not EvidenceStatus.UNKNOWN:
                        return aggregate

        root = project.root / "validation"
        if not root.is_dir() or root.is_symlink():
            return (
                MetricValue.unknown(design_revision),
                None,
                EvidenceCheck.unknown(design_revision),
            )
        for directory in sorted(root.iterdir(), reverse=True)[:100]:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                receipt = load_json_limited(directory / "receipt.json", APP_FILE_LIMIT)
                if (
                    not isinstance(receipt, Mapping)
                    or receipt.get("schema") != "pcbdraft-individual-check-receipt"
                    or receipt.get("status") != "complete"
                    or receipt.get("check") != kind
                    or receipt.get("design_content_hash") != design_hash
                    or receipt.get("source_design_revision") != design_revision
                    or not isinstance(receipt.get("report"), str)
                ):
                    continue
                report = load_json_limited(
                    directory / str(receipt["report"]), GATE_JSON_LIMIT
                )
            except PCBDraftError:
                # Malformed or partial evidence cannot become a known zero.
                continue
            if (
                not isinstance(report, Mapping)
                or report.get("check") != kind
                or report.get("design_content_hash") != design_hash
                or report.get("outcome") != receipt.get("outcome")
                or report.get("state") != receipt.get("state")
            ):
                continue
            details = report.get("details")
            violations = (
                details.get("violations") if isinstance(details, Mapping) else None
            )
            if not isinstance(violations, list):
                continue
            errors, _warnings = count_severities(violations)
            outcome = report.get("outcome")
            if outcome not in {"pass", "fail"}:
                continue
            metric = MetricValue.known(errors, design_revision)
            fatal = (
                MetricValue.known(_fatal_drc_count(violations), design_revision)
                if kind == "run_drc"
                else None
            )
            return (
                metric,
                fatal,
                EvidenceCheck.known(outcome == "pass", design_revision),
            )
        return (
            MetricValue.unknown(design_revision),
            None,
            EvidenceCheck.unknown(design_revision),
        )

    @staticmethod
    def _bind_aggregate_validation_revision(
        validation_root: Path, design_hash: str, design_revision: int
    ) -> None:
        """Bind a complete aggregate receipt to the revision that produced it."""

        receipt_path = validation_root / "receipt.json"
        try:
            receipt = load_json_limited(receipt_path, APP_FILE_LIMIT)
        except PCBDraftError:
            return
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-validation-receipt"
            or receipt.get("status") != "complete"
            or receipt.get("design_content_hash") != design_hash
        ):
            return
        receipt["source_design_revision"] = design_revision
        atomic_write_json(receipt_path, receipt)

    @staticmethod
    def _latest_drc_baseline(
        project: ApplicationProject,
    ) -> tuple[Path, int, str] | None:
        """Preserve historical receipt-loader and error patch paths."""

        return _latest_drc_baseline_impl(
            project,
            load_record=load_json_limited,
            file_limit=APP_FILE_LIMIT,
            record_error=PCBDraftError,
        )

    @staticmethod
    def _aggregate_check_progress(
        validation_root: Path,
        design_hash: str,
        kind: str,
        design_revision: int,
    ) -> tuple[MetricValue, MetricValue | None, EvidenceCheck]:
        """Read one aggregate validation's normalized ERC/DRC evidence."""

        unknown = (
            MetricValue.unknown(design_revision),
            None,
            EvidenceCheck.unknown(design_revision),
        )
        if kind not in {"run_erc", "run_drc"}:
            return unknown
        try:
            receipt = load_json_limited(
                validation_root / "receipt.json", APP_FILE_LIMIT
            )
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema") != "pcbdraft-validation-receipt"
                or receipt.get("status") != "complete"
                or receipt.get("design_content_hash") != design_hash
                or receipt.get("source_design_revision") != design_revision
                or not isinstance(receipt.get("tool_runs"), Mapping)
            ):
                return unknown
            tool_kind = "erc" if kind == "run_erc" else "drc"
            tool_run = receipt["tool_runs"].get(tool_kind)
            if (
                not isinstance(tool_run, Mapping)
                or tool_run.get("status") != "completed"
                or tool_run.get("failure") is not None
                or not isinstance(tool_run.get("normalized_report"), str)
            ):
                return unknown
            report_name = str(tool_run["normalized_report"])
            if (
                Path(report_name).name != report_name
                or report_name != f"{tool_kind}.json"
            ):
                return unknown
            document = load_json_limited(validation_root / report_name, GATE_JSON_LIMIT)
        except PCBDraftError:
            return unknown
        if not isinstance(document, Mapping) or not document:
            return unknown
        errors, _warnings = count_severities(document)
        metric = MetricValue.known(errors, design_revision)
        if kind == "run_erc":
            return metric, None, EvidenceCheck.known(errors == 0, design_revision)
        fatal = MetricValue.known(_fatal_drc_count(document), design_revision)
        return metric, fatal, EvidenceCheck.known(errors == 0, design_revision)

    @staticmethod
    def _transaction_receipts(
        project: ApplicationProject, *, strict: bool = False
    ) -> tuple[dict[str, Any], ...]:
        root = project.root / "transactions"
        if not root.is_dir() or root.is_symlink():
            return ()
        paths = tuple(root.glob("*/receipt.json"))
        if strict and len(paths) > 2_000:
            raise ValidationError("route convergence history exceeds its bound")
        records: list[tuple[str, str, dict[str, Any]]] = []
        for path in paths:
            try:
                value = load_json_limited(path, APP_FILE_LIMIT)
            except PCBDraftError as exc:
                if strict:
                    raise ValidationError(
                        "route convergence history contains an unreadable receipt"
                    ) from exc
                continue
            if isinstance(value, dict):
                created_at = value.get("created_at")
                records.append(
                    (
                        created_at if isinstance(created_at, str) else "",
                        path.parent.name,
                        value,
                    )
                )
            elif strict:
                raise ValidationError(
                    "route convergence history contains a malformed receipt"
                )
        records.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(item[2] for item in records[: 2_000 if strict else 100])

    @classmethod
    def _routing_failure_count(
        cls, project: ApplicationProject, design_revision: int, state_key: str | None
    ) -> int:
        return sum(
            receipt.get("operation") == "route_net"
            and receipt.get("status") == "failed"
            and receipt.get("baseline_design_revision") == design_revision
            and (state_key is None or receipt.get("convergence_state_key") == state_key)
            and isinstance(receipt.get("routing_failure"), Mapping)
            for receipt in cls._transaction_receipts(project)
        )

    @classmethod
    def _route_convergence_decision(
        cls,
        project: ApplicationProject,
        *,
        state_key: str,
    ) -> ConvergenceDecision:
        observations: list[ConvergenceObservation] = []
        matching_retry_key: str | None = None
        try:
            receipts = cls._transaction_receipts(project, strict=True)
            for receipt in reversed(receipts):
                schema = receipt.get("schema")
                if schema not in {
                    "pcbdraft-flat-operation-receipt",
                    "pcbdraft-kicad-part-registration-receipt",
                    "pcbdraft-agent-repair-transaction",
                }:
                    raise ValidationError(
                        "route convergence history contains an unknown receipt"
                    )
                if schema != "pcbdraft-flat-operation-receipt":
                    if receipt.get("operation") == "route_net":
                        raise ValidationError(
                            "route convergence receipt schema is inconsistent"
                        )
                    continue
                operation = receipt.get("operation")
                if not isinstance(operation, str):
                    raise ValidationError(
                        "route convergence history contains a malformed operation"
                    )
                if operation != "route_net":
                    continue
                if receipt.get("version") != 2 or receipt.get("status") not in {
                    "failed",
                    "applied",
                }:
                    raise ValidationError(
                        "route convergence history contains an incomplete route receipt"
                    )
                retained_state = _route_state_key_from_record(
                    receipt.get("convergence_state")
                )
                if receipt.get("convergence_state_key") != retained_state:
                    raise ValidationError("route convergence state key is inconsistent")
                delta = receipt.get("progress_delta")
                classification = (
                    delta.get("classification") if isinstance(delta, Mapping) else None
                )
                if (
                    not isinstance(delta, Mapping)
                    or delta.get("schema") != "pcbdraft-progress-delta"
                    or delta.get("version") != 1
                    or classification
                    not in {item.value for item in ProgressClassification}
                ):
                    raise ValidationError(
                        "route convergence progress evidence is malformed"
                    )
                failure = receipt.get("routing_failure")
                retry_key = (
                    _routing_failure_retry_key(failure) if failure is not None else None
                )
                observations.append(
                    ConvergenceObservation(
                        retained_state,
                        ProgressClassification(classification),
                        retry_key,
                    )
                )
                if retained_state == state_key and retry_key is not None:
                    matching_retry_key = retry_key
        except ValidationError:
            return ConvergenceDecision(
                False,
                "strategy_change_required",
                "convergence_history_invalid",
                0,
                0,
            )
        return evaluate_convergence(
            tuple(observations),
            state_key=state_key,
            retry_key=matching_retry_key,
            policy=DEFAULT_CONVERGENCE_POLICY,
        )

    def _current_progress_and_stage(
        self, project: ApplicationProject
    ) -> tuple[ProgressVector, StageProjection]:
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            revision = int(project.state["design_revision"])
            return (
                ProgressVector.unknown(revision),
                StageProjection(
                    EngineeringStage.NOT_STARTED,
                    False,
                    ("requirements_not_frozen",),
                ),
            )
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        revision = int(project.state["design_revision"])
        progress, stage, _consistency = self._managed_progress_and_stage(
            project,
            managed,
            revision,
        )
        return progress, stage

    def _managed_progress_and_stage(
        self,
        project: ApplicationProject,
        managed: Any,
        revision: int,
        *,
        validation_root: Path | None = None,
        include_routing_failures: bool = True,
    ) -> tuple[ProgressVector, StageProjection, NativeConsistencyReport | None]:
        """Project revision progress for either the live or one staged tree."""

        graph = managed.graph.with_footprint_overrides(managed.design)
        consistency, board = self._native_progress_sources(managed, revision, graph)
        if validation_root is None:
            erc, _unused, erc_check = self._retained_check_progress(
                project, managed.design.content_hash(), "run_erc", revision
            )
            drc, fatal, drc_check = self._retained_check_progress(
                project, managed.design.content_hash(), "run_drc", revision
            )
        else:
            erc, _unused, erc_check = self._aggregate_check_progress(
                validation_root, managed.design.content_hash(), "run_erc", revision
            )
            drc, fatal, drc_check = self._aggregate_check_progress(
                validation_root, managed.design.content_hash(), "run_drc", revision
            )
        progress = _progress_vector(
            managed.design,
            graph,
            revision,
            consistency=consistency,
            board=board,
            fatal_drc=fatal,
            error_drc=drc,
            erc_error=erc,
            routing_failure_count=(
                self._routing_failure_count(project, revision, None)
                if include_routing_failures
                else 0
            ),
        )
        stage = derive_stage(
            progress,
            _progress_stage_evidence(
                managed.design,
                revision,
                requirements_frozen=managed.requirements_path.is_file(),
                consistency=consistency,
                progress=progress,
                erc_check=erc_check,
                drc_check=drc_check,
            ),
        )
        return progress, stage, consistency

    @staticmethod
    def _require_current_native_consistency(
        report: NativeConsistencyReport | None,
        revision: int,
        *,
        label: str,
    ) -> NativeConsistencyReport:
        if (
            report is None
            or report.candidate_revision != revision
            or report.schematic_status != "evaluated"
            or report.board_status != "evaluated"
            or not report.consistency_passed
        ):
            raise _PCBOperationPostconditionError(
                "native_consistency_failed",
                f"legacy modification {label} native consistency is unavailable or failing",
            )
        return report

    @staticmethod
    def _entry_mapping(value: Any, field: str) -> dict[str, Any]:
        """Preserve the historical semantic entry-mapping boundary."""

        return _application_semantic_operations._entry_mapping(value, field)

    @staticmethod
    def _parameter_mapping(value: Any, field: str) -> dict[str, Any]:
        """Preserve the historical semantic parameter-mapping boundary."""

        return _application_semantic_operations._parameter_mapping(value, field)

    @classmethod
    def _flat_semantic_operations(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        design: Design,
        *,
        graph: PartGraph,
    ) -> list[dict[str, Any]]:
        """Preserve historical parsers, validators, and operation patch paths."""

        return _application_semantic_operations._flat_semantic_operations(
            tool_name,
            arguments,
            design,
            graph=graph,
            connect_parser=parse_connect_group,
            place_parser=parse_place_group,
            validate_connect_group=cls._validate_connect_group,
            validate_place_group=cls._validate_place_group,
            operation_builder=cls._flat_semantic_operation,
        )

    @staticmethod
    def _validate_connect_group(
        entries: tuple[ConnectGroupEntry, ...],
        design: Design,
        graph: PartGraph,
    ) -> None:
        """Preserve the historical connect-group validation boundary."""

        _application_semantic_operations._validate_connect_group(
            entries,
            design,
            graph,
        )

    @staticmethod
    def _validate_place_group(
        entries: tuple[PlaceGroupEntry, ...],
        design: Design,
        graph: PartGraph,
    ) -> None:
        """Preserve the historical routed-copper patch path."""

        _application_semantic_operations._validate_place_group(
            entries,
            design,
            graph,
            routed_component_nets=_routed_component_nets,
        )

    @classmethod
    def _flat_semantic_operation(
        cls, tool_name: str, arguments: dict[str, Any], design: Design
    ) -> dict[str, Any]:
        """Preserve historical normalizer and entropy patch paths."""

        return _application_semantic_operations._flat_semantic_operation(
            tool_name,
            arguments,
            design,
            token_hex=secrets.token_hex,
            deep_copy=copy.deepcopy,
            entry_mapping=cls._entry_mapping,
            parameter_mapping=cls._parameter_mapping,
        )

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

        clean = self._normalize_message_text(text, "reply text")
        project = self._open(project_id)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            conversation = current.conversation
            binding = self._reply_delivery_binding(turn_id, index)
            if binding is not None and self._reply_already_delivered(
                conversation, binding
            ):
                return self._public_project(current)
            self._append_message(
                conversation,
                "assistant",
                "reply",
                clean,
                data=binding,
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
        clean = self._normalize_message_text(text, "message")
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
        baseline_design_revision = int(project.state["design_revision"])
        before_progress: ProgressVector | None = None
        before_stage: StageProjection | None = None
        before_consistency: NativeConsistencyReport | None = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            authoritative = open_managed_project(project.design_root)
            authoritative.assert_synchronized()
            if authoritative.design.design_id != request.design_id:
                raise ValidationError(
                    "authoritative design identity differs from the pending repair plan"
                )
            before_progress, before_stage, before_consistency = (
                self._managed_progress_and_stage(
                    project,
                    authoritative,
                    baseline_design_revision,
                )
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
            "version": 2,
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
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": baseline_design_revision + 1,
            "postconditions": [],
            "artifact": {},
        }
        if before_progress is None or before_stage is None:
            raise ValidationError("repair transaction lacks authoritative progress")
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        receipt["convergence_classification"] = receipt["progress_delta"][
            "classification"
        ]
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
                canonical_revision=expected_revision,
                design_revision=baseline_design_revision + 1,
            )
            self._bind_aggregate_validation_revision(
                transaction / "validation",
                candidate.design.content_hash(),
                baseline_design_revision + 1,
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
                "source_design_revision": baseline_design_revision + 1,
                "assurance": str(
                    candidate.design.metadata.get("assurance", "provisional")
                ),
            }
            atomic_write_json(
                transaction / "semantic-diff.json",
                semantic_diff(authoritative.design, candidate.design),
            )
            candidate_progress, candidate_stage, candidate_consistency = (
                self._managed_progress_and_stage(
                    project,
                    candidate,
                    baseline_design_revision + 1,
                    validation_root=transaction / "validation",
                    include_routing_failures=False,
                )
            )
            verified_candidate = self._require_current_native_consistency(
                candidate_consistency,
                baseline_design_revision + 1,
                label="staged candidate",
            )
            atomic_write_json(
                transaction / "native-consistency-before.json",
                (
                    before_consistency.to_dict()
                    if before_consistency is not None
                    else _unavailable_consistency_report(
                        baseline_design_revision
                    ).to_dict()
                ),
            )
            atomic_write_json(
                transaction / "native-consistency-candidate.json",
                verified_candidate.to_dict(),
            )
            receipt["artifact"] = {
                "semantic_diff": "semantic-diff.json",
                "native_consistency_before": "native-consistency-before.json",
                "native_consistency_candidate": "native-consistency-candidate.json",
                "validation": "validation",
            }
            receipt["candidate_progress"] = candidate_progress.to_dict()
            receipt["candidate_stage"] = candidate_stage.to_dict()
            receipt["postconditions"] = [
                {
                    "name": "candidate_native_consistency",
                    "passed": verified_candidate.consistency_passed,
                }
            ]
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage="native_consistency", tool_name="repair_candidate"
            )
            _attach_progress(
                receipt,
                before_progress,
                before_progress,
                before_stage,
                before_stage,
            )
            receipt["convergence_classification"] = receipt["progress_delta"][
                "classification"
            ]
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
        rejected = candidate_feedback is not None
        if rejected:
            receipt["repair_feedback"] = candidate_feedback
        else:
            receipt["result_status"] = (
                "validated" if validation_run.candidate_ready else "generated"
            )
        # Candidate-only evidence may be durable while the top-level live
        # progress remains neutral.  The terminal ready/rejected fact is
        # published only after matching project records and its event.
        prepublication_receipt = copy.deepcopy(receipt)
        atomic_write_json(receipt_path, prepublication_receipt)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while a repair candidate was validated"
                    )
                current_managed = open_managed_project(current.design_root)
                current_managed.assert_synchronized()
                if current_managed.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed while a repair was staged"
                    )
                current.state["status"] = (
                    "repair_failed" if rejected else "change_ready"
                )
                if not rejected:
                    current.state["active_transaction"] = transaction_id
                current.state["revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                text = (
                    "The repair candidate retained deterministic L1-L3 failures; "
                    "the authoritative design was not changed."
                    if rejected
                    else "A replacement design passed deterministic L1-L3 repair "
                    "gates and is staged for atomic application."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_rejected" if rejected else "repair_ready",
                    text,
                    data=(
                        {
                            "transaction_id": transaction_id,
                            "repair_feedback": candidate_feedback,
                        }
                        if rejected
                        else {"transaction_id": transaction_id}
                    ),
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "repair.candidate_failed" if rejected else "repair.ready",
                    text,
                    level="error" if rejected else "info",
                )
                self._write_records(current.root, current.state, current.conversation)
                terminal = "rejected" if rejected else "ready"
                receipt["status"] = terminal
                receipt[f"{terminal}_at"] = utc_timestamp()
                receipt["publication"] = {
                    "status": "committed",
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.clear()
                receipt.update(copy.deepcopy(prepublication_receipt))
                receipt["publication"] = {
                    "status": (
                        "rollback_incomplete" if rollback_failures else "failed"
                    ),
                    "error_code": "publication_failed",
                    "failure": _sanitize_secret_text(str(exc))[:2048],
                    "rollback": {
                        "state": "incomplete" if rollback_failures else "restored",
                        "performed": not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    receipt["status"] = "rollback_incomplete"
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(before_progress.source_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                    receipt["convergence_classification"] = receipt["progress_delta"][
                        "classification"
                    ]
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "repair candidate publication failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.open_project(project_id)
        result["transaction_progress"] = _transaction_progress_projection(receipt)
        return result

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
            if (
                candidate.name.startswith(".")
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
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
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-agent-repair-transaction"
            or receipt.get("status") != "ready"
            or receipt.get("version") not in {1, 2}
        ):
            raise ValidationError("semantic change receipt is not ready")
        staged = transaction / "staged"
        before = transaction / "before"
        baseline_progress, baseline_stage = self._current_progress_and_stage(project)
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            original_receipt = copy.deepcopy(receipt)
            moved_before = False
            moved_candidate = False
            event_path: Path | None = None
            before_progress = baseline_progress
            before_stage = baseline_stage
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed before candidate application"
                    )
                if current.state["active_transaction"] != transaction_id:
                    raise ValidationError("active semantic transaction changed")
                if before.exists() or before.is_symlink():
                    raise ValidationError("candidate application backup already exists")
                current_managed = open_managed_project(current.design_root)
                staged_managed = open_managed_project(staged)
                current_managed.assert_synchronized()
                staged_managed.assert_synchronized()
                if current_managed.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed after semantic preview"
                    )
                if staged_managed.design.content_hash() != receipt["after_hash"]:
                    raise ValidationError(
                        "staged design no longer matches the semantic receipt"
                    )
                semantic_delta = load_json_limited(
                    transaction / "semantic-diff.json", APP_FILE_LIMIT
                )
                if (
                    not isinstance(semantic_delta, Mapping)
                    or semantic_delta.get("schema") != "pcbdraft-semantic-diff"
                    or semantic_delta.get("before_hash") != receipt["before_hash"]
                    or semantic_delta.get("after_hash") != receipt["after_hash"]
                ):
                    raise _PCBOperationPostconditionError(
                        "native_delta_failed",
                        "legacy modification semantic replacement identity is invalid",
                    )
                before_revision = int(current.state["design_revision"])
                after_revision = before_revision + 1
                before_progress, before_stage, before_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        current_managed,
                        before_revision,
                    )
                )
                after_progress, after_stage, after_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        staged_managed,
                        after_revision,
                        validation_root=transaction / "validation",
                        include_routing_failures=False,
                    )
                )
                verified_before = self._require_current_native_consistency(
                    before_consistency,
                    before_revision,
                    label="authoritative source",
                )
                verified_after = self._require_current_native_consistency(
                    after_consistency,
                    after_revision,
                    label="staged candidate",
                )
                atomic_write_json(
                    transaction / "application-native-before.json",
                    verified_before.to_dict(),
                )
                atomic_write_json(
                    transaction / "application-native-after.json",
                    verified_after.to_dict(),
                )
                receipt["version"] = 2
                receipt["baseline_design_revision"] = before_revision
                receipt["candidate_revision"] = after_revision
                receipt.setdefault("artifact", {})
                receipt["artifact"].update(
                    {
                        "application_native_before": "application-native-before.json",
                        "application_native_after": "application-native-after.json",
                    }
                )
                receipt["postconditions"] = [
                    {
                        "name": "source_native_consistency",
                        "passed": verified_before.consistency_passed,
                    },
                    {
                        "name": "candidate_native_consistency",
                        "passed": verified_after.consistency_passed,
                    },
                    {
                        "name": "semantic_replacement_identity",
                        "passed": True,
                    },
                ]
                _attach_progress(
                    receipt,
                    before_progress,
                    after_progress,
                    before_stage,
                    after_stage,
                )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["application_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                receipt["application"] = {
                    "status": "publishing",
                    "error_code": None,
                    "rollback": {
                        "state": "not_required",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                atomic_write_json(receipt_path, receipt)
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                moved_candidate = True
                current.state["status"] = receipt.get("result_status", "validated")
                current.state["active_transaction"] = None
                current.state["last_transaction"] = transaction_id
                current.state["last_validation"] = {
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
                            "source_design_revision",
                        )
                    },
                    "assurance": receipt["validation"].get("assurance", "provisional"),
                    "levels": load_json_limited(
                        transaction / receipt["validation"]["report"], APP_FILE_LIMIT
                    )["levels"],
                }
                current.state["last_release"] = None
                current.state["last_preview"] = None
                current.state["design_revision"] = after_revision
                current.state["revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                text = (
                    "Applied the staged replacement atomically; undo remains available."
                    if receipt.get("schema") == "pcbdraft-agent-repair-transaction"
                    else "Applied the confirmed semantic change atomically; undo remains available."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "change_applied",
                    text,
                    data={"transaction_id": transaction_id},
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(current.state, current.root, "change.applied", text)
                self._write_records(current.root, current.state, current.conversation)
                receipt["status"] = "applied"
                receipt["applied_at"] = utc_timestamp()
                receipt["application"] = {
                    "status": "committed",
                    "error_code": None,
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": False,
                    },
                }
                atomic_write_json(receipt_path, receipt)
                expected_revision = int(current.state["revision"])
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_candidate:
                    try:
                        if staged.exists() or staged.is_symlink():
                            raise ValidationError(
                                "staged rollback destination already exists"
                            )
                        os.replace(current.design_root, staged)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                if moved_before:
                    try:
                        if (
                            current.design_root.exists()
                            or current.design_root.is_symlink()
                        ):
                            raise ValidationError(
                                "live rollback destination already exists"
                            )
                        os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.pop("applied_at", None)
                receipt["status"] = (
                    "rollback_incomplete"
                    if rollback_failures
                    else str(original_receipt.get("status", "ready"))
                )
                receipt["application"] = {
                    "status": "rollback_incomplete" if rollback_failures else "failed",
                    "error_code": _operation_failure_code(
                        exc, stage="publication", tool_name="repair_candidate"
                    ),
                    "failure": _sanitize_secret_text(str(exc))[:2048],
                    "rollback": {
                        "state": (
                            "incomplete"
                            if rollback_failures
                            else "restored"
                            if moved_before or moved_candidate
                            else "not_required"
                        ),
                        "performed": (moved_before or moved_candidate)
                        and not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(before_progress.source_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["application_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                try:
                    atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "candidate application failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )
        result["transaction_progress"] = _transaction_progress_projection(receipt)
        return result

    def verify_release(self, project_id: str) -> dict[str, Any]:
        project = self._open(project_id)
        release = project.state["last_release"]
        if not isinstance(release, dict) or not isinstance(release.get("id"), str):
            raise ValidationError("project has no manufacturing-candidate release")
        root = project.root / "releases" / release["id"]
        result = verify_manufacturing_release(root)
        return result.to_dict()
