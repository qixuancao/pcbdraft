"""Bidirectional synchronization between managed semantic IR and native KiCad."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CopperWrightError, ValidationError
from .io import atomic_write_json, load_json_limited, make_directory
from .kicad_pcb import inspect_native_board
from .kicad_schematic import inspect_native_schematic
from .locking import ResourceLock
from .managed import (
    MANAGED_MANIFEST_LIMIT,
    ManagedProject,
    inspect_native_project,
    materialize_managed_design,
    open_managed_project,
)
from .operations import ChangeSet, apply_change_set, semantic_diff
from .parts import PartGraph
from .project import sha256_file
from .requirements import load_requirements
from .runs import utc_timestamp
from .validation import validate_managed_project

SYNC_RECEIPT_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class SyncPreview:
    project_root: Path
    board_sha256: str
    manifest_sha256: str
    tracked_hashes: dict[str, str]
    change_set: ChangeSet | None
    diff: dict[str, Any]
    native_changes: tuple[dict[str, Any], ...]

    @property
    def has_changes(self) -> bool:
        return self.change_set is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "copperwright-kicad-sync-preview",
            "version": 1,
            "project": str(self.project_root),
            "board_sha256": self.board_sha256,
            "manifest_sha256": self.manifest_sha256,
            "tracked_hashes": dict(sorted(self.tracked_hashes.items())),
            "has_changes": self.has_changes,
            "native_changes": list(self.native_changes),
            "change_set": self.change_set.to_dict() if self.change_set else None,
            "semantic_diff": self.diff,
        }


def preview_kicad_import(
    project_value: ManagedProject | str | Path,
    *,
    graph: PartGraph | None = None,
    system_python: str | Path | None = None,
) -> SyncPreview:
    """Translate recognized native placement edits into a semantic change set.

    Topology, part contracts, schematic semantics, routes, and board rules must
    still match their generated snapshots. Unknown native edits are rejected
    rather than silently discarded or represented as raw text changes.
    """
    project = (
        project_value
        if isinstance(project_value, ManagedProject)
        else open_managed_project(project_value)
    )
    resolved_graph = graph or PartGraph.bundled()
    drift = set(project.drift())
    allowed = {"board:hash_mismatch", "kicad_project:hash_mismatch"}
    if drift - allowed:
        raise ValidationError(
            "KiCad import requires every non-board artifact to match the manifest: "
            + ", ".join(sorted(drift - allowed))
        )
    current_schematic = inspect_native_schematic(project.schematic_path)
    baseline_schematic = project.manifest["native_snapshots"]["schematic"]
    if current_schematic != baseline_schematic:
        raise ValidationError(
            "native schematic contains unsupported semantic or graphical edits"
        )
    current_project = inspect_native_project(project.project_path)
    baseline_project = project.manifest["native_snapshots"]["project"]
    if current_project != baseline_project:
        raise ValidationError("native KiCad project rules/settings changed")
    current_board = inspect_native_board(
        project.design, project.board_path, system_python=system_python
    )
    baseline_board = project.manifest["native_snapshots"]["board"]
    changes = _board_pose_changes(baseline_board, current_board)
    board_hash = sha256_file(project.board_path, max_bytes=128 * 1024 * 1024)
    manifest_hash = sha256_file(project.manifest_path, max_bytes=MANAGED_MANIFEST_LIMIT)
    tracked_hashes = {
        name: sha256_file(project.root / relative, max_bytes=128 * 1024 * 1024)
        for name, relative in project.manifest["files"].items()
        if name != "manifest"
    }
    if not changes:
        if "board:hash_mismatch" in drift:
            raise ValidationError(
                "native board bytes changed, but no supported semantic placement edit was found"
            )
        return SyncPreview(
            project.root,
            board_hash,
            manifest_hash,
            tracked_hashes,
            None,
            {
                "schema": "copperwright-semantic-diff",
                "version": 1,
                "before_hash": project.design.content_hash(),
                "after_hash": project.design.content_hash(),
                "summary": {
                    "objects_added": 0,
                    "objects_removed": 0,
                    "objects_modified": 0,
                    "requires_component_contract_validation": False,
                    "requires_connectivity_validation": False,
                    "requires_geometry_validation": False,
                },
                "collections": {},
                "board_fields": {},
                "metadata_fields": {},
            },
            (),
        )

    component_by_reference = {
        component.reference: component for component in project.design.components
    }
    operations = []
    for index, change in enumerate(changes, start=1):
        component = component_by_reference.get(change["reference"])
        if component is None or component.placement is None:
            raise ValidationError(
                f"native placement references an unknown semantic component: {change['reference']}"
            )
        part = resolved_graph.get(component.part_id)
        if part.footprint is None:
            raise ValidationError(
                f"native placement references a board-excluded component: {change['reference']}"
            )
        operations.append(
            {
                "id": f"import_pose_{index:03d}",
                "op": "update_component",
                "args": {
                    "component_id": component.id,
                    "changes": {
                        "placement": {
                            "x_mm": change["after"]["x_mm"],
                            "y_mm": change["after"]["y_mm"],
                            "rotation_deg": change["after"]["rotation_deg"],
                            "side": change["after"]["side"],
                            "fixed": True,
                        }
                    },
                },
                "expected": {"placement": component.placement.to_dict()},
                "reason": "Import a reviewed native KiCad footprint placement into semantic IR.",
            }
        )
    identity = hashlib.sha256(
        (project.design.content_hash() + board_hash).encode("ascii")
    ).hexdigest()[:16]
    change_set = ChangeSet.from_dict(
        {
            "schema": "copperwright-change-set",
            "version": 1,
            "id": f"kicad_import_{identity}",
            "base_hash": project.design.content_hash(),
            "intent": "Synchronize reviewed KiCad footprint placements back to semantic IR.",
            "actor": "kicad_bidirectional_sync",
            "operations": operations,
            "provenance": ["native_kicad_board"],
        }
    )
    after = apply_change_set(project.design, change_set)
    return SyncPreview(
        project.root,
        board_hash,
        manifest_hash,
        tracked_hashes,
        change_set,
        semantic_diff(project.design, after),
        tuple(changes),
    )


def apply_kicad_import(
    preview: SyncPreview,
    *,
    graph: PartGraph | None = None,
    system_python: str | Path | None = None,
    timeout: float = 120.0,
) -> Path:
    """Validate, regenerate, and atomically publish a previewed KiCad import."""
    if preview.change_set is None:
        raise ValidationError("KiCad synchronization preview contains no changes")
    project = open_managed_project(preview.project_root)
    _verify_preview_baseline(project, preview)
    resolved_graph = graph or PartGraph.bundled()
    after = apply_change_set(project.design, preview.change_set)
    requirements = load_requirements(project.requirements_path)

    transaction_parent = project.root.parent / ".copperwright-transactions"
    make_directory(transaction_parent)
    transaction = transaction_parent / (
        f"sync-{preview.change_set.id}-{secrets.token_hex(4)}"
    )
    make_directory(transaction)
    receipt_path = transaction / "receipt.json"
    receipt: dict[str, Any] = {
        "schema": "copperwright-project-transaction",
        "version": 1,
        "kind": "kicad_import",
        "status": "preparing",
        "created_at": utc_timestamp(),
        "project": str(project.root),
        "before_board_sha256": preview.board_sha256,
        "before_manifest_sha256": preview.manifest_sha256,
        "before_design_hash": project.design.content_hash(),
        "after_design_hash": after.content_hash(),
        "change_set": preview.change_set.to_dict(),
        "semantic_diff": preview.diff,
    }
    atomic_write_json(receipt_path, receipt)
    staged = transaction / "staged"
    backup = transaction / "backup"
    try:
        generated = materialize_managed_design(
            requirements,
            after,
            staged,
            graph=resolved_graph,
            system_python=system_python,
        )
        validation = validate_managed_project(
            generated.project,
            output=transaction / "validation",
            graph=resolved_graph,
            timeout=timeout,
        )
        if not validation.candidate_ready:
            raise ValidationError(
                "regenerated KiCad import did not pass engineering-candidate gates"
            )
        receipt.update(
            {
                "status": "ready",
                "validation_report_sha256": validation.report_sha256,
                "staged_manifest_sha256": sha256_file(
                    generated.project.manifest_path,
                    max_bytes=MANAGED_MANIFEST_LIMIT,
                ),
            }
        )
        atomic_write_json(receipt_path, receipt)
        lock_parent = project.root.parent / ".copperwright-locks"
        with ResourceLock(project.root, lock_parent, timeout=10.0):
            current = open_managed_project(project.root)
            _verify_preview_baseline(current, preview)
            receipt["status"] = "applying"
            atomic_write_json(receipt_path, receipt)
            os.rename(project.root, backup)
            try:
                os.rename(staged, project.root)
                _fsync_directory(project.root.parent)
                applied = open_managed_project(project.root)
                applied.assert_synchronized()
            except BaseException:
                failed = transaction / "failed-publication"
                if project.root.exists() and not failed.exists():
                    os.rename(project.root, failed)
                if backup.exists() and not project.root.exists():
                    os.rename(backup, project.root)
                    _fsync_directory(project.root.parent)
                raise
            receipt.update(
                {
                    "status": "applied",
                    "applied_at": utc_timestamp(),
                    "applied_manifest_sha256": sha256_file(
                        applied.manifest_path, max_bytes=MANAGED_MANIFEST_LIMIT
                    ),
                    "backup": backup.name,
                }
            )
            atomic_write_json(receipt_path, receipt)
        return transaction
    except BaseException as exc:
        if receipt.get("status") not in {"applied"}:
            receipt["status"] = (
                "rolled_back" if project.root.exists() else "recovery_required"
            )
            receipt["failure"] = str(exc)[:2048]
            atomic_write_json(receipt_path, receipt)
        raise


def undo_kicad_import(transaction_value: str | Path) -> Path:
    transaction = Path(transaction_value).resolve(strict=True)
    receipt_path = transaction / "receipt.json"
    receipt = load_json_limited(receipt_path, SYNC_RECEIPT_LIMIT)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "copperwright-project-transaction"
    ):
        raise ValidationError("unsupported project transaction receipt")
    project = Path(receipt.get("project", "")).resolve(strict=True)
    backup_name = receipt.get("backup")
    if backup_name != "backup":
        raise ValidationError("project transaction backup reference is malformed")
    backup = transaction / backup_name
    if receipt.get("status") == "undone":
        return transaction
    if receipt.get("status") != "applied" or not backup.is_dir():
        raise ValidationError("undo requires an applied transaction with a backup")
    with ResourceLock(project, project.parent / ".copperwright-locks", timeout=10.0):
        current = open_managed_project(project)
        if sha256_file(
            current.manifest_path, max_bytes=MANAGED_MANIFEST_LIMIT
        ) != receipt.get("applied_manifest_sha256"):
            raise ValidationError("managed project changed after synchronization")
        replaced = transaction / "replaced"
        if replaced.exists() or replaced.is_symlink():
            raise ValidationError("transaction replacement slot is occupied")
        os.rename(project, replaced)
        try:
            os.rename(backup, project)
            _fsync_directory(project.parent)
        except BaseException:
            if not project.exists() and replaced.exists():
                os.rename(replaced, project)
            raise
        receipt["status"] = "undone"
        receipt["undone_at"] = utc_timestamp()
        atomic_write_json(receipt_path, receipt)
    return transaction


def recover_kicad_import(transaction_value: str | Path) -> str:
    """Recover a directory swap interrupted between backup and publication."""
    transaction = Path(transaction_value).resolve(strict=True)
    receipt_path = transaction / "receipt.json"
    receipt = load_json_limited(receipt_path, SYNC_RECEIPT_LIMIT)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "copperwright-project-transaction"
    ):
        raise ValidationError("unsupported project transaction receipt")
    project = Path(receipt.get("project", "")).resolve(strict=False)
    backup = transaction / "backup"
    if project.exists():
        return str(receipt.get("status", "unknown"))
    if not backup.is_dir():
        raise ValidationError("project is missing and no recoverable backup exists")
    with ResourceLock(project, project.parent / ".copperwright-locks", timeout=10.0):
        if not project.exists():
            os.rename(backup, project)
            _fsync_directory(project.parent)
        receipt["status"] = "rolled_back"
        receipt["recovered_at"] = utc_timestamp()
        atomic_write_json(receipt_path, receipt)
    return "rolled_back"


def _board_pose_changes(
    baseline: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]]:
    for field in ("schema", "version", "mode", "board", "tracks"):
        if current.get(field) != baseline.get(field):
            raise ValidationError(f"native board contains unsupported {field} changes")
    before = {item["reference"]: item for item in baseline.get("components", [])}
    after = {item["reference"]: item for item in current.get("components", [])}
    if len(before) != len(baseline.get("components", [])) or len(after) != len(
        current.get("components", [])
    ):
        raise ValidationError("native board contains duplicate references")
    if set(before) != set(after):
        raise ValidationError("native board component set changed")
    pose_fields = {"x_mm", "y_mm", "rotation_deg", "side"}
    changes = []
    for reference in sorted(before):
        old = before[reference]
        new = after[reference]
        if {key: value for key, value in old.items() if key not in pose_fields} != {
            key: value for key, value in new.items() if key not in pose_fields
        }:
            raise ValidationError(
                f"native board contains unsupported topology/part edit at {reference}"
            )
        old_pose = {field: old[field] for field in pose_fields}
        new_pose = {field: new[field] for field in pose_fields}
        if old_pose != new_pose:
            changes.append(
                {"reference": reference, "before": old_pose, "after": new_pose}
            )
    return changes


def _verify_preview_baseline(project: ManagedProject, preview: SyncPreview) -> None:
    if project.root != preview.project_root:
        raise ValidationError("synchronization preview targets another project")
    board_hash = sha256_file(project.board_path, max_bytes=128 * 1024 * 1024)
    manifest_hash = sha256_file(project.manifest_path, max_bytes=MANAGED_MANIFEST_LIMIT)
    if board_hash != preview.board_sha256 or manifest_hash != preview.manifest_sha256:
        raise ValidationError("managed project drifted after synchronization preview")
    for name, expected in preview.tracked_hashes.items():
        relative = project.manifest["files"].get(name)
        if not isinstance(relative, str):
            raise ValidationError("synchronization preview file map changed")
        actual = sha256_file(project.root / relative, max_bytes=128 * 1024 * 1024)
        if actual != expected:
            raise ValidationError(
                f"managed project file changed after synchronization preview: {name}"
            )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise CopperWrightError(f"cannot fsync transaction directory: {path}") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
