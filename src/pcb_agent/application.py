"""Authoritative project and conversation service shared by terminal and web UIs."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .doctor import doctor_report
from .errors import PcbAgentError, ValidationError
from .io import (
    atomic_write_json,
    load_json_limited,
    make_directory,
    privatize_tree,
)
from .locking import ResourceLock
from .managed import generate_managed_project, open_managed_project
from .operations import semantic_diff
from .previews import generate_previews
from .profiles import (
    build_requirements,
    get_product_profile,
    product_profiles,
    safe_design_id,
)
from .providers import (
    MAX_USER_MESSAGE_BYTES,
    IntentProvider,
    ProviderContext,
    resolve_provider,
)
from .release import build_manufacturing_release, verify_manufacturing_release
from .requirements import RequirementsSpec, compile_requirements
from .runs import new_run_id, utc_timestamp
from .validation import validate_managed_project

APP_PROJECT_SCHEMA = "copperwright-application-project"
APP_PROJECT_VERSION = 1
CONVERSATION_SCHEMA = "copperwright-conversation-record"
CONVERSATION_VERSION = 1
APP_FILE_LIMIT = 4 * 1024 * 1024
MAX_MESSAGES = 2_000
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_TRANSIENT_STATES = {
    "interpreting",
    "generating",
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
    "migration",
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
    configured = os.environ.get("COPPERWRIGHT_HOME")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "share" / "copperwright" / "application"
    )


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


def _sanitize_secret_text(value: str) -> str:
    """Redact obvious credentials before provider use and durable storage."""

    result = value
    patterns = (
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        r"\bsk-[A-Za-z0-9_-]{8,}",
        r"\bgh[opusr]_[A-Za-z0-9]{20,}",
        r"(?i)\b(api[_ -]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
    )
    for pattern in patterns:
        result = re.sub(pattern, "[REDACTED]", result)
    for name, secret in os.environ.items():
        upper = name.upper()
        if (
            len(secret) >= 8
            and any(
                marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )
            and secret in result
        ):
            result = result.replace(secret, "[REDACTED]")
    return result


def sanitize_user_text(value: str) -> str:
    """Public product-layer redaction used before any durable UI/job record."""

    return _sanitize_secret_text(value)


class ApplicationService:
    """Single write authority for product projects and their engineering runtime."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        provider_name: str = "auto",
        provider: IntentProvider | None = None,
    ) -> None:
        raw = Path(workspace).expanduser() if workspace else default_application_home()
        if raw.is_symlink():
            raise ValidationError("application workspace must not be a symlink")
        make_directory(raw)
        self.root = raw.resolve(strict=True)
        if not self.root.is_dir():
            raise ValidationError("application workspace must be a directory")
        self.projects_root = make_directory(self.root / "projects")
        self.locks_root = make_directory(self.root / "locks")
        self.provider = provider or resolve_provider(provider_name)
        self._recover_interrupted_projects()

    def diagnostics(self) -> dict[str, Any]:
        tools = doctor_report()
        kicad_config = Path.home() / ".config" / "kicad" / "10.0"
        library_tables = {
            name: {
                "configured": (kicad_config / name).is_file()
                and not (kicad_config / name).is_symlink(),
                "template_available": (
                    Path("/usr/share/kicad/template") / name
                ).is_file(),
            }
            for name in ("sym-lib-table", "fp-lib-table")
        }
        libraries_ready = all(item["configured"] for item in library_tables.values())
        return {
            "schema": "copperwright-first-run-diagnostics",
            "version": 1,
            "workspace": str(self.root),
            "loopback_default": True,
            "provider": self.provider.diagnostic(),
            "tools": tools["tools"],
            "kicad_library_tables": library_tables,
            "ready_for_generation": tools["ok"] and libraries_ready,
            "profiles": [profile.public_dict() for profile in product_profiles()],
            "credential_guidance": {
                "codex": "Authenticate with the Codex CLI outside CopperWright.",
                "openai_compatible": (
                    "Set COPPERWRIGHT_OPENAI_BASE_URL, COPPERWRIGHT_OPENAI_MODEL, "
                    "and the configured API-key environment variable before launch."
                ),
                "persistence": "Credential values are never written to project records.",
                "kicad": (
                    "Install the KiCad 10 global library tables in ~/.config/kicad/10.0; "
                    "scripts/prepare-kicad-environment.sh does this from a checkout."
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
            except PcbAgentError:
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
            "provider": self.provider.provider_id,
            "revision": 0,
            "design_revision": 0,
            "event_sequence": 0,
            "active_transaction": None,
            "last_transaction": None,
            "last_validation": None,
            "last_preview": None,
            "last_release": None,
            "migration": None,
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

    def open_project(self, project_id: str) -> dict[str, Any]:
        return self._public_project(self._open(project_id))

    def project_root(self, project_id: str) -> Path:
        """Return a validated application-owned root for internal adapters."""

        return self._open(project_id).root

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
            current.state["revision"] += 1
            current.state["updated_at"] = utc_timestamp()
            self._event(
                current.state,
                current.root,
                kind,
                _safe_text(message, "event message", limit=4096),
                level=level,
            )
            self._write_records(current.root, current.state, current.conversation)

    def send_message(
        self,
        project_id: str,
        text: str,
        *,
        timeout: float = 420.0,
    ) -> dict[str, Any]:
        clean = _sanitize_secret_text(
            _safe_text(text, "message", limit=MAX_USER_MESSAGE_BYTES)
        )
        project = self._open(project_id)
        if project.state["status"] in {"generated", "validated", "released"}:
            return self.preview_modification(project_id, clean)
        if project.state["status"] in _TRANSIENT_STATES:
            raise ValidationError("project already has a running operation")
        expected_revision = int(project.state["revision"])
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
            proposal, requirements = self._prepare_proposal(
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

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while the provider was running")
            state = current.state
            conversation = current.conversation
            conversation["proposal"] = proposal
            conversation["decisions"] = proposal.get("decisions", {})
            if requirements is not None:
                atomic_write_json(
                    current.root / "pending-requirements.json",
                    requirements.to_dict(),
                )
            pending = proposal["clarifications"]
            accepted = proposal["scope"]["decision"] == "supported"
            state["status"] = (
                "unsupported"
                if not accepted
                else "needs_clarification"
                if pending
                else "awaiting_confirmation"
            )
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            assistant_text = self._proposal_message(proposal)
            self._append_message(
                conversation,
                "assistant",
                "proposal" if accepted else "unsupported",
                assistant_text,
                data={
                    "status": state["status"],
                    "clarification_count": len(pending),
                },
            )
            self._event(
                state,
                current.root,
                "proposal.ready" if accepted else "scope.rejected",
                assistant_text,
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool = True,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        project = self._open(project_id)
        if project.state["status"] not in {
            "awaiting_confirmation",
            "generation_failed",
            "interrupted",
            "generated",
        }:
            raise ValidationError("project is not awaiting generation confirmation")
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            open_managed_project(project.design_root).assert_synchronized()
            self.generate_project_previews(project_id)
            return self.validate_project(project_id, timeout=timeout)
        pending_path = project.root / "pending-requirements.json"
        spec = RequirementsSpec.from_dict(
            load_json_limited(pending_path, APP_FILE_LIMIT)
        )
        expected_revision = project.state["revision"]
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
        try:
            generated = generate_managed_project(spec, project.design_root)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "generation_failed",
                "generation.failed",
                str(exc),
            )
            raise
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
                "The confirmed semantic design was generated as a native KiCad project.",
                data={"design_content_hash": generated.project.design.content_hash()},
            )
            self._event(
                state,
                current.root,
                "generation.complete",
                "Native KiCad project generated",
            )
            self._write_records(current.root, state, conversation)
        self.generate_project_previews(project_id)
        if validate:
            return self.validate_project(project_id, timeout=timeout)
        return self.open_project(project_id)

    def import_managed_project(
        self,
        name: str,
        source: str | Path,
    ) -> dict[str, Any]:
        """Wrap a synchronized 0.2 managed runtime project without changing it."""

        managed = open_managed_project(source)
        managed.assert_synchronized()
        created = self.create_draft(name)
        project_id = created["project"]["id"]
        project = self._open(project_id)
        if project.design_root.exists():
            raise ValidationError("import target unexpectedly contains a design")
        temporary = project.root / ".design-importing"
        make_directory(temporary)
        try:
            for relative in managed.manifest["files"].values():
                source_path = managed.root / relative
                shutil.copy2(source_path, temporary / source_path.name)
            privatize_tree(temporary)
            open_managed_project(temporary).assert_synchronized()
            os.replace(temporary, project.design_root)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            state = current.state
            conversation = current.conversation
            state["status"] = "generated"
            state["design_revision"] = 1
            state["migration"] = {
                "from": "CopperWright managed project 0.2.x",
                "source_manifest_schema": managed.manifest["schema"],
                "source_content_hash": managed.design.content_hash(),
                "imported_at": utc_timestamp(),
            }
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._append_message(
                conversation,
                "assistant",
                "migration",
                "Imported the synchronized managed project into the application record.",
            )
            self._event(
                state, current.root, "migration.complete", "Managed project imported"
            )
            self._write_records(current.root, state, conversation)
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
    ) -> tuple[dict[str, Any], RequirementsSpec | None]:
        merged = dict(value)
        if (
            prior.get("proposed_profile")
            in {profile.id for profile in product_profiles()}
            and value["proposed_profile"] == "unsupported"
            # A terse response to a previously asked layer question is not new scope.
            and re.fullmatch(
                r"\s*(?:use\s+)?[24]\s*(?:[- ]?layers?)?\s*",
                request,
                re.IGNORECASE,
            )
        ):
            merged["proposed_profile"] = prior["proposed_profile"]
            merged["unsupported_reasons"] = []
        for field in ("layers",):
            if merged.get(field) is None and prior.get(field) is not None:
                merged[field] = prior[field]
        old_board = prior.get("board") if isinstance(prior.get("board"), dict) else {}
        merged["board"] = {
            key: merged["board"].get(key)
            if merged["board"].get(key) is not None
            else old_board.get(key)
            for key in ("width_mm", "height_mm")
        }
        profile_id = merged["proposed_profile"]
        if profile_id == "unsupported":
            return (
                {
                    **merged,
                    "scope": {
                        "decision": "unsupported",
                        "reasons": merged["unsupported_reasons"],
                        "external_gates": [],
                    },
                    "clarifications": [],
                    "brief": None,
                    "decisions": {},
                },
                None,
            )
        layers = merged.get("layers")
        clarifications: list[dict[str, Any]] = []
        if layers not in {2, 4}:
            clarifications.append(
                {
                    "id": "layers",
                    "question": "Should this board use 2 or 4 copper layers?",
                    "choices": ["2 layers", "4 layers"],
                    "required": True,
                }
            )
        width = merged["board"].get("width_mm") or 45.0
        height = merged["board"].get("height_mm") or 30.0
        merged["board"] = {"width_mm": float(width), "height_mm": float(height)}
        decisions = {
            "proposed_profile": profile_id,
            "design_name": merged["design_name"],
            "layers": layers,
            "board": merged["board"],
            "power_source": {
                "low_voltage_i2c_controller_v1": "externally_regulated_3v3",
                "low_voltage_spi_environment_v1": "externally_regulated_3v3",
                "low_voltage_uart_ldo_controller_v1": "externally_regulated_5v_to_onboard_ldo",
            }[profile_id],
            "risk_class": "prototype",
        }
        proposal: dict[str, Any] = {
            **merged,
            "scope": {
                "decision": "supported",
                "reasons": ["Request maps to a locally verified low-voltage profile."],
                "external_gates": [
                    "human engineering review",
                    "physical build and test",
                    "production sign-off",
                ],
            },
            "clarifications": clarifications,
            "brief": None,
            "decisions": decisions,
        }
        if clarifications:
            return proposal, None
        source_date = created_at[:10]
        requirements = build_requirements(
            profile_id,
            design_name=merged["design_name"],
            design_id=safe_design_id(merged["design_name"]),
            layers=int(layers),
            width_mm=float(width),
            height_mm=float(height),
            source_locator=f"application/projects/{project_id}/conversation.json",
            source_date=source_date,
        )
        design = compile_requirements(requirements)
        counts: dict[tuple[str, str], int] = {}
        references: dict[tuple[str, str], list[str]] = {}
        for component in design.components:
            key = (component.part_id, component.value)
            counts[key] = counts.get(key, 0) + 1
            references.setdefault(key, []).append(component.reference)
        proposal["brief"] = {
            "purpose": merged["request_summary"],
            "architecture": [
                {"id": block.id, "kind": block.kind, "name": block.name}
                for block in design.blocks
            ],
            "assumptions": merged["assumptions"],
            "power": requirements.power,
            "interfaces": list(requirements.interfaces),
            "board": requirements.board.to_dict(),
            "bom": [
                {
                    "part_id": key[0],
                    "value": key[1],
                    "quantity": counts[key],
                    "references": sorted(references[key]),
                }
                for key in sorted(counts)
            ],
            "constraints": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "severity": item.severity,
                    "rationale": item.rationale,
                }
                for item in design.constraints
            ],
            "semantic_content_hash": design.content_hash(),
            "confirmation_required": True,
        }
        return proposal, requirements

    @staticmethod
    def _proposal_message(proposal: dict[str, Any]) -> str:
        if proposal["scope"]["decision"] != "supported":
            return "Unsupported scope: " + "; ".join(proposal["scope"]["reasons"])
        if proposal["clarifications"]:
            return proposal["clarifications"][0]["question"]
        return (
            "The design brief, assumptions, BOM, interfaces, and constraints are ready. "
            "Confirm explicitly before CopperWright creates KiCad files."
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
            except PcbAgentError:
                continue
            if project.state["status"] not in _TRANSIENT_STATES:
                continue
            try:
                with ResourceLock(candidate, self.locks_root, timeout=0):
                    current = self._open_path(candidate)
                    if current.state["status"] in _TRANSIENT_STATES:
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
                            except PcbAgentError:
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
            except PcbAgentError:
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
                "schema": "copperwright-structured-event",
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
                "validation": receipt.get("validation"),
            }
        return {
            "schema": "copperwright-application-view",
            "version": 1,
            "project": self._summary(project),
            "state": project.state,
            "conversation": project.conversation,
            "design": design,
            "artifacts": {
                "previews": project.state["last_preview"],
                "validation": project.state["last_validation"],
                "release": project.state["last_release"],
            },
            "active_change": active_change,
            "events": self.events(
                project.state["id"], after=max(0, project.state["event_sequence"] - 50)
            ),
        }

    def validate_project(
        self, project_id: str, *, timeout: float = 90.0
    ) -> dict[str, Any]:
        """Run the real layered runtime validation and attach its evidence."""

        project = self._open(project_id)
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
        expected_revision = project.state["revision"]
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
                state, current.root, "validation.started", "Running L0-L7 gates"
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
            "production_ready": result.production_ready,
            "production_claimed": False,
            "levels": report["levels"],
        }
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while validation was running")
            state = current.state
            conversation = current.conversation
            state["last_validation"] = summary
            state["status"] = (
                "validated" if result.candidate_ready else "validation_failed"
            )
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            text = (
                "Engineering-candidate validation passed; human review and physical "
                "L7 evidence remain external gates."
                if result.candidate_ready
                else "Validation found release-blocking issues; inspect the L0-L7 results."
            )
            self._append_message(
                conversation,
                "assistant",
                "validation",
                text,
                data={
                    "candidate_ready": result.candidate_ready,
                    "production_ready": result.production_ready,
                },
            )
            self._event(
                state,
                current.root,
                "validation.complete",
                text,
                level="info" if result.candidate_ready else "error",
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def generate_project_previews(
        self, project_id: str, *, timeout: float = 90.0
    ) -> dict[str, Any]:
        """Generate browser-safe links to real KiCad exports and a 3D render."""

        project = self._open(project_id)
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

    def preview_modification(self, project_id: str, request: str) -> dict[str, Any]:
        """Compile and validate a semantic change in staging without applying it."""

        clean = _sanitize_secret_text(
            _safe_text(request, "modification request", limit=MAX_USER_MESSAGE_BYTES)
        )
        project = self._open(project_id)
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "released",
        }:
            raise ValidationError("project must be generated before it can be modified")
        if project.state["active_transaction"] is not None:
            raise ValidationError("confirm or discard the active semantic change first")
        managed = open_managed_project(project.design_root)
        managed.assert_synchronized()
        current_spec = RequirementsSpec.from_dict(
            load_json_limited(managed.requirements_path, APP_FILE_LIMIT)
        )
        dimensions = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×]\s*"
            r"(\d+(?:\.\d+)?)\s*mm\b",
            clean.casefold(),
        )
        layer_match = re.search(r"\b([24])\s*[- ]?layers?\b", clean.casefold())
        width = (
            float(dimensions.group(1)) if dimensions else current_spec.board.width_mm
        )
        height = (
            float(dimensions.group(2)) if dimensions else current_spec.board.height_mm
        )
        layers = int(layer_match.group(1)) if layer_match else current_spec.board.layers
        if not dimensions and not layer_match:
            raise ValidationError(
                "this verified profile currently supports conversational board-envelope "
                "and 2/4-layer changes; no supported semantic change was found"
            )
        profile_id = str(managed.design.metadata.get("profile", ""))
        if profile_id == "attiny402_tmp102_controller_v1":
            # Migration compatibility for managed projects generated before the
            # end-user profile identifier became authoritative.
            profile_id = "low_voltage_i2c_controller_v1"
        get_product_profile(profile_id)
        new_spec = build_requirements(
            profile_id,
            design_name=current_spec.name,
            design_id=current_spec.design_id,
            layers=layers,
            width_mm=width,
            height_mm=height,
            source_locator=f"application/projects/{project_id}/conversation.json",
            source_date=project.state["created_at"][:10],
        )
        new_design = compile_requirements(new_spec)
        diff = semantic_diff(managed.design, new_design)
        if (
            diff["summary"]["objects_added"] == 0
            and diff["summary"]["objects_removed"] == 0
            and diff["summary"]["objects_modified"] == 0
            and not diff["board_fields"]
            and not diff["metadata_fields"]
        ):
            raise ValidationError(
                "the requested values already match the authoritative design"
            )
        transaction_id = new_run_id()
        transaction = project.root / "transactions" / transaction_id
        make_directory(transaction)
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "copperwright-project-transaction",
            "version": 1,
            "id": transaction_id,
            "status": "preparing",
            "created_at": utc_timestamp(),
            "request": clean,
            "before_hash": managed.design.content_hash(),
            "after_hash": new_design.content_hash(),
            "before_design_revision": project.state["design_revision"],
            "prior_status": project.state["status"],
            "prior_validation": project.state["last_validation"],
            "prior_preview": project.state["last_preview"],
            "prior_release": project.state["last_release"],
            "diff": "semantic-diff.json",
            "staged": "staged",
            "validation": None,
            "applied_at": None,
            "undone_at": None,
        }
        atomic_write_json(receipt_path, receipt)
        atomic_write_json(transaction / "semantic-diff.json", diff)
        expected_revision = project.state["revision"]
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before semantic preview")
            state = current.state
            conversation = current.conversation
            state["status"] = "applying_change"
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            self._append_message(conversation, "user", "modification", clean)
            self._event(
                state,
                current.root,
                "change.preview.started",
                "Compiling and validating semantic change in staging",
            )
            self._write_records(current.root, state, conversation)
            expected_revision = state["revision"]
        try:
            staged = generate_managed_project(new_spec, transaction / "staged")
            validation = validate_managed_project(
                staged.project,
                output=transaction / "validation",
                timeout=120.0,
            )
            if not validation.candidate_ready:
                raise ValidationError(
                    "staged semantic change did not pass candidate gates"
                )
            receipt["status"] = "ready"
            receipt["validation"] = {
                "report": validation.report_path.relative_to(transaction).as_posix(),
                "report_sha256": validation.report_sha256,
                "candidate_ready": validation.candidate_ready,
                "production_ready": validation.production_ready,
                "production_claimed": False,
            }
            atomic_write_json(receipt_path, receipt)
        except BaseException as exc:
            receipt["status"] = "rejected"
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            atomic_write_json(receipt_path, receipt)
            self._record_failure(
                project_id,
                expected_revision,
                project.state["status"],
                "change.preview.failed",
                str(exc),
            )
            raise
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError(
                    "project changed while semantic preview was running"
                )
            state = current.state
            conversation = current.conversation
            state["status"] = "change_ready"
            state["active_transaction"] = transaction_id
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            text = (
                "A staged semantic diff passed deterministic validation. Confirm explicitly "
                "to apply it, or discard it; the current KiCad project is unchanged."
            )
            self._append_message(
                conversation,
                "assistant",
                "change_preview",
                text,
                data={"transaction_id": transaction_id, "diff": diff["summary"]},
            )
            self._event(state, current.root, "change.preview.ready", text)
            self._write_records(current.root, state, conversation)
        view = self.open_project(project_id)
        view["active_change"] = {
            "transaction_id": transaction_id,
            "request": clean,
            "diff": diff,
            "validation": receipt["validation"],
        }
        return view

    def apply_modification(self, project_id: str) -> dict[str, Any]:
        """Atomically publish the currently staged, confirmed semantic change."""

        project = self._open(project_id)
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
            state["status"] = "validated"
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
                        "production_ready",
                        "production_claimed",
                    )
                },
                "levels": load_json_limited(
                    transaction / receipt["validation"]["report"], APP_FILE_LIMIT
                )["levels"],
            }
            state["last_release"] = None
            state["last_preview"] = None
            state["design_revision"] += 1
            state["revision"] += 1
            state["updated_at"] = utc_timestamp()
            text = "Applied the confirmed semantic change atomically; undo remains available."
            self._append_message(
                conversation,
                "assistant",
                "change_applied",
                text,
                data={"transaction_id": transaction_id},
            )
            self._event(state, current.root, "change.applied", text)
            self._write_records(current.root, state, conversation)
        self.generate_project_previews(project_id)
        return self.open_project(project_id)

    def discard_modification(self, project_id: str) -> dict[str, Any]:
        project = self._open(project_id)
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

    def undo_last_modification(self, project_id: str) -> dict[str, Any]:
        project = self._open(project_id)
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
        self, project_id: str, *, timeout: float = 180.0
    ) -> dict[str, Any]:
        project = self._open(project_id)
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
        expected_revision = project.state["revision"]
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
