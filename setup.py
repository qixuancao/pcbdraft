"""Setuptools build hook that ships the vendored Hermes runtime in the wheel.

The trimmed Hermes runtime is kept under ``vendor/hermes`` (the git-tracked
source of truth, diffable against upstream) and is intentionally outside
``src/``.  The default setuptools wheel build only packages ``pcbdraft``, so
this hook copies the runtime into the build tree as wheel data
(``pcbdraft/data/vendor/hermes``), where
:func:`pcbdraft.core.hermes_paths.hermes_vendor_dir` finds it in installed
environments.  ``pyproject.toml`` remains the single project metadata source;
this file only contributes the ``build_py`` command class.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

#: Files/directories excluded from the shipped runtime.  The .venv alone is
#: ~115M of the 158M checkout; __pycache__ and upstream tooling/docs are also
#: irrelevant to a PCB terminal runtime.
_VENDOR_IGNORE = shutil.ignore_patterns(
    ".venv",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".github",
    "docs",
    "locales",
    "native",
    "hermes_agent.egg-info",
    "uv.lock",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.windows.yml",
    "setup-hermes.sh",
    "flake.lock",
    "flake.nix",
    "eslint.config.shared.mjs",
    ".git*",
    ".env*",
    ".nvmrc",
    ".npmrc",
    ".python-version",
    ".prettierrc",
    ".prettierignore",
    ".hadolint.yaml",
    ".dockerignore",
    ".envrc",
    ".mailmap",
    ".coderabbit.yaml",
)


class BuildPyWithVendor(build_py):
    """Standard build plus a copy of vendor/hermes into the wheel data dir."""

    def run(self) -> None:
        super().run()
        source = Path(__file__).resolve().parent / "vendor" / "hermes"
        if not (source / "cli.py").is_file():
            return  # No vendored runtime available; nothing to ship.
        destination = Path(self.build_lib) / "pcbdraft" / "data" / "vendor" / "hermes"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, ignore=_VENDOR_IGNORE)


setup(cmdclass={"build_py": BuildPyWithVendor})
