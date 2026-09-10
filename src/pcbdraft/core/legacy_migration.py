"""Explicit, lossless migration of PCBDraft-owned legacy runtime state.

Startup only considers the product configuration directory's ``hermes`` child.
Project metadata migration requires an explicit ``--project PATH`` argument.
The independent Hermes installation is never discovered or inspected.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.io import atomic_write_json, read_text_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.platform_paths import pcbdraft_config_dir

RuntimeMigrationOutcome = Literal[
    "explicit",
    "fresh",
    "native",
    "migrated",
    "conflict",
]
_MIGRATION_RECORD = "runtime-migration.json"
_CONFLICT_RECORD = "runtime-migration-conflict.json"
_CONFIG_LIMIT = 4 * 1024 * 1024
# These are configuration documents, not credential stores. Never scan .env,
# auth.json, databases, or arbitrary JSON artifacts for textual replacements.
_PATH_CONFIG_NAMES = frozenset(
    {
        "config.json",
        "config.yaml",
        "config.yml",
        "settings.json",
        "settings.yaml",
        "settings.yml",
        "environment.json",
        # Mem0's setup persists oss.vector_store.config.path (Qdrant) here.
        # Holographic db_path and OpenViking ovcli_config_path live in the
        # already-covered config.yaml; Hindsight uses hindsight/config.json.
        "mem0.json",
    }
)
_PATH_KEYS = frozenset(
    {"path", "paths", "file", "directory", "dir", "root", "home", "cwd", "workspace"}
)


def _tree_entries(root: Path) -> Iterator[tuple[Path, int]]:
    """Walk metadata without following directory or file symlinks."""
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                mode = entry.stat(follow_symlinks=False).st_mode
                yield path, mode
                if stat.S_ISDIR(mode):
                    pending.append(path)


def _points_into(value: str, root: Path) -> bool:
    """Compare absolute/tilde paths lexically, never probing their targets."""
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        return False
    normalized = Path(os.path.normcase(os.path.abspath(candidate)))
    return normalized.is_relative_to(Path(os.path.normcase(os.path.abspath(root))))


def _has_saved_root_reference(document: object, root: Path) -> bool:
    pending = [(document, False)]
    seen: set[tuple[int, bool]] = set()
    while pending:
        value, is_path = pending.pop()
        if isinstance(value, str):
            if is_path and _points_into(value, root):
                return True
        elif isinstance(value, (dict, list)):
            # YAML aliases may be cyclic. Inspect each container at most once
            # per path/non-path context, without expanding aliases recursively.
            marker = (id(value), is_path)
            if marker in seen:
                continue
            seen.add(marker)
            if isinstance(value, list):
                pending.extend((item, is_path) for item in value)
            else:
                for key, item in value.items():
                    name = str(key).casefold()
                    path_key = name in _PATH_KEYS or name.endswith(
                        (
                            "_path",
                            "_paths",
                            "_dir",
                            "_directory",
                            "_root",
                            "_home",
                            "_file",
                        )
                    )
                    pending.append((item, is_path or path_key))
    return False


def _load_path_config(path: Path) -> object:
    text = read_text_limited(path, _CONFIG_LIMIT)
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise PCBDraftError(
            "cannot parse PCBDraft runtime configuration for migration; source retained"
        ) from None


def _preflight_runtime_references(legacy: Path, *, alias: Path) -> None:
    """Refuse a rename that would strand links or known persisted paths.

    Preserve the whole source tree and let the user repair the configuration
    explicitly. External links are inspected with readlink only and kept as-is.
    Both the configured spelling and canonical root are recognized without
    resolving arbitrary external link targets or saved configuration values.
    """
    roots = (legacy,) if alias == legacy else (legacy, alias)
    try:
        for path, mode in _tree_entries(legacy):
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if any(_points_into(target, root) for root in roots):
                    raise PCBDraftError(
                        "PCBDraft runtime migration blocked: a symbolic link refers "
                        "to the old runtime root; source retained. Repair the link "
                        "or set PCBDRAFT_RUNTIME_HOME to the existing directory."
                    )
            elif stat.S_ISREG(mode) and path.name in _PATH_CONFIG_NAMES:
                document = _load_path_config(path)
                if any(_has_saved_root_reference(document, root) for root in roots):
                    raise PCBDraftError(
                        "PCBDraft runtime migration blocked: a saved configuration "
                        "path refers to the old runtime root; source retained. "
                        "Repair the saved path or set PCBDRAFT_RUNTIME_HOME to "
                        "the existing directory."
                    )
    except PCBDraftError:
        raise
    except (OSError, ValueError, RuntimeError, RecursionError):
        raise PCBDraftError(
            "cannot preflight PCBDraft runtime references; source retained"
        ) from None


@dataclass(frozen=True)
class RuntimeHomeResolution:
    path: Path
    migration: RuntimeMigrationOutcome
    record_path: Path | None = None


def _write_record_once(path: Path, value: dict[str, object]) -> None:
    if path.is_symlink():
        raise PCBDraftError("PCBDraft runtime migration record is invalid")
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise PCBDraftError("PCBDraft runtime migration record is invalid")
        return
    atomic_write_json(path, value)


def _migration_record(outcome: str, *, source_retained: bool) -> dict[str, object]:
    return {
        "schema": "pcbdraft-runtime-migration",
        "version": 1,
        "outcome": outcome,
        "source_directory": "hermes",
        "target_directory": "runtime",
        "source_retained": source_retained,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def migrate_legacy_runtime_home() -> RuntimeHomeResolution:
    """Run once at startup, retaining both directories on a conflict.

    Explicit runtime overrides bypass filesystem migration. Known configuration
    path references and link targets are preflighted; credentials and database
    contents are never read, rewritten or merged.
    """

    if os.environ.get("PCBDRAFT_HERMES_HOME", "").strip():
        raise PCBDraftError(
            "PCBDRAFT_HERMES_HOME is no longer supported. Set PCBDRAFT_RUNTIME_HOME "
            "to your existing runtime directory and remove PCBDRAFT_HERMES_HOME "
            "before restarting; no data has been migrated."
        )
    explicit = os.environ.get("PCBDRAFT_RUNTIME_HOME", "").strip()
    if explicit:
        return RuntimeHomeResolution(Path(explicit).expanduser(), "explicit")
    configured_base = pcbdraft_config_dir()
    if configured_base.is_symlink():
        raise PCBDraftError("PCBDraft runtime migration directory is a symbolic link")
    # The platform-selected configuration root is the trust boundary. Resolve
    # its parents (e.g. macOS /var -> /private/var), not the migration children.
    # Keep the root itself subject to the same no-symlink rule as before.
    base = configured_base.parent.resolve() / configured_base.name
    native = base / "runtime"
    legacy = base / "hermes"
    # Reject links before following exists()/is_dir(), including dangling links.
    for directory in (base, native, legacy):
        if directory.is_symlink():
            raise PCBDraftError(
                "PCBDraft runtime migration directory is a symbolic link"
            )
    # Do not probe the two children separately outside the lock: a concurrent
    # rename between those probes can make an existing runtime look "fresh".
    if not base.exists():
        return RuntimeHomeResolution(native, "fresh")
    lock_parent = base / ".runtime-migration-locks"
    if lock_parent.is_symlink():
        raise PCBDraftError("PCBDraft runtime migration lock directory is invalid")
    with ResourceLock(base / "runtime-home", lock_parent, timeout=10.0):
        for directory in (native, legacy):
            if directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                raise PCBDraftError("PCBDraft runtime migration directory is invalid")
        if native.exists():
            if legacy.exists():
                record = base / _CONFLICT_RECORD
                _write_record_once(
                    record,
                    _migration_record("conflict", source_retained=True),
                )
                return RuntimeHomeResolution(native, "conflict", record)
            return RuntimeHomeResolution(native, "native")
        if not legacy.is_dir():
            return RuntimeHomeResolution(native, "fresh")
        _preflight_runtime_references(legacy, alias=configured_base / "hermes")
        try:
            legacy.rename(native)
        except OSError as exc:
            raise PCBDraftError(
                "cannot atomically migrate the PCBDraft runtime directory"
            ) from exc
        record = base / _MIGRATION_RECORD
        try:
            _write_record_once(
                record,
                _migration_record("migrated", source_retained=False),
            )
        except BaseException as exc:
            try:
                native.rename(legacy)
            except OSError as rollback_exc:
                raise PCBDraftError(
                    "PCBDraft runtime migration failed and rollback was incomplete"
                ) from rollback_exc
            raise PCBDraftError(
                "PCBDraft runtime migration failed and was rolled back"
            ) from exc
        return RuntimeHomeResolution(native, "migrated", record)


_PROJECT_ITEMS = ("environment.json", "skills", "plugins")


@dataclass(frozen=True)
class ProjectMigrationResult:
    """Paths relative to project metadata roots; source is always retained."""

    copied: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def _reject_link_components(path: Path, *, root: Path) -> None:
    """Reject links at/below the canonical selected project boundary."""
    relative = path.relative_to(root)
    component = root
    if component.is_symlink():
        raise PCBDraftError("project migration refuses symbolic-link paths")
    for part in relative.parts:
        component = component / part
        if component.is_symlink():
            raise PCBDraftError("project migration refuses symbolic-link paths")


def _project_entries(root: Path, *, source: bool) -> list[tuple[Path, int]]:
    """Inspect only the migration allowlist, never plans or sibling metadata."""
    result: list[tuple[Path, int]] = []
    for name in _PROJECT_ITEMS:
        path = root / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        entries = [(path, mode)]
        if stat.S_ISDIR(mode):
            entries.extend(_tree_entries(path))
        for entry, entry_mode in entries:
            if stat.S_ISLNK(entry_mode):
                raise PCBDraftError("project migration refuses symbolic links")
            if not (stat.S_ISDIR(entry_mode) or stat.S_ISREG(entry_mode)):
                raise PCBDraftError(
                    "project migration requires regular files and directories"
                )
            result.append((entry.relative_to(root), entry_mode))
        if source and (
            (name == "environment.json" and not stat.S_ISREG(mode))
            or (name != "environment.json" and not stat.S_ISDIR(mode))
        ):
            raise PCBDraftError(
                "project migration source has an invalid metadata layout"
            )
    return sorted(result, key=lambda item: (len(item[0].parts), str(item[0])))


def _same_file_contents(source: Path, target: Path) -> bool:
    if source.stat().st_size != target.stat().st_size:
        return False
    with source.open("rb") as left, target.open("rb") as right:
        while True:
            left_chunk = left.read(64 * 1024)
            right_chunk = right.read(64 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _copy_project_file(source: Path, target: Path, mode: int) -> None:
    """Exclusive creation also refuses destinations appearing after preflight."""
    with source.open("rb") as reader, target.open("xb") as writer:
        try:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
            target.chmod(stat.S_IMODE(mode))
        except BaseException:
            writer.close()
            target.unlink()
            raise


def _migrate_project_locked(project: Path) -> ProjectMigrationResult:
    source = project / ".hermes"
    target = project / ".pcbdraft"
    _reject_link_components(source, root=project)
    _reject_link_components(target, root=project)
    entries = _project_entries(source, source=True)
    _project_entries(target, source=False)
    directories: list[tuple[Path, int]] = []
    files: list[tuple[Path, int]] = []
    unchanged: list[str] = []
    conflicts: list[Path] = []
    for relative, mode in entries:
        if any(relative.is_relative_to(parent) for parent in conflicts):
            continue
        destination = target / relative
        try:
            destination_mode = destination.lstat().st_mode
        except FileNotFoundError:
            (directories if stat.S_ISDIR(mode) else files).append((relative, mode))
            continue
        if stat.S_IFMT(mode) != stat.S_IFMT(destination_mode):
            conflicts.append(relative)
        elif stat.S_ISREG(mode):
            if _same_file_contents(source / relative, destination):
                unchanged.append(relative.as_posix())
            else:
                conflicts.append(relative)
    # Preflight the entire selection before publishing any file. A conflict
    # leaves both trees as they were; the caller gets an actionable path list.
    if conflicts:
        return ProjectMigrationResult(
            unchanged=tuple(unchanged),
            conflicts=tuple(path.as_posix() for path in conflicts),
        )
    if not directories and not files:
        return ProjectMigrationResult(unchanged=tuple(unchanged))
    target.mkdir(mode=0o700, exist_ok=True)
    copied: list[str] = []
    for relative, _mode in directories:
        (target / relative).mkdir(mode=0o700)
        copied.append(relative.as_posix())
    for relative, mode in files:
        _copy_project_file(source / relative, target / relative, mode)
        copied.append(relative.as_posix())
    # Set modes after writing children, so a read-only source directory can
    # still be copied. Existing destination directory modes are never changed.
    for relative, mode in reversed(directories):
        (target / relative).chmod(stat.S_IMODE(mode))
    return ProjectMigrationResult(tuple(copied), tuple(unchanged))


def migrate_project(project: str | Path) -> ProjectMigrationResult:
    """Copy explicitly selected project .hermes metadata into .pcbdraft.

    Only environment.json, skills and plugins are eligible. Source files and
    plans remain untouched; identical destination files are idempotent, and
    any conflicting file/type blocks the entire copy. Links in selected source
    or destination trees (including dangling links) are rejected without following.
    No runtime or home-directory discovery is performed.
    """
    project = Path(os.path.abspath(Path(project).expanduser()))
    if project.is_symlink():
        raise PCBDraftError("project migration refuses symbolic-link paths")
    # Permit platform parent aliases, but never resolve away a symlink at the
    # explicitly selected project or below it.
    project = project.parent.resolve() / project.name
    _reject_link_components(project, root=project)
    if not project.is_dir():
        raise PCBDraftError("project migration requires an existing project directory")
    source = project / ".hermes"
    target = project / ".pcbdraft"
    for path in (source, target):
        _reject_link_components(path, root=project)
        if path.exists() and not path.is_dir():
            raise PCBDraftError("project metadata root must be a directory")
    # Reject bad selected trees before creating even the coordination lock.
    entries = _project_entries(source, source=True)
    _project_entries(target, source=False)
    if not entries:
        return ProjectMigrationResult()
    lock_parent = project / ".pcbdraft-migration-locks"
    _reject_link_components(lock_parent, root=project)
    with ResourceLock(target, lock_parent, timeout=10.0):
        return _migrate_project_locked(project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit PCBDraft project metadata migration"
    )
    parser.add_argument(
        "--project",
        required=True,
        metavar="PATH",
        help="project directory to migrate (source retained)",
    )
    args = parser.parse_args(argv)
    try:
        result = migrate_project(args.project)
    except PCBDraftError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("project migration failed; source retained", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "source_retained": True,
                "copied": result.copied,
                "unchanged": result.unchanged,
                "conflicts": result.conflicts,
            },
            sort_keys=True,
        )
    )
    return 1 if result.conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
