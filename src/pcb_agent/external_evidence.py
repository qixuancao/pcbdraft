"""Honest import of externally produced L4/L6/L7 release evidence."""

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
from .ir import _json_value, canonical_json_bytes
from .locking import ResourceLock
from .managed import ManagedProject, open_managed_project
from .runs import utc_timestamp

EXTERNAL_SCHEMA = "pcb-agent-external-evidence"
EXTERNAL_VERSION = 1
EXTERNAL_INDEX = "external-evidence.json"
EXTERNAL_DIR = "external-evidence"
EXTERNAL_LIMIT = 64 * 1024 * 1024
INDEX_LIMIT = 4 * 1024 * 1024
METADATA_LIMIT = 256 * 1024
MAX_ARTIFACTS = 64
EXTERNAL_LEVELS = {"L4", "L6", "L7"}
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


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
    if level not in EXTERNAL_LEVELS:
        raise ValidationError("external evidence level must be L4, L6, or L7")
    if outcome not in {"pass", "fail"}:
        raise ValidationError("external evidence outcome must be pass or fail")
    for name, value, limit in (
        ("actor", actor, 256),
        ("role", role, 256),
        ("performed_at", performed_at, 64),
        ("statement", statement, 4096),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > limit
        ):
            raise ValidationError(f"external evidence {name} is invalid")
    if _UTC_RE.fullmatch(performed_at) is None:
        raise ValidationError("external evidence performed_at must be UTC ISO-8601")
    normalized_metadata = _validate_metadata(level, metadata)
    if not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        raise ValidationError(f"external evidence requires 1-{MAX_ARTIFACTS} artifacts")

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
                "metadata": normalized_metadata,
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
        if level not in EXTERNAL_LEVELS or level in seen:
            raise ValidationError("external evidence levels are invalid or duplicated")
        seen.add(level)
        if entry["outcome"] not in {"pass", "fail"}:
            raise ValidationError("external evidence outcome is invalid")
        if entry["verification"] != "externally_supplied_not_independently_verified":
            raise ValidationError("external evidence attribution is invalid")
        for name, limit in (
            ("actor", 256),
            ("role", 256),
            ("performed_at", 64),
            ("recorded_at", 64),
            ("statement", 4096),
        ):
            value = entry[name]
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.encode("utf-8")) > limit
            ):
                raise ValidationError(f"external evidence {name} is invalid")
        if _UTC_RE.fullmatch(entry["performed_at"]) is None:
            raise ValidationError("external evidence performed_at is invalid")
        if _UTC_RE.fullmatch(entry["recorded_at"]) is None:
            raise ValidationError("external evidence recorded_at is invalid")
        _validate_metadata(level, entry["metadata"])
        if not isinstance(entry["artifacts"], list) or not entry["artifacts"]:
            raise ValidationError("external evidence artifacts are missing")
        if len(entry["artifacts"]) > MAX_ARTIFACTS:
            raise ValidationError("external evidence artifact count is excessive")
        total = 0
        for artifact in entry["artifacts"]:
            total += _verify_artifact(project, artifact)
            if total > EXTERNAL_LIMIT:
                raise ValidationError("external evidence exceeds the total byte limit")
    return document


def _validate_metadata(level: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("external evidence metadata must be an object")
    normalized = _json_value(value, "$.metadata")
    if len(canonical_json_bytes(normalized)) > METADATA_LIMIT:
        raise ValidationError("external evidence metadata exceeds its byte limit")
    required_by_level = {
        "L4": {
            "authorized_sourcing_snapshot",
            "fabricator_capability",
            "assembly_profile",
        },
        "L6": {"review_scope", "reviewer_qualification"},
        "L7": {"board_serial", "test_plan", "result_summary"},
    }
    required = required_by_level[level]
    if not required <= set(normalized) or not all(
        isinstance(normalized[name], str) and normalized[name].strip()
        for name in required
    ):
        raise ValidationError(
            f"{level} evidence requires metadata: {', '.join(sorted(required))}"
        )
    return normalized


def _verify_artifact(project: ManagedProject, value: Any) -> int:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise ValidationError("external evidence artifact record is malformed")
    relative = value["path"]
    if (
        not isinstance(relative, str)
        or not relative.startswith(f"{EXTERNAL_DIR}/")
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or Path(relative).as_posix() != relative
    ):
        raise ValidationError("external evidence artifact path is unsafe")
    size = value["size"]
    digest = value["sha256"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= EXTERNAL_LIMIT
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValidationError("external evidence artifact identity is invalid")
    path = project.root / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValidationError("external evidence artifact is missing or unsafe")
    data = read_bytes_limited(path, EXTERNAL_LIMIT)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValidationError("external evidence artifact hash mismatch")
    return len(data)
