"""Native uv lookup for optional tool helpers.

The package installer owns Python and dependencies. This module never downloads
an installer, self-updates uv, replaces a venv, or sweeps old runtime trees.
"""

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from pcbdraft.core.resources import PACKAGE_ROOT
from pcbdraft.core.runtime_environment import get_runtime_home
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle


def managed_uv_path() -> Path:
    return (
        get_runtime_home()
        / "bin"
        / ("uv.exe" if platform.system() == "Windows" else "uv")
    )


def resolve_uv() -> str | None:
    path = managed_uv_path()
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def ensure_uv(*, repair_observer=None) -> str | None:
    """Return an already provisioned native uv, or no installer.

    Optional-tool callers retain their existing missing-dependency handling.
    Provisioning is deliberately not an implicit side effect of this lookup.
    """
    return resolve_uv()


def managed_python_install_dir(project_root: Path | None = None) -> Path:
    root = project_root if project_root is not None else PACKAGE_ROOT
    return Path(root) / ".pcbdraft-runtime" / "python"


def managed_python_env(
    project_root: Path | None = None,
    *,
    install_dir: Path | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    for key in (
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "UV_PROJECT_ENVIRONMENT",
        "UV_NO_MANAGED_PYTHON",
        "UV_PYTHON",
        "UV_PYTHON_DOWNLOADS",
        "UV_SYSTEM_PYTHON",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        env.pop(key, None)
    env.update(
        {
            "UV_MANAGED_PYTHON": "1",
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_PYTHON_INSTALL_REGISTRY": "0",
            "UV_PYTHON_INSTALL_DIR": str(
                install_dir
                if install_dir is not None
                else managed_python_install_dir(project_root)
            ),
        }
    )
    return env


@dataclass(frozen=True)
class RuntimeRepairResult:
    status: str
    detail: str = ""
    sqlite_before: str = ""
    sqlite_after: str = ""
    backup_venv: Path | None = None

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"


update_managed_uv = unsupported_lifecycle
repair_vulnerable_runtime = unsupported_lifecycle
