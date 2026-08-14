"""Review, patch, and apply workflow orchestration."""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from . import PRODUCT_NAME, __version__
from .doctor import probe_executable
from .errors import PCBDraftError, TransactionRejected, ValidationError
from .gates import collect_structured_evidence, gate_dict, gates_are_runnable, run_gates
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    load_json_limited,
    make_directory,
    read_bytes_limited,
)
from .model_review import invoke_model, patch_prompt, review_prompt
from .patching import (
    MAX_TARGET_BYTES,
    apply_operations,
    build_unified_patch,
    regression_reasons,
)
from .project import (
    baseline_manifest,
    canonical_project,
    copy_project,
    discover_project,
    manifest_hashes,
    manifests_match,
    resolve_member,
    review_inventory,
    sha256_file,
    validate_agent_tree,
    validate_baseline_document,
)
from .report import render_review_markdown
from .runs import create_run, ensure_output_outside_project, utc_timestamp
from .semantic import collect_semantic_context

RECEIPT_LIMIT = 32 * 1024 * 1024
REQUEST_MAX_BYTES = 32 * 1024
PROMPT_INVENTORY_ENTRIES = 200


def _receipt_base(*, run_id: str, kind: str, project: Path) -> dict[str, Any]:
    return {
        "receipt_version": 1,
        "runtime": {"name": PRODUCT_NAME, "version": __version__},
        "run_id": run_id,
        "kind": kind,
        "project": str(project),
        "created_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
        "status": "running",
    }


def _tool_versions(
    *,
    kicad_executable: str | None,
) -> dict[str, Any]:
    kicad = kicad_executable or shutil.which("kicad-cli")
    return {"kicad-cli": probe_executable(kicad, ["--version"])}


def _write_receipt(run_dir: Path, receipt: dict[str, Any]) -> None:
    receipt["updated_at"] = utc_timestamp()
    atomic_write_json(run_dir / "receipt.json", receipt)


def _unavailable_tools(versions: Mapping[str, Any]) -> list[str]:
    return [name for name, detail in versions.items() if not detail.get("available")]


def _prompt_inventory(inventory: Mapping[str, Any]) -> dict[str, Any]:
    entries = inventory.get("entries", [])
    bounded_entries = (
        entries[:PROMPT_INVENTORY_ENTRIES] if isinstance(entries, list) else []
    )
    return {
        "entries": bounded_entries,
        "entry_count": inventory.get("entry_count"),
        "entries_in_prompt": len(bounded_entries),
        "prompt_entries_truncated": isinstance(entries, list)
        and len(entries) > len(bounded_entries),
        "total_hashed_bytes": inventory.get("total_hashed_bytes"),
        "complete": inventory.get("complete"),
        "omitted": inventory.get("omitted"),
        "limits": inventory.get("limits"),
    }


def _existing_artifacts(run_dir: Path, candidates: list[str]) -> list[str]:
    present: list[str] = []
    for candidate in candidates:
        normalized = candidate.rstrip("/")
        pure = PurePosixPath(normalized)
        if not normalized or pure.is_absolute() or ".." in pure.parts:
            continue
        path = run_dir.joinpath(*pure.parts)
        if path.exists() and not path.is_symlink():
            present.append(candidate)
    return sorted(set(present))


def _record_apply_artifacts(run_dir: Path, receipt: dict[str, Any]) -> None:
    existing = list(receipt.get("artifacts", []))
    receipt["artifacts"] = _existing_artifacts(
        run_dir,
        existing + ["backup/", "apply-gates/erc.json", "apply-gates/drc.json"],
    )


def _capture_model_failure(run_dir: Path, receipt: dict[str, Any]) -> None:
    if "model" in receipt:
        return
    for filename in ("model-review.receipt.json", "model-patch.receipt.json"):
        artifact = run_dir / filename
        if not artifact.exists():
            continue
        try:
            receipt["model"] = load_json_limited(artifact, RECEIPT_LIMIT)
        except PCBDraftError:
            receipt["model"] = {
                "completed": False,
                "schema_valid": False,
                "artifact_unreadable": True,
            }
        return


def _gate_failure_message(results: Mapping[str, Any]) -> str:
    failed = [name for name, result in results.items() if result.tool_status != "ok"]
    missing = [name for name in ("erc", "drc") if name not in results]
    return ", ".join(failed + missing) or "unknown gate"


