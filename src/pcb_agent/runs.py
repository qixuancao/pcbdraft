"""Private run-directory creation and receipt initialization."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from .errors import PcbAgentError


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def default_runs_parent() -> Path:
    return Path.home() / ".local" / "share" / "copperwright" / "runs"


def canonical_output_parent(value: str | None) -> Path:
    parent = Path(value).expanduser() if value else default_runs_parent()
    try:
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True)
        parent = parent.resolve(strict=True)
    except OSError as exc:
        raise PcbAgentError(f"cannot prepare output parent: {parent}") from exc
    if not parent.is_dir():
        raise PcbAgentError(f"output parent is not a directory: {parent}")
    return parent


def create_run(output_parent: str | None) -> tuple[str, Path]:
    parent = canonical_output_parent(output_parent)
    for _ in range(8):
        run_id = new_run_id()
        run_dir = parent / run_id
        try:
            run_dir.mkdir(mode=0o700)
            run_dir.chmod(0o700)
            return run_id, run_dir
        except FileExistsError:
            continue
        except OSError as exc:
            raise PcbAgentError(f"cannot create run directory under: {parent}") from exc
    raise PcbAgentError("could not allocate a unique run id")


def ensure_output_outside_project(project: Path, output_parent: str | None) -> None:
    """Prevent recursive staging copies when a custom output lives in the project."""
    raw = Path(output_parent).expanduser() if output_parent else default_runs_parent()
    try:
        candidate = raw.resolve(strict=False)
    except OSError as exc:
        raise PcbAgentError(f"cannot resolve output parent: {raw}") from exc
    if candidate == project or candidate.is_relative_to(project):
        raise PcbAgentError("output parent must be outside the source project")
