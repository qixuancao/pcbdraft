"""Individual native PCB check, render, and manufacturing output workflows.

The host application remains the project-record authority. It installs
late-bound adapters for managed-project access, native output executors,
bounded receipts, locking, timestamps, and diagnostic projection so legacy
``services.application`` patch points remain effective without a reverse
import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError


def _unconfigured(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("application native-output hooks are not configured")


open_managed_project: Callable[..., Any] = _unconfigured
new_run_id: Callable[..., Any] = _unconfigured
run_individual_check: Callable[..., Any] = _unconfigured
load_json_limited: Callable[..., Any] = _unconfigured
atomic_write_json: Callable[..., Any] = _unconfigured
count_severities: Callable[..., Any] = _unconfigured
structured_violations: Callable[..., Any] = _unconfigured
_resource_lock: Callable[..., Any] = _unconfigured
utc_timestamp: Callable[..., Any] = _unconfigured
generate_preview: Callable[..., Any] = _unconfigured
export_manufacturing_output: Callable[..., Any] = _unconfigured
_app_file_limit: Callable[[], int] = _unconfigured
_gate_json_limit: Callable[[], int] = _unconfigured


def _configure_legacy_application_hooks(
    *,
    open_managed_project_hook: Callable[..., Any],
    new_run_id_hook: Callable[..., Any],
    run_individual_check_hook: Callable[..., Any],
    load_json_limited_hook: Callable[..., Any],
    atomic_write_json_hook: Callable[..., Any],
    count_severities_hook: Callable[..., Any],
    structured_violations_hook: Callable[..., Any],
    resource_lock_hook: Callable[..., Any],
    utc_timestamp_hook: Callable[..., Any],
    generate_preview_hook: Callable[..., Any],
    export_manufacturing_output_hook: Callable[..., Any],
    app_file_limit_hook: Callable[[], int],
    gate_json_limit_hook: Callable[[], int],
) -> None:
    """Install late-bound adapters owned by the application module."""

    global open_managed_project
    global new_run_id
    global run_individual_check
    global load_json_limited
    global atomic_write_json
    global count_severities
    global structured_violations
    global _resource_lock
    global utc_timestamp
    global generate_preview
    global export_manufacturing_output
    global _app_file_limit
    global _gate_json_limit

    open_managed_project = open_managed_project_hook
    new_run_id = new_run_id_hook
    run_individual_check = run_individual_check_hook
    load_json_limited = load_json_limited_hook
    atomic_write_json = atomic_write_json_hook
    count_severities = count_severities_hook
    structured_violations = structured_violations_hook
    _resource_lock = resource_lock_hook
    utc_timestamp = utc_timestamp_hook
    generate_preview = generate_preview_hook
    export_manufacturing_output = export_manufacturing_output_hook
    _app_file_limit = app_file_limit_hook
    _gate_json_limit = gate_json_limit_hook


class ApplicationNativeOutputsMixin:
    """Generate one source-bound native check, render, or export at a time."""

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
        check_receipt_path = output / "receipt.json"
        check_receipt = load_json_limited(check_receipt_path, _app_file_limit())
        if (
            not isinstance(check_receipt, dict)
            or check_receipt.get("schema") != "pcbdraft-individual-check-receipt"
            or check_receipt.get("status") != "complete"
        ):
            raise ValidationError("individual PCB check receipt is incomplete")
        # The low-level checker is reusable outside ApplicationService and binds
        # itself to content.  The product boundary additionally binds its
        # evidence to the exact semantic revision before it can advance a stage.
        check_receipt["source_revision"] = expected_revision
        check_receipt["source_design_revision"] = project.state["design_revision"]
        atomic_write_json(check_receipt_path, check_receipt)
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
        report = load_json_limited(result.report_path, _gate_json_limit())
        details = report.get("details", {}) if isinstance(report, dict) else {}
        if isinstance(details, dict):
            violations = details.get("violations")
            issues = details.get("issues")
            if isinstance(violations, list):
                errors, warnings = count_severities(violations)
                diagnostics = {
                    "counts": {
                        "error": errors,
                        "warning": warnings,
                        "total": errors + warnings,
                    },
                    **structured_violations(violations, max_violations=20),
                    "full_details_report": summary["report"],
                }
                shown = diagnostics["violations"]
                total_seen = diagnostics["violation_count_seen"]
                diagnostics["details_truncated"] = diagnostics["violations_truncated"]
                diagnostics["remaining_violation_count"] = max(
                    0, total_seen - len(shown)
                )
                tool_run = report.get("tool_run") if isinstance(report, dict) else None
                raw_report = (
                    tool_run.get("raw_report")
                    if isinstance(tool_run, Mapping)
                    else None
                )
                if (
                    isinstance(raw_report, str)
                    and raw_report
                    and Path(raw_report).name == raw_report
                ):
                    diagnostics["raw_report"] = (
                        (result.report_path.parent / raw_report)
                        .relative_to(project.root)
                        .as_posix()
                    )
                summary["diagnostics"] = diagnostics
            elif isinstance(issues, list):
                summary["diagnostics"] = {
                    "issue_count_seen": len(issues),
                    "issues": issues[:20],
                    "issues_truncated": len(issues) > 20,
                }
        with _resource_lock(project.root, self.locks_root):
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
        with _resource_lock(project.root, self.locks_root):
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
        with _resource_lock(project.root, self.locks_root):
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