def run_review(
    project_value: str,
    *,
    output_parent: str | None,
    timeout: float,
    kicad_executable: str | None = None,
) -> Path:
    project = canonical_project(project_value)
    validate_agent_tree(project)
    files = discover_project(project)
    ensure_output_outside_project(project, output_parent)
    run_id, run_dir = create_run(output_parent)
    deadline = time.monotonic() + timeout
    redactions = {str(project): "<PROJECT>", str(run_dir): "<RUN_DIR>"}
    receipt = _receipt_base(run_id=run_id, kind="review", project=project)
    _write_receipt(run_dir, receipt)

    try:
        receipt["tool_versions"] = _tool_versions(
            kicad_executable=kicad_executable,
        )
        receipt["selected_input_hashes"] = {
            name: sha256_file(path)
            for name, path in (("schematic", files.schematic), ("board", files.board))
        }
        receipt["selected_files"] = files.relative(project)
        _write_receipt(run_dir, receipt)
        unavailable = _unavailable_tools(receipt["tool_versions"])
        if unavailable:
            raise PCBDraftError(
                f"required tool version check failed: {', '.join(unavailable)}"
            )
        inventory = review_inventory(project)
        receipt["input_hashes"] = manifest_hashes(inventory)
        atomic_write_json(run_dir / "inventory.json", inventory)
        _write_receipt(run_dir, receipt)
        gates = run_gates(
            files=files,
            output_dir=run_dir / "gates",
            deadline=deadline,
            redactions=redactions,
            executable=kicad_executable,
        )
        evidence_gates = gate_dict(gates, prefix="gates")
        violation_evidence = collect_structured_evidence(
            output_dir=run_dir / "gates", results=gates
        )
        evidence = {
            "kind": "deterministic_kicad_evidence",
            "selected_files": files.relative(project),
            "inventory": {
                key: value for key, value in inventory.items() if key != "root"
            },
            "gates": evidence_gates,
            "violations": violation_evidence,
        }
        atomic_write_json(run_dir / "evidence.json", evidence)
        receipt["gates"] = evidence_gates
        if not gates_are_runnable(gates):
            receipt["status"] = "failed"
            receipt["failure"] = (
                f"deterministic gate tool failed: {_gate_failure_message(gates)}"
            )
            _write_receipt(run_dir, receipt)
            raise PCBDraftError(f"review gate tool failed; evidence kept in {run_dir}")

        semantic_context = collect_semantic_context(
            files=files,
            project_root=project,
            output_dir=run_dir / "semantic",
            deadline=deadline,
            redactions=redactions,
            executable=kicad_executable,
        )
        atomic_write_json(run_dir / "semantic-context.json", semantic_context)
        receipt["semantic_exports"] = semantic_context["exports"]
        _write_receipt(run_dir, receipt)
        unavailable_semantic = [
            name
            for name, status in semantic_context["exports"].items()
            if status.get("available") is not True
        ]
        if unavailable_semantic:
            raise PCBDraftError(
                f"semantic KiCad export failed: {', '.join(sorted(unavailable_semantic))}"
            )

        model = invoke_model(
            mode="review",
            run_dir=run_dir,
            prompt=review_prompt(
                files=files.relative(project),
                inventory=_prompt_inventory(inventory),
                gates={"summary": evidence_gates, "violations": violation_evidence},
                semantic_context=semantic_context,
            ),
            timeout=min(1800.0, max(1.0, deadline - time.monotonic())),
        )
        receipt["model"] = model.receipt
        report = {
            "report_version": 1,
            "run_id": run_id,
            "project": str(project),
            "selected_files": files.relative(project),
            "evidence_classes": {
                "deterministic": "Local kicad-cli ERC/DRC evidence",
                "ai_heuristic": "Model interpretation; not an engineering sign-off",
            },
            "deterministic_evidence": {
                "gates": evidence_gates,
                "violations": violation_evidence,
            },
            "ai_heuristic": model.value,
        }
        atomic_write_json(run_dir / "report.json", report)
        atomic_write_text(
            run_dir / "report.md",
            render_review_markdown(
                run_id=run_id,
                project=str(project),
                selected_files=files.relative(project),
                gates=evidence_gates,
                review=model.value,
                violations=violation_evidence,
            ),
        )
        receipt["status"] = "complete"
        receipt["artifacts"] = _existing_artifacts(
            run_dir,
            [
                "gates/erc.json",
                "gates/drc.json",
                "inventory.json",
                "evidence.json",
                "semantic/schematic.netlist.xml",
                "semantic/board-stats.json",
                "semantic/board-netlist.d356",
                "semantic-context.json",
                "model-review.schema.json",
                "model-review.final.json",
                "model-review.receipt.json",
                "report.json",
                "report.md",
                "receipt.json",
            ],
        )
        _write_receipt(run_dir, receipt)
        return run_dir
    except Exception as exc:
        if receipt.get("status") == "running":
            _capture_model_failure(run_dir, receipt)
            receipt["status"] = "failed"
            receipt["failure"] = str(exc)
            _write_receipt(run_dir, receipt)
        raise


