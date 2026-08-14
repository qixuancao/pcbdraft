"""Canonical locations for immutable resources shipped with PCBDraft."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PACKAGE_ROOT / "data"


def data_path(*parts: str) -> Path:
    """Return a path below the installed package's immutable data directory."""

    return DATA_ROOT.joinpath(*parts)
