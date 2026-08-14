"""Persistent location for the user's PCBDraft project repository.

The application is intentionally not a general-purpose coding agent.  Its
durable output belongs in one user-selected repository, regardless of the
directory from which ``pcbdraft`` is launched.  This module owns the small
pointer stored in the user's configuration directory and the marker stored in
the repository itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, make_directory
from pcbdraft.core.runs import utc_timestamp

REPOSITORY_CONFIG_SCHEMA = "pcbdraft-project-repository-config"
REPOSITORY_MARKER_SCHEMA = "pcbdraft-project-repository"
REPOSITORY_VERSION = 1
REPOSITORY_CONFIG_LIMIT = 16 * 1024
REPOSITORY_MARKER_NAME = ".pcbdraft-repository.json"


@dataclass(frozen=True)
class ProjectRepository:
    """A validated project repository and the source of its location."""

    root: Path
    source: str
    configured_now: bool = False

    @property
    def projects_root(self) -> Path:
        return self.root / "projects"


def repository_config_path() -> Path:
    """Return the private, user-owned pointer to the selected repository."""

    explicit = os.environ.get("PCBDRAFT_REPOSITORY_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "pcbdraft" / "repository.json"


def default_repository_path() -> Path:
    """Return the clear, user-visible location used on first launch."""

    return Path.home() / "PCBDraft"


def legacy_repository_path() -> Path:
    """Return the pre-repository application home used by PCBDraft 1.0."""

    return Path.home() / ".local" / "share" / "pcbdraft" / "application"


def configure_repository(directory: str | Path) -> ProjectRepository:
    """Persist ``directory`` as the sole normal destination for PCB projects."""

    root = _prepare_repository(directory)
    marker = root / REPOSITORY_MARKER_NAME
    if marker.exists() and (marker.is_symlink() or not marker.is_file()):
        raise ValidationError("PCB project repository marker must be a regular file")
    if marker.is_file():
        _validate_marker(marker)
    else:
        atomic_write_json(
            marker,
            {
                "schema": REPOSITORY_MARKER_SCHEMA,
                "version": REPOSITORY_VERSION,
                "created_at": utc_timestamp(),
            },
            mode=0o600,
        )
    config = repository_config_path()
    atomic_write_json(
        config,
        {
            "schema": REPOSITORY_CONFIG_SCHEMA,
            "version": REPOSITORY_VERSION,
            "root": str(root),
            "updated_at": utc_timestamp(),
        },
        mode=0o600,
    )
    return ProjectRepository(root=root, source="configured", configured_now=True)


def current_repository(*, create_default: bool = True) -> ProjectRepository:
    """Resolve the persisted repository, optionally creating the first default."""

    config = repository_config_path()
    if config.exists():
        if config.is_symlink() or not config.is_file():
            raise ValidationError(
                "PCB project repository config must be a regular file"
            )
        payload = load_json_limited(config, REPOSITORY_CONFIG_LIMIT)
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "version",
            "root",
            "updated_at",
        }:
            raise ValidationError("PCB project repository config is invalid")
        if (
            payload["schema"] != REPOSITORY_CONFIG_SCHEMA
            or payload["version"] != REPOSITORY_VERSION
            or not isinstance(payload["root"], str)
            or not isinstance(payload["updated_at"], str)
        ):
            raise ValidationError("PCB project repository config is invalid")
        root = _open_repository(payload["root"])
        return ProjectRepository(root=root, source="configured")
    if not create_default:
        raise ValidationError(
            "PCB project repository is not configured; run `pcbdraft repository /path/to/repository`"
        )
    # Do not strand projects created by the pre-repository application home.
    # The location is recorded as-is rather than moving any user files.
    legacy = legacy_repository_path()
    if not legacy.is_symlink() and legacy.is_dir() and (legacy / "projects").is_dir():
        repository = configure_repository(legacy)
        return ProjectRepository(
            root=repository.root,
            source="migrated-legacy",
            configured_now=True,
        )
    repository = configure_repository(default_repository_path())
    return ProjectRepository(
        root=repository.root,
        source="first-run-default",
        configured_now=True,
    )


def explicit_repository(directory: str | Path) -> ProjectRepository:
    """Open an explicit automation/test repository without changing user config."""

    root = _prepare_repository(directory)
    return ProjectRepository(root=root, source="explicit")


def _prepare_repository(directory: str | Path) -> Path:
    raw = Path(directory).expanduser()
    if not str(raw).strip() or "\x00" in str(raw):
        raise ValidationError("PCB project repository path is invalid")
    if raw.exists() and raw.is_symlink():
        raise ValidationError("PCB project repository must not be a symlink")
    make_directory(raw)
    root = raw.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("PCB project repository must be a directory")
    return root


def _open_repository(directory: str) -> Path:
    raw = Path(directory).expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise ValidationError(
            "configured PCB project repository is unavailable; "
            "run `pcbdraft repository /new/location` to choose a new location"
        )
    root = raw.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("configured PCB project repository is invalid")
    marker = root / REPOSITORY_MARKER_NAME
    if marker.exists():
        if marker.is_symlink() or not marker.is_file():
            raise ValidationError(
                "PCB project repository marker must be a regular file"
            )
        _validate_marker(marker)
    return root


def _validate_marker(marker: Path) -> None:
    try:
        payload = load_json_limited(marker, REPOSITORY_CONFIG_LIMIT)
    except PCBDraftError as exc:
        raise ValidationError("PCB project repository marker is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "version",
        "created_at",
    }:
        raise ValidationError("PCB project repository marker is invalid")
    if (
        payload["schema"] != REPOSITORY_MARKER_SCHEMA
        or payload["version"] != REPOSITORY_VERSION
        or not isinstance(payload["created_at"], str)
    ):
        raise ValidationError("PCB project repository marker is invalid")
