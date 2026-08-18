"""PCBDraft runtime package and stable public identity."""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, distribution, version
from typing import Any

try:
    __version__ = version("pcbdraft")
except PackageNotFoundError:  # Source tree before the distribution is installed.
    __version__ = "0.1.0"
PRODUCT_NAME = "PCBDraft"
DISTRIBUTION_NAME = "pcbdraft"
PRIMARY_CLI = "pcbdraft"
_IMMUTABLE_ARCHIVE_COMMIT = re.compile(
    r"https://(?:github\.com/qixuancao/pcbdraft/archive/|"
    r"codeload\.github\.com/qixuancao/pcbdraft/tar\.gz/)"
    r"([0-9a-fA-F]{40})\.tar\.gz\Z|"
    r"https://codeload\.github\.com/qixuancao/pcbdraft/tar\.gz/"
    r"([0-9a-fA-F]{40})\Z"
)


def build_identity() -> dict[str, Any]:
    """Return bounded PEP 610 provenance for receipts and diagnostics."""

    commit: str | None = None
    try:
        raw = distribution(DISTRIBUTION_NAME).read_text("direct_url.json")
        document = json.loads(raw) if raw else {}
        value = document.get("vcs_info", {}).get("commit_id")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
            commit = value.casefold()
        if commit is None:
            archive_url = document.get("url")
            match = (
                _IMMUTABLE_ARCHIVE_COMMIT.fullmatch(archive_url)
                if isinstance(archive_url, str)
                else None
            )
            if match is not None:
                commit = (match.group(1) or match.group(2)).casefold()
    except (PackageNotFoundError, AttributeError, TypeError, json.JSONDecodeError):
        pass
    return {"version": __version__, "commit": commit}


# Release 1.0 exposed most implementation modules directly below ``pcbdraft``.
# The implementation now lives in responsibility-focused subpackages, while a
# lazy import hook keeps those historical imports working for downstream users.
from ._compat import install_moved_module_aliases as _install_moved_module_aliases

_install_moved_module_aliases()
del _install_moved_module_aliases