def run_patch(
    project_value: str,
    *,
    request: str,
    output_parent: str | None,
    timeout: float,
    kicad_executable: str | None = None,
) -> Path:
    if not isinstance(request, str) or not request.strip():
        raise ValidationError("--request must be non-empty")
    if len(request.encode("utf-8")) > REQUEST_MAX_BYTES:
        raise ValidationError(f"--request exceeds {REQUEST_MAX_BYTES} bytes")
    project = canonical_project(project_value)
    validate_agent_tree(project)
    discover_project(project)
    ensure_output_outside_project(project, output_parent)
    run_id, run_dir = create_run(output_parent)
    deadline = time.monotonic() + timeout
    staging = run_dir / "staging"
    redactions = {
        str(project): "<PROJECT>",
        str(staging): "<STAGING>",
        str(run_dir): "<RUN_DIR>",
    }
    receipt = _receipt_base(run_id=run_id, kind="patch", project=project)
    receipt["request"] = {
        "bytes": len(request.encode("utf-8")),
        "sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "stored_verbatim": False,
    }
    _write_receipt(run_dir, receipt)

    try:
        receipt["tool_versions"] = _tool_versions(
            kicad_executable=kicad_executable,
        )
        _write_receipt(run_dir, receipt)
        unavailable = _unavailable_tools(receipt["tool_versions"])
        if unavailable:
            raise PCBDraftError(
                f"required tool version check failed: {', '.join(unavailable)}"
            )
        baseline = baseline_manifest(project)
        receipt["baseline_manifest"] = "baseline.json"
        receipt["input_hashes"] = manifest_hashes(baseline)
        atomic_write_json(run_dir / "baseline.json", baseline)
        _write_receipt(run_dir, receipt)
        copy_project(project, staging)
        copied = baseline_manifest(staging)
        if not manifests_match(baseline, copied):
            raise ValidationError(
                "source project changed while the staging snapshot was being copied"
            )
        staging_files = discover_project(staging)
        inventory = review_inventory(staging)
        atomic_write_json(run_dir / "inventory.json", inventory)

        baseline_gates = run_gates(
            files=staging_files,
            output_dir=run_dir / "baseline-gates",
            deadline=deadline,
            redactions=redactions,
            executable=kicad_executable,
        )
        baseline_gate_data = gate_dict(baseline_gates, prefix="baseline-gates")
        baseline_violation_evidence = collect_structured_evidence(
            output_dir=run_dir / "baseline-gates", results=baseline_gates
        )
        receipt["baseline_gates"] = baseline_gate_data
        if not gates_are_runnable(baseline_gates):
            receipt["status"] = "failed"
            receipt["failure"] = (
                f"baseline gate tool failed: {_gate_failure_message(baseline_gates)}"
            )
            _write_receipt(run_dir, receipt)
            raise PCBDraftError(
                f"baseline gate tool failed; transaction kept in {run_dir}"
            )

        model = invoke_model(
            mode="patch",
            run_dir=run_dir,
            prompt=patch_prompt(
                request=request,
                files=staging_files.relative(staging),
                inventory=_prompt_inventory(inventory),
                gates={
                    "summary": baseline_gate_data,
                    "violations": baseline_violation_evidence,
                },
            ),
            timeout=min(1800.0, max(1.0, deadline - time.monotonic())),
        )
        receipt["model"] = model.receipt

        applied = apply_operations(staging, model.value["operations"])
        changed_paths = sorted({operation.relative_path for operation in applied})
        staged_manifest = baseline_manifest(staging)
        baseline_paths = set(manifest_hashes(baseline))
        staged_paths = set(manifest_hashes(staged_manifest))
        if baseline_paths != staged_paths:
            raise ValidationError(
                "change set attempted to create, remove, or rename project members"
            )
        actual_changed = sorted(
            path
            for path in baseline_paths
            if manifest_hashes(baseline)[path] != manifest_hashes(staged_manifest)[path]
        )
        if actual_changed != changed_paths:
            raise ValidationError(
                "staging changes do not exactly match the declared operation paths"
            )

        after_files = discover_project(staging)
        after_gates = run_gates(
            files=after_files,
            output_dir=run_dir / "after-gates",
            deadline=deadline,
            redactions=redactions,
            executable=kicad_executable,
        )
        after_gate_data = gate_dict(after_gates, prefix="after-gates")
        after_violation_evidence = collect_structured_evidence(
            output_dir=run_dir / "after-gates", results=after_gates
        )
        atomic_write_json(
            run_dir / "evidence.json",
            {
                "kind": "deterministic_kicad_transaction_evidence",
                "baseline": {
                    "gates": baseline_gate_data,
                    "violations": baseline_violation_evidence,
                },
                "after": {
                    "gates": after_gate_data,
                    "violations": after_violation_evidence,
                },
            },
        )
        reasons = regression_reasons(baseline_gates, after_gates)
        status = "rejected" if reasons else "ready"
        change_set = {
            "change_set_version": 1,
            "summary": model.value["summary"],
            "operations": model.value["operations"],
            "application": [operation.to_dict() for operation in applied],
            "changed_paths": changed_paths,
            "unsupported_checks": model.value["unsupported_checks"],
            "status": status,
            "rejection_reasons": reasons,
        }
        atomic_write_json(run_dir / "change_set.json", change_set)
        atomic_write_text(
            run_dir / "changes.patch",
            build_unified_patch(
                original=project, staging=staging, relative_paths=changed_paths
            ),
        )
        receipt["after_gates"] = after_gate_data
        receipt["status"] = status
        receipt["changed_paths"] = changed_paths
        receipt["staged_hashes"] = {
            path: manifest_hashes(staged_manifest)[path] for path in changed_paths
        }
        receipt["rejection_reasons"] = reasons
        receipt["artifacts"] = _existing_artifacts(
            run_dir,
            [
                "baseline.json",
                "baseline-gates/erc.json",
                "baseline-gates/drc.json",
                "after-gates/erc.json",
                "after-gates/drc.json",
                "inventory.json",
                "model-patch.schema.json",
                "model-patch.final.json",
                "model-patch.receipt.json",
                "evidence.json",
                "change_set.json",
                "changes.patch",
                "receipt.json",
                "staging/",
            ],
        )
        _write_receipt(run_dir, receipt)
        if reasons:
            raise TransactionRejected(
                f"patch transaction rejected; see {run_dir}", str(run_dir)
            )
        return run_dir
    except TransactionRejected:
        raise
    except Exception as exc:
        if receipt.get("status") == "running":
            _capture_model_failure(run_dir, receipt)
            receipt["status"] = "failed"
            receipt["failure"] = str(exc)
            _write_receipt(run_dir, receipt)
        raise


