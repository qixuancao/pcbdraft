"""Previewable, reversible, crash-recoverable semantic IR transactions."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .errors import PCBDraftError, ValidationError
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
    make_directory,
    read_bytes_limited,
)
from .ir import IR_FILE_LIMIT, Design, load_design
from .locking import ResourceLock
from .operations import ChangeSet, apply_change_set, semantic_diff
from .parts import PartGraph
from .runs import canonical_output_parent, create_run, utc_timestamp
from .scope import assert_supported

TRANSACTION_RECEIPT_VERSION = 1
TRANSACTION_RECEIPT_LIMIT = 16 * 1024 * 1024
Validator = Callable[[Design], Iterable[Any] | None]


def _canonical_ir_path(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ValidationError("semantic IR path must not be a symlink")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"semantic IR file does not exist: {candidate}") from exc
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValidationError("semantic IR must be a single-link regular file")
    return path


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lock_parent(output_parent: str | Path | None) -> Path:
    if output_parent is None:
        parent = canonical_output_parent(None)
    else:
        parent = Path(output_parent).expanduser().resolve(strict=False)
        make_directory(parent)
    return parent / ".locks"


def _run_validators(
    design: Design, validators: Iterable[Validator]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, validator in enumerate(validators):
        name = getattr(validator, "__name__", f"validator_{index}")
        result = validator(design)
        items = [] if result is None else list(result)
        failures: list[dict[str, Any]] = []
        for item in items:
            if hasattr(item, "to_dict"):
                value = item.to_dict()
            elif isinstance(item, dict):
                value = dict(item)
            else:
                value = {"severity": "error", "message": str(item)}
            if value.get("severity") == "error":
                failures.append(value)
        evidence.append(
            {
                "validator": name,
                "findings": items_to_json(items),
                "passed": not failures,
            }
        )
        if failures:
            first = failures[0]
            raise ValidationError(
                f"semantic transaction validator {name} failed ({len(failures)} errors): "
                f"{first.get('code', 'validation')}: {first.get('message', '')}"
            )
    return evidence


def items_to_json(items: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if hasattr(item, "to_dict"):
            result.append(item.to_dict())
        elif isinstance(item, dict):
            result.append(dict(item))
        else:
            result.append({"value": str(item)})
    return result


def default_validators(
    graph: PartGraph | None = None, *, check_libraries: bool = False
) -> tuple[Validator, ...]:
    resolved_graph = graph or PartGraph.bundled()

    def scope_validator(design: Design) -> list[Any]:
        assert_supported(design)
        return []

    def part_validator(design: Design) -> list[Any]:
        return resolved_graph.validate_design(design, check_libraries=check_libraries)

    return (scope_validator, part_validator)


def prepare_transaction(
    ir_value: str | Path,
    change_set: ChangeSet,
    *,
    output_parent: str | Path | None = None,
    validators: Iterable[Validator] | None = None,
    lock_timeout: float = 10.0,
) -> Path:
    source = _canonical_ir_path(ir_value)
    parent = canonical_output_parent(
        str(output_parent) if output_parent is not None else None
    )
    if parent == source.parent or parent.is_relative_to(source.parent):
        raise ValidationError(
            "semantic transaction output must be outside the design directory"
        )
    run_id, run_dir = create_run(str(parent))
    receipt: dict[str, Any] = {
        "receipt_version": TRANSACTION_RECEIPT_VERSION,
        "kind": "semantic_transaction",
        "run_id": run_id,
        "change_set_id": change_set.id,
        "change_set_hash": change_set.content_hash(),
        "source": str(source),
        "status": "preparing",
        "created_at": utc_timestamp(),
    }
    atomic_write_json(run_dir / "receipt.json", receipt)
    try:
        with ResourceLock(source, _lock_parent(parent), timeout=lock_timeout):
            before_bytes = read_bytes_limited(source, IR_FILE_LIMIT)
            before = load_design(source)
            after = apply_change_set(before, change_set)
            validation = _run_validators(after, validators or default_validators())
            after_bytes = after.canonical_bytes()
            atomic_write_bytes(run_dir / "before.pcbir.json", before_bytes)
            atomic_write_bytes(run_dir / "after.pcbir.json", after_bytes)
            atomic_write_bytes(
                run_dir / "change_set.json", change_set.canonical_bytes()
            )
            atomic_write_json(
                run_dir / "semantic_diff.json", semantic_diff(before, after)
            )
            atomic_write_json(run_dir / "validation.json", validation)
            receipt.update(
                {
                    "status": "ready",
                    "source_file_sha256": _hash(before_bytes),
                    "before_semantic_hash": before.content_hash(),
                    "after_file_sha256": _hash(after_bytes),
                    "after_semantic_hash": after.content_hash(),
                    "artifacts": [
                        "before.pcbir.json",
                        "after.pcbir.json",
                        "change_set.json",
                        "semantic_diff.json",
                        "validation.json",
                        "receipt.json",
                    ],
                }
            )
            atomic_write_json(run_dir / "receipt.json", receipt)
        return run_dir
    except BaseException as exc:
        if receipt.get("status") == "preparing":
            receipt["status"] = "failed"
            receipt["failure"] = str(exc)
            atomic_write_json(run_dir / "receipt.json", receipt)
        raise


def _transaction_dir(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    try:
        run_dir = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(
            f"semantic transaction directory does not exist: {candidate}"
        ) from exc
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise ValidationError("semantic transaction path must be a real directory")
    return run_dir


def _load_receipt(run_dir: Path) -> dict[str, Any]:
    value = load_json_limited(run_dir / "receipt.json", TRANSACTION_RECEIPT_LIMIT)
    if (
        not isinstance(value, dict)
        or value.get("receipt_version") != TRANSACTION_RECEIPT_VERSION
        or value.get("kind") != "semantic_transaction"
    ):
        raise ValidationError("unsupported or malformed semantic transaction receipt")
    return value


def _verified_artifact(run_dir: Path, name: str, expected_hash: str) -> bytes:
    path = run_dir / name
    if path.is_symlink():
        raise ValidationError(f"transaction artifact must not be a symlink: {name}")
    data = read_bytes_limited(path, IR_FILE_LIMIT)
    if _hash(data) != expected_hash:
        raise ValidationError(f"transaction artifact hash mismatch: {name}")
    return data


def apply_transaction(
    run_value: str | Path,
    *,
    lock_timeout: float = 10.0,
) -> Path:
    run_dir = _transaction_dir(run_value)
    receipt = _load_receipt(run_dir)
    source = _canonical_ir_path(receipt.get("source", ""))
    parent = run_dir.parent
    with ResourceLock(source, _lock_parent(parent), timeout=lock_timeout):
        status = receipt.get("status")
        current = read_bytes_limited(source, IR_FILE_LIMIT)
        if status == "applied":
            if _hash(current) == receipt.get("applied_file_sha256"):
                return run_dir
            raise ValidationError(
                "an applied semantic transaction no longer matches its source"
            )
        if status != "ready":
            raise ValidationError("apply accepts only a ready semantic transaction")
        if _hash(current) != receipt.get("source_file_sha256"):
            raise ValidationError(
                "semantic design drifted from the transaction baseline"
            )
        staged = _verified_artifact(
            run_dir, "after.pcbir.json", receipt.get("after_file_sha256", "")
        )
        staged_design = Design.from_dict(_load_json_bytes(staged, "after.pcbir.json"))
        if staged_design.content_hash() != receipt.get("after_semantic_hash"):
            raise ValidationError(
                "staged semantic hash does not match the transaction receipt"
            )
        backup = run_dir / "backup.pcbir.json"
        if backup.exists() or backup.is_symlink():
            raise ValidationError(
                "transaction backup already exists before first apply"
            )
        atomic_write_bytes(backup, current)
        receipt["status"] = "applying"
        receipt["backup"] = backup.name
        atomic_write_json(run_dir / "receipt.json", receipt)
        try:
            atomic_write_bytes(source, staged, mode=source.stat().st_mode & 0o777)
            applied = read_bytes_limited(source, IR_FILE_LIMIT)
            applied_design = load_design(source)
            if _hash(applied) != receipt["after_file_sha256"]:
                raise PCBDraftError("post-write semantic transaction hash mismatch")
            receipt.update(
                {
                    "status": "applied",
                    "applied_at": utc_timestamp(),
                    "applied_file_sha256": _hash(applied),
                    "applied_semantic_hash": applied_design.content_hash(),
                }
            )
            atomic_write_json(run_dir / "receipt.json", receipt)
            return run_dir
        except BaseException as exc:
            rollback_ok = False
            try:
                original = read_bytes_limited(backup, IR_FILE_LIMIT)
                atomic_write_bytes(source, original, mode=source.stat().st_mode & 0o777)
                rollback_ok = (
                    _hash(read_bytes_limited(source, IR_FILE_LIMIT))
                    == receipt["source_file_sha256"]
                )
            except BaseException as rollback_exc:  # noqa: BLE001 -- preserve original failure
                receipt["rollback_failure"] = str(rollback_exc)
            receipt["status"] = "rolled_back" if rollback_ok else "rollback_failed"
            receipt["rollback_completed"] = rollback_ok
            receipt["failure"] = str(exc)
            atomic_write_json(run_dir / "receipt.json", receipt)
            raise


def undo_transaction(run_value: str | Path, *, lock_timeout: float = 10.0) -> Path:
    run_dir = _transaction_dir(run_value)
    receipt = _load_receipt(run_dir)
    source = _canonical_ir_path(receipt.get("source", ""))
    with ResourceLock(source, _lock_parent(run_dir.parent), timeout=lock_timeout):
        current = read_bytes_limited(source, IR_FILE_LIMIT)
        if receipt.get("status") == "undone":
            if _hash(current) == receipt.get("source_file_sha256"):
                return run_dir
            raise ValidationError(
                "an undone semantic transaction no longer matches its baseline"
            )
        if receipt.get("status") != "applied":
            raise ValidationError("undo accepts only an applied semantic transaction")
        if _hash(current) != receipt.get("applied_file_sha256"):
            raise ValidationError(
                "semantic design changed after apply; refusing destructive undo"
            )
        backup_name = receipt.get("backup")
        if not isinstance(backup_name, str) or Path(backup_name).name != backup_name:
            raise ValidationError("semantic transaction backup reference is malformed")
        original = _verified_artifact(
            run_dir, backup_name, receipt.get("source_file_sha256", "")
        )
        atomic_write_bytes(source, original, mode=source.stat().st_mode & 0o777)
        restored = load_design(source)
        if restored.content_hash() != receipt.get("before_semantic_hash"):
            raise PCBDraftError(
                "undo restored bytes but semantic identity differs from baseline"
            )
        receipt["status"] = "undone"
        receipt["undone_at"] = utc_timestamp()
        atomic_write_json(run_dir / "receipt.json", receipt)
        return run_dir


def recover_transaction(run_value: str | Path, *, lock_timeout: float = 10.0) -> Path:
    """Recover a transaction interrupted only after its backup journal was durable."""
    run_dir = _transaction_dir(run_value)
    receipt = _load_receipt(run_dir)
    source = _canonical_ir_path(receipt.get("source", ""))
    if receipt.get("status") not in {"applying", "rollback_failed"}:
        raise ValidationError(
            "recovery is only valid for applying/rollback_failed transactions"
        )
    with ResourceLock(source, _lock_parent(run_dir.parent), timeout=lock_timeout):
        backup_name = receipt.get("backup")
        if not isinstance(backup_name, str) or Path(backup_name).name != backup_name:
            raise ValidationError("transaction has no safe recovery backup")
        original = _verified_artifact(
            run_dir, backup_name, receipt.get("source_file_sha256", "")
        )
        current_hash = _hash(read_bytes_limited(source, IR_FILE_LIMIT))
        allowed = {receipt.get("source_file_sha256"), receipt.get("after_file_sha256")}
        if current_hash not in allowed:
            raise ValidationError(
                "source has unrelated changes; automatic recovery would destroy data"
            )
        atomic_write_bytes(source, original, mode=source.stat().st_mode & 0o777)
        load_design(source)
        receipt["status"] = "recovered"
        receipt["recovered_at"] = utc_timestamp()
        receipt["rollback_completed"] = True
        atomic_write_json(run_dir / "receipt.json", receipt)
        return run_dir


def _load_json_bytes(data: bytes, label: str) -> Any:
    import json

    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"cannot parse transaction artifact {label}: {exc}"
        ) from exc
