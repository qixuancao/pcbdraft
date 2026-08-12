"""Honest import of externally produced L6/L7 review and physical evidence."""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
    make_directory,
    read_bytes_limited,
)
from .locking import ResourceLock
from .managed import ManagedProject, open_managed_project
from .runs import utc_timestamp

EXTERNAL_SCHEMA = "pcb-agent-external-evidence"
EXTERNAL_VERSION = 1
EXTERNAL_INDEX = "external-evidence.json"
EXTERNAL_DIR = "external-evidence"
EXTERNAL_LIMIT = 64 * 1024 * 1024
INDEX_LIMIT = 4 * 1024 * 1024


def record_external_evidence(
    project_value: ManagedProject | str | Path,
    *,
    level: str,
    outcome: str,
    actor: str,
    role: str,
    performed_at: str,
    statement: str,
    artifacts: list[str | Path],
    metadata: dict[str, Any],
) -> Path:
    """Record supplied evidence with attribution; never infer or self-sign it."""
    project = (
        project_value
        if isinstance(project_value, ManagedProject)
        else open_managed_project(project_value)
    )
    if level not in {"L6", "L7"}:
        raise ValidationError("external evidence level must be L6 or L7")
    if outcome not in {"pass", "fail"}:
        raise ValidationError("external evidence outcome must be pass or fail")
    for name, value, limit in (
        ("actor", actor, 256),
        ("role", role, 256),
        ("performed_at", performed_at, 64),
        ("statement", statement, 4096),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValidationError(f"external evidence {name} is invalid")
    if not isinstance(metadata, dict):
        raise ValidationError("external evidence metadata must be an object")
    required_metadata = (
        {"review_scope", "reviewer_qualification"}
        if level == "L6"
        else {"board_serial", "test_plan", "result_summary"}
    )
    if not required_metadata <= set(metadata) or not all(
        isinstance(metadata[name], str) and metadata[name].strip()
        for name in required_metadata
    ):
        raise ValidationError(
            f"{level} evidence requires metadata: {', '.join(sorted(required_metadata))}"
        )
    if not artifacts:
        raise ValidationError("external evidence requires at least one artifact")

    evidence_dir = project.root / EXTERNAL_DIR
    index_path = project.root / EXTERNAL_INDEX
    with ResourceLock(
        project.root,
        project.root.parent / ".pcb-agent-locks",
        timeout=10.0,
    ):
        make_directory(evidence_dir)
        records = []
        total = 0
        for index, source_value in enumerate(artifacts, start=1):
            source = Path(source_value)
            try:
                info = source.lstat()
            except OSError as exc:
                raise ValidationError(
                    f"external evidence artifact is missing: {source}"
                ) from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or source.is_symlink()
            ):
                raise ValidationError(
                    "external evidence artifact must be a regular file"
                )
            data = read_bytes_limited(source, EXTERNAL_LIMIT)
            total += len(data)
            if total > EXTERNAL_LIMIT:
                raise ValidationError("external evidence exceeds the total byte limit")
            digest = hashlib.sha256(data).hexdigest()
            safe_name = (
                re.sub(r"[^A-Za-z0-9_.-]+", "_", source.name)[:128] or "artifact"
            )
            target_name = f"{level.lower()}-{index:02d}-{digest[:12]}-{safe_name}"
            target = evidence_dir / target_name
            if target.exists() or target.is_symlink():
                if read_bytes_limited(target, EXTERNAL_LIMIT) != data:
                    raise ValidationError("external evidence artifact name collision")
            else:
                atomic_write_bytes(target, data, mode=0o600)
            records.append(
                {
                    "path": f"{EXTERNAL_DIR}/{target_name}",
                    "size": len(data),
                    "sha256": digest,
                }
            )
        document = load_external_evidence(project)
        entries = [
            entry for entry in document["entries"] if entry.get("level") != level
        ]
        entries.append(
            {
                "level": level,
                "outcome": outcome,
                "actor": actor,
                "role": role,
                "performed_at": performed_at,
                "recorded_at": utc_timestamp(),
                "statement": statement,
                "metadata": metadata,
                "artifacts": records,
                "verification": "externally_supplied_not_independently_verified",
            }
        )
        document["entries"] = sorted(entries, key=lambda entry: entry["level"])
        atomic_write_json(index_path, document)
    return index_path


def load_external_evidence(
    project_value: ManagedProject | str | Path,
) -> dict[str, Any]:
    project = (
        project_value
        if isinstance(project_value, ManagedProject)
        else open_managed_project(project_value)
    )
    path = project.root / EXTERNAL_INDEX
    if not path.exists():
        return {
            "schema": EXTERNAL_SCHEMA,
            "version": EXTERNAL_VERSION,
            "entries": [],
        }
    if path.is_symlink() or not path.is_file():
        raise ValidationError("external evidence index is unsafe")
    document = load_json_limited(path, INDEX_LIMIT)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "version", "entries"}
        or document["schema"] != EXTERNAL_SCHEMA
        or document["version"] != EXTERNAL_VERSION
        or not isinstance(document["entries"], list)
    ):
        raise ValidationError("external evidence index is malformed")
    seen = set()
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "level",
            "outcome",
            "actor",
            "role",
            "performed_at",
            "recorded_at",
            "statement",
            "metadata",
            "artifacts",
            "verification",
        }:
            raise ValidationError("external evidence entry is malformed")
        level = entry["level"]
        if level not in {"L6", "L7"} or level in seen:
            raise ValidationError("external evidence levels are invalid or duplicated")
        seen.add(level)
        if entry["outcome"] not in {"pass", "fail"}:
            raise ValidationError("external evidence outcome is invalid")
        if entry["verification"] != "externally_supplied_not_independently_verified":
            raise ValidationError("external evidence attribution is invalid")
        if not isinstance(entry["artifacts"], list) or not entry["artifacts"]:
            raise ValidationError("external evidence artifacts are missing")
        for artifact in entry["artifacts"]:
            _verify_artifact(project, artifact)
    return document


def _verify_artifact(project: ManagedProject, value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise ValidationError("external evidence artifact record is malformed")
    relative = value["path"]
    if (
        not isinstance(relative, str)
        or not relative.startswith(f"{EXTERNAL_DIR}/")
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValidationError("external evidence artifact path is unsafe")
    path = project.root / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValidationError("external evidence artifact is missing or unsafe")
    data = read_bytes_limited(path, EXTERNAL_LIMIT)
    if (
        len(data) != value["size"]
        or hashlib.sha256(data).hexdigest() != value["sha256"]
    ):
        raise ValidationError("external evidence artifact hash mismatch")