def canonical_run_dir(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"run directory does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValidationError(f"run path is not a directory: {resolved}")
    return resolved


def _rollback(
    *,
    project: Path,
    backup: Path,
    changed_paths: list[str],
) -> None:
    errors: list[str] = []
    for relative_path in changed_paths:
        try:
            target = resolve_member(project, relative_path, must_exist=True)
            source = resolve_member(backup, relative_path, must_exist=True)
            mode = target.stat().st_mode & 0o777
            atomic_write_bytes(
                target, read_bytes_limited(source, MAX_TARGET_BYTES), mode=mode
            )
        except (PCBDraftError, OSError) as exc:
            errors.append(str(exc))
    if errors:
        raise PCBDraftError(
            "automatic rollback failed; restore from the run backup manually"
        )


def run_apply(
    run_value: str,
    *,
    kicad_executable: str | None = None,
    timeout: float = 300.0,
) -> Path:
    run_dir = canonical_run_dir(run_value)
    receipt_path = run_dir / "receipt.json"
    receipt = load_json_limited(receipt_path, RECEIPT_LIMIT)
    if not isinstance(receipt, dict) or receipt.get("kind") != "patch":
        raise ValidationError("run directory is not a PCBDraft patch transaction")
    runtime = receipt.get("runtime")
    if (
        receipt.get("receipt_version") != 1
        or not isinstance(runtime, dict)
        or runtime.get("name") != PRODUCT_NAME
    ):
        raise ValidationError("unsupported or malformed transaction receipt")
    if receipt.get("status") != "ready":
        raise ValidationError("apply accepts only a ready transaction")
    project_value = receipt.get("project")
    changed_paths = receipt.get("changed_paths")
    if (
        not isinstance(project_value, str)
        or not isinstance(changed_paths, list)
        or not all(isinstance(path, str) for path in changed_paths)
    ):
        raise ValidationError("transaction receipt is incomplete")
    if changed_paths != sorted(set(changed_paths)):
        raise ValidationError("transaction changed_paths must be unique and sorted")
    change_set = load_json_limited(run_dir / "change_set.json", RECEIPT_LIMIT)
    if (
        not isinstance(change_set, dict)
        or change_set.get("status") != "ready"
        or change_set.get("changed_paths") != changed_paths
    ):
        raise ValidationError("transaction change_set does not match the ready receipt")
    operations = change_set.get("operations")
    if not isinstance(operations, list) or not all(
        isinstance(operation, dict) and isinstance(operation.get("relative_path"), str)
        for operation in operations
    ):
        raise ValidationError("transaction operation paths do not match changed_paths")
    operation_paths = {operation["relative_path"] for operation in operations}
    if operation_paths != set(changed_paths):
        raise ValidationError("transaction operation paths do not match changed_paths")
    existing_artifacts = receipt.get("artifacts")
    if not isinstance(existing_artifacts, list) or not all(
        isinstance(item, str) for item in existing_artifacts
    ):
        raise ValidationError("transaction artifact list is malformed")
    model_receipt = receipt.get("model")
    if (
        not isinstance(model_receipt, dict)
        or model_receipt.get("completed") is not True
        or model_receipt.get("schema_valid") is not True
    ):
        raise ValidationError(
            "transaction lacks a completed schema-valid Model change set"
        )
    baseline_gate_data = receipt.get("baseline_gates")
    after_gate_data = receipt.get("after_gates")
    if not isinstance(baseline_gate_data, dict) or not isinstance(
        after_gate_data, dict
    ):
        raise ValidationError("transaction gate evidence is missing")
    if regression_reasons(baseline_gate_data, after_gate_data):
        raise ValidationError(
            "transaction receipt is marked ready despite a gate regression"
        )
    project = canonical_project(project_value)
    receipt["apply_tool_versions"] = _tool_versions(
        kicad_executable=kicad_executable,
    )
    unavailable = _unavailable_tools(receipt["apply_tool_versions"])
    if unavailable:
        receipt["last_apply_failure"] = (
            f"required tool version check failed: {', '.join(unavailable)}"
        )
        _write_receipt(run_dir, receipt)
        raise PCBDraftError(receipt["last_apply_failure"])
    staging_entry = run_dir / "staging"
    if staging_entry.is_symlink():
        raise ValidationError("transaction staging directory must not be a symlink")
    try:
        staging = staging_entry.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("transaction staging directory is missing") from exc
    if not staging.is_dir() or not staging.is_relative_to(run_dir):
        raise ValidationError("transaction staging directory is missing or unsafe")
    baseline = load_json_limited(run_dir / "baseline.json", RECEIPT_LIMIT)
    baseline_hashes = validate_baseline_document(baseline, expected_root=project)
    if receipt.get("input_hashes") != baseline_hashes:
        raise ValidationError(
            "transaction receipt input hashes do not match its baseline"
        )
    current = baseline_manifest(project)
    if not manifests_match(baseline, current):
        raise ValidationError(
            "source project drifted from the transaction baseline; refusing apply"
        )

    expected_staged_hashes = receipt.get("staged_hashes")
    if not isinstance(expected_staged_hashes, dict) or set(
        expected_staged_hashes
    ) != set(changed_paths):
        raise ValidationError("transaction staged hashes are missing")
    for relative_path in changed_paths:
        if relative_path not in baseline_hashes:
            raise ValidationError(
                f"changed path is absent from the baseline: {relative_path}"
            )
        staged_path = resolve_member(staging, relative_path, must_exist=True)
        staged_hash = sha256_file(staged_path, max_bytes=MAX_TARGET_BYTES)
        if staged_hash != expected_staged_hashes.get(relative_path):
            raise ValidationError(
                f"staging file drifted after validation: {relative_path}"
            )
        if staged_hash == baseline_hashes[relative_path]:
            raise ValidationError(
                f"declared changed path has baseline-identical content: {relative_path}"
            )

    deadline = time.monotonic() + timeout
    backup = run_dir / "backup"
    apply_gate_dir = run_dir / "apply-gates"
    if backup.exists() or backup.is_symlink():
        raise ValidationError(
            "transaction backup already exists; refusing a repeated apply"
        )
    if apply_gate_dir.exists() or apply_gate_dir.is_symlink():
        raise ValidationError("transaction apply-gates artifact already exists")
    make_directory(backup)
    redactions = {
        str(project): "<PROJECT>",
        str(staging): "<STAGING>",
        str(run_dir): "<RUN_DIR>",
    }

    for relative_path in changed_paths:
        source = resolve_member(project, relative_path, must_exist=True)
        destination = backup / relative_path
        make_directory(destination.parent)
        atomic_write_bytes(
            destination, read_bytes_limited(source, MAX_TARGET_BYTES), mode=0o600
        )

    receipt["status"] = "applying"
    receipt["backup"] = "backup/"
    _write_receipt(run_dir, receipt)
    applied_paths: list[str] = []
    try:
        for relative_path in changed_paths:
            target = resolve_member(project, relative_path, must_exist=True)
            source = resolve_member(staging, relative_path, must_exist=True)
            mode = target.stat().st_mode & 0o777
            applied_paths.append(relative_path)
            atomic_write_bytes(
                target, read_bytes_limited(source, MAX_TARGET_BYTES), mode=mode
            )

        project_files = discover_project(project)
        apply_gates = run_gates(
            files=project_files,
            output_dir=apply_gate_dir,
            deadline=deadline,
            redactions=redactions,
            executable=kicad_executable,
        )
        apply_gate_data = gate_dict(apply_gates, prefix="apply-gates")
        reasons = regression_reasons(baseline_gate_data, apply_gate_data)
        receipt["apply_gates"] = apply_gate_data
        if reasons:
            _rollback(project=project, backup=backup, changed_paths=applied_paths)
            receipt["status"] = "rolled_back"
            receipt["rollback_reasons"] = reasons
            receipt["rollback_completed"] = True
            _record_apply_artifacts(run_dir, receipt)
            _write_receipt(run_dir, receipt)
            raise PCBDraftError(
                "apply validation regressed; original files restored from backup"
            )

        receipt["status"] = "applied"
        receipt["applied_at"] = utc_timestamp()
        _record_apply_artifacts(run_dir, receipt)
        receipt["applied_hashes"] = {
            path: sha256_file(
                resolve_member(project, path, must_exist=True),
                max_bytes=MAX_TARGET_BYTES,
            )
            for path in changed_paths
        }
        _write_receipt(run_dir, receipt)
        return run_dir
    # Roll back even on cancellation/KeyboardInterrupt after source mutation begins.
    except BaseException as exc:
        if receipt.get("status") == "applying":
            try:
                _rollback(project=project, backup=backup, changed_paths=applied_paths)
                receipt["rollback_completed"] = True
            except BaseException as rollback_exc:  # noqa: BLE001 -- rollback must not mask root failure
                receipt["rollback_completed"] = False
                receipt["rollback_failure"] = str(rollback_exc)
            receipt["status"] = (
                "rolled_back"
                if receipt.get("rollback_completed")
                else "rollback_failed"
            )
            receipt["failure"] = str(exc)
            _record_apply_artifacts(run_dir, receipt)
            _write_receipt(run_dir, receipt)
        raise
