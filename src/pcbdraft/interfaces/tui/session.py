"""Small, non-secret terminal-session pointer for project recovery."""

from __future__ import annotations

from pathlib import Path

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.core.runs import utc_timestamp

TUI_SESSION_SCHEMA = "pcbdraft-tui-session"
TUI_SESSION_VERSION = 1
TUI_SESSION_LIMIT = 64 * 1024


class TuiSessionStore:
    """Remember only the last opened local project, never prompts or credentials."""

    def __init__(self, workspace: Path) -> None:
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValidationError("TUI session workspace must be a directory")
        self.path = workspace / "tui-session.json"

    def load_project_id(self, valid_project_ids: set[str]) -> str | None:
        if self.path.is_symlink() or not self.path.is_file():
            return None
        try:
            value = load_json_limited(self.path, TUI_SESSION_LIMIT)
        except PCBDraftError:
            return None
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "version",
            "project_id",
            "updated_at",
        }:
            return None
        if (
            value["schema"] != TUI_SESSION_SCHEMA
            or value["version"] != TUI_SESSION_VERSION
            or not isinstance(value["project_id"], str)
            or not isinstance(value["updated_at"], str)
        ):
            return None
        return value["project_id"] if value["project_id"] in valid_project_ids else None

    def save_project_id(self, project_id: str) -> None:
        if not project_id or len(project_id) > 256 or "\x00" in project_id:
            raise ValidationError("TUI session project id is invalid")
        atomic_write_json(
            self.path,
            {
                "schema": TUI_SESSION_SCHEMA,
                "version": TUI_SESSION_VERSION,
                "project_id": project_id,
                "updated_at": utc_timestamp(),
            },
            mode=0o600,
        )
