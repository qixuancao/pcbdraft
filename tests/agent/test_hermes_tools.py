from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from pcbdraft.agent.hermes_tools import register_all_pcb_tools
from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY


class HermesToolRegistrationTests(unittest.TestCase):
    def test_registration_exposes_only_the_canonical_flat_pcb_toolset(self) -> None:
        registrations: list[dict[str, object]] = []
        registry = types.SimpleNamespace(
            register=lambda **kwargs: registrations.append(kwargs)
        )
        tools_package = types.ModuleType("tools")
        tools_package.__path__ = []  # type: ignore[attr-defined]
        registry_module = types.ModuleType("tools.registry")
        registry_module.registry = registry  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {"tools": tools_package, "tools.registry": registry_module},
        ):
            register_all_pcb_tools(permission_mode="read_only")

        self.assertEqual(
            [item["name"] for item in registrations],
            [spec.external_name for spec in DEFAULT_PCB_TOOL_REGISTRY.specs],
        )
        self.assertEqual({item["toolset"] for item in registrations}, {"pcbdraft"})
        self.assertTrue(
            all(
                "operation" not in item["schema"]["parameters"]["properties"]  # type: ignore[index]
                for item in registrations
            )
        )


if __name__ == "__main__":
    unittest.main()
