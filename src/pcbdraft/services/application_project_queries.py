"""Read-only project query surfaces composed into ``ApplicationService``.

Project creation, repository configuration, recovery, and every project-state
write remain in the host application.  The mixin uses host adapters for the
historical application-level lock and error patch points and deliberately does
not import :mod:`pcbdraft.services.application`.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from pathlib import Path
from typing import Any


class ApplicationProjectQueriesMixin:
    """List and project stable public views without mutating project records."""

    def list_projects(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for candidate in sorted(self.projects_root.iterdir()):
            if (
                candidate.name.startswith(".")
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
                continue
            try:
                project = self._open_path(candidate)
            except self._project_query_error_type():
                continue
            result.append(self._summary(project))
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def open_project(self, project_id: str) -> dict[str, Any]:
        return self._public_project(self._open(project_id))

    def try_open_project_snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        """Read a self-consistent public view without blocking live UI polling.

        Project records and managed design directories are updated under the
        project lock.  A live client must use the same lock or it can otherwise
        observe a conversation from one revision and state/design files from
        another.  Returning ``None`` when the writer is busy lets callers keep
        streaming events and retry on their next poll.
        """

        root = self._project_path(project_id)
        lock = self._project_query_resource_lock(root, self.locks_root, timeout=timeout)
        try:
            lock.acquire()
        except self._project_query_error_type() as exc:
            if "resource is locked by another runtime process" in str(exc):
                return None
            raise
        try:
            return self._public_project(self._open_path(root))
        finally:
            lock.release()

    def project_root(self, project_id: str) -> Path:
        """Return a validated application-owned root for internal adapters."""

        return self._open(project_id).root
