"""Lazy aliases for implementation modules moved after the 1.0 release."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from types import ModuleType
from typing import cast

MOVED_MODULES: dict[str, str] = {
    "pcbdraft.agent_capabilities": "pcbdraft.agent.capabilities",
    "pcbdraft.agent_design": "pcbdraft.agent.design",
    "pcbdraft.agent_events": "pcbdraft.agent.events",
    "pcbdraft.agent_repair": "pcbdraft.agent.repair",
    "pcbdraft.agent_runtime": "pcbdraft.agent.runtime",
    "pcbdraft.agent_tools": "pcbdraft.agent.tools",
    "pcbdraft.application": "pcbdraft.services.application",
    "pcbdraft.benchmark": "pcbdraft.verification.benchmark",
    "pcbdraft.blocks": "pcbdraft.domain.blocks",
    "pcbdraft.cli": "pcbdraft.interfaces.cli",
    "pcbdraft.component_qualification": "pcbdraft.domain.component_qualification",
    "pcbdraft.doctor": "pcbdraft.services.doctor",
    "pcbdraft.errors": "pcbdraft.core.errors",
    "pcbdraft.external_evidence": "pcbdraft.verification.evidence",
    "pcbdraft.gates": "pcbdraft.verification.gates",
    "pcbdraft.io": "pcbdraft.core.io",
    "pcbdraft.ir": "pcbdraft.domain.ir",
    "pcbdraft.jobs": "pcbdraft.services.jobs",
    "pcbdraft.kicad_pcb": "pcbdraft.kicad.pcb",
    "pcbdraft.kicad_schematic": "pcbdraft.kicad.schematic",
    "pcbdraft.kicad_support": "pcbdraft.kicad.support",
    "pcbdraft.locking": "pcbdraft.core.locking",
    "pcbdraft.managed": "pcbdraft.services.managed",
    "pcbdraft.model_api": "pcbdraft.model.api",
    "pcbdraft.model_config": "pcbdraft.model.config",
    "pcbdraft.model_review": "pcbdraft.model.review",
    "pcbdraft.operations": "pcbdraft.domain.operations",
    "pcbdraft.parts": "pcbdraft.domain.parts",
    "pcbdraft.patching": "pcbdraft.services.patching",
    "pcbdraft.pcbnew_worker": "pcbdraft.kicad.pcbnew_worker",
    "pcbdraft.placement": "pcbdraft.kicad.placement",
    "pcbdraft.previews": "pcbdraft.kicad.previews",
    "pcbdraft.process": "pcbdraft.core.process",
    "pcbdraft.profiles": "pcbdraft.domain.profiles",
    "pcbdraft.project": "pcbdraft.core.project",
    "pcbdraft.providers": "pcbdraft.model.providers",
    "pcbdraft.release": "pcbdraft.verification.release",
    "pcbdraft.report": "pcbdraft.verification.report",
    "pcbdraft.requirements": "pcbdraft.domain.requirements",
    "pcbdraft.routing": "pcbdraft.kicad.routing",
    "pcbdraft.runs": "pcbdraft.core.runs",
    "pcbdraft.scope": "pcbdraft.domain.scope",
    "pcbdraft.semantic_rules": "pcbdraft.domain.semantic_rules",
    "pcbdraft.sync": "pcbdraft.kicad.sync",
    "pcbdraft.terminal_text": "pcbdraft.interfaces.terminal_text",
    "pcbdraft.transactions": "pcbdraft.services.transactions",
    "pcbdraft.validation": "pcbdraft.verification.validation",
}


class _MovedModuleLoader(importlib.abc.Loader):
    """Return the canonical module object for one historical module name."""

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self._metadata: (
            tuple[
                str,
                object,
                str | None,
                importlib.machinery.ModuleSpec | None,
            ]
            | None
        ) = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self.target_name)
        self._metadata = (
            module.__name__,
            module.__loader__,
            module.__package__,
            module.__spec__,
        )
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Import machinery temporarily applies the alias spec to the returned
        # module. Restore canonical metadata so reloads and diagnostics remain
        # truthful while both sys.modules keys reference the exact same object.
        if self._metadata is not None:
            name, loader, package, spec = self._metadata
            module.__name__ = name
            module.__loader__ = cast(importlib.abc.Loader | None, loader)
            module.__package__ = package
            module.__spec__ = spec


class _MovedModuleFinder(importlib.abc.MetaPathFinder):
    """Resolve only the explicit historical module names above."""

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        target_name = MOVED_MODULES.get(fullname)
        if target_name is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _MovedModuleLoader(target_name),
            origin=f"moved:{target_name}",
        )


def install_moved_module_aliases() -> None:
    """Install the compatibility finder once for this interpreter."""

    if not any(isinstance(finder, _MovedModuleFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _MovedModuleFinder())
