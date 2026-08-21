from __future__ import annotations

import sys
import types
import unittest
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.hermes_tools import (
    _execute_tool,
    _handler,
    _set_service,
    register_all_pcb_tools,
    set_current_project_id,
)
from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.core.errors import PCBDraftError


def _view(project_id: str) -> dict[str, Any]:
    return {
        "project": {
            "id": project_id,
            "name": project_id,
            "status": "generated",
            "design_revision": 1,
        },
        "state": {"revision": 1},
        "artifacts": {},
        "conversation": {},
        "events": [],
    }


class FakePCBService:
    def __init__(self) -> None:
        self.created = 0
        self.calls: list[tuple[Any, ...]] = []

    def inspect_installed_library(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("library", tool_name, arguments))
        return {"symbols": ["Device:LED"]}

    def create_empty_project(self, name: str) -> dict[str, Any]:
        self.created += 1
        self.calls.append(("create", name))
        return _view(f"created-{self.created}")

    def open_project(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("open", project_id))
        return _view(project_id)

    def execute_pcb_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.calls.append(("execute", project_id, tool_name, arguments))
        return _view(project_id)


class HermesToolRegistrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _set_service(None)
        set_current_project_id(None)

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

    def test_installed_library_reads_need_no_project_or_session(self) -> None:
        service = FakePCBService()
        _set_service(service)
        set_current_project_id(None)

        result = _execute_tool(
            DEFAULT_PCB_TOOL_REGISTRY.resolve("search_symbols"),
            {"query": "LED"},
            session_id="",
        )

        self.assertIsNone(result["project_id"])
        self.assertEqual(result["result"]["symbols"], ["Device:LED"])
        self.assertEqual(
            service.calls, [("library", "search_symbols", {"query": "LED"})]
        )

    def test_failed_installed_library_read_never_opens_current_project(self) -> None:
        service = FakePCBService()
        _set_service(service)
        set_current_project_id("trusted-project-a")
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("search_symbols")

        with patch.object(
            service,
            "inspect_installed_library",
            side_effect=PCBDraftError("installed symbol lookup failed"),
        ):
            result = _handler(spec)({"query": "LED"}, session_id="session-a")

        self.assertNotIn("project_id", result)
        self.assertNotIn("trusted-project-a", result)
        self.assertEqual(service.calls, [])

    def test_model_created_project_is_session_bound_until_trusted_switch(self) -> None:
        service = FakePCBService()
        _set_service(service)
        set_current_project_id(None)
        create = DEFAULT_PCB_TOOL_REGISTRY.resolve("create_project")
        inspect = DEFAULT_PCB_TOOL_REGISTRY.resolve("inspect_project")

        created = _execute_tool(create, {"name": "Board B"}, session_id="session-b")
        self.assertEqual(created["project_id"], "created-1")
        with self.assertRaisesRegex(PCBDraftError, "already bound"):
            _execute_tool(create, {"name": "Replacement"}, session_id="session-b")

        inspected = _execute_tool(inspect, {}, session_id="session-b")
        self.assertEqual(inspected["project_id"], "created-1")
        set_current_project_id("trusted-project-a")
        switched = _execute_tool(inspect, {}, session_id="session-b")
        self.assertEqual(switched["project_id"], "trusted-project-a")


if __name__ == "__main__":
    unittest.main()
