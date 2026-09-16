"""Stable compatibility facade for the legacy terminal implementation.

New terminal components should live in focused modules. Existing imports keep
working through this facade while ``legacy_app`` is decomposed incrementally.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from pcbdraft.interfaces.tui import legacy_app as _legacy_app

_DELEGATED_NAMES = frozenset(vars(_legacy_app))

# Keep the two primary entry points visible to readers and static analyzers.
# All other historical names, including private rendering helpers, are served
# lazily by ``__getattr__`` below.
TerminalApp = _legacy_app.TerminalApp
main = _legacy_app.main

# Some runtime checks and integrations use the historical class path. This
# metadata assignment does not create a wrapper: both modules expose the exact
# same class object.
TerminalApp.__module__ = __name__

# Match the public names that ``from ...app import *`` exposed before the
# implementation moved. Explicit private imports continue through __getattr__.
__all__ = [name for name in vars(_legacy_app) if not name.startswith("_")]


def __getattr__(name: str) -> Any:
    """Resolve historical names from the legacy implementation."""
    return getattr(_legacy_app, name)


def __dir__() -> list[str]:
    """Include delegated legacy names in interactive inspection."""
    return sorted(set(globals()) | set(dir(_legacy_app)))


class _CompatibilityModule(ModuleType):
    """Forward monkeypatches and assignments to legacy function globals."""

    _LOCAL_NAMES = frozenset(
        {
            "_legacy_app",
            "_DELEGATED_NAMES",
            "_CompatibilityModule",
            "__all__",
            "__class__",
            "__dict__",
            "__doc__",
            "__file__",
            "__getattr__",
            "__loader__",
            "__name__",
            "__package__",
            "__path__",
            "__spec__",
        }
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in self._LOCAL_NAMES and (
            name in _DELEGATED_NAMES or hasattr(_legacy_app, name)
        ):
            setattr(_legacy_app, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        delegated = name not in self._LOCAL_NAMES and hasattr(_legacy_app, name)
        if delegated:
            delattr(_legacy_app, name)
        try:
            super().__delattr__(name)
        except AttributeError:
            # Delegated attributes normally do not exist in the facade's own
            # namespace, so deleting the legacy attribute already did the job.
            if not delegated:
                raise


sys.modules[__name__].__class__ = _CompatibilityModule


if __name__ == "__main__":
    import fire

    fire.Fire(main)
