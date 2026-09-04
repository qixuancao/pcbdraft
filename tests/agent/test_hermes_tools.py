from __future__ import annotations

import json
import statistics
import sys
import types
import unittest
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.hermes_tools import (
    _execute_tool,
    _handler,
    _set_service,
    model_tool_projection,
    register_all_pcb_tools,
    reset_session_project_context,
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
        "state": {"revision": 1, "design_revision": 1},
        "design": {
            "root": f"/tmp/private/{project_id}/design",
            "files": {"board": f"/tmp/private/{project_id}/board.kicad_pcb"},
        },
        "artifacts": {},
        "conversation": {},
        "events": [],
    }


class FakePCBService:
    def __init__(self) -> None:
        self.created = 0
        self.calls: list[tuple[Any, ...]] = []
        self.stage = "routing"
        self.next_tool_result: dict[str, Any] | None = None

    def inspect_engineering_stage(self, project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "live_revision": 1,
            "design_revision": 1,
            "evidence_source": "validation-run:none",
            "stage": self.stage,
            "release_gate_passed": self.stage == "release_gate",
            "blockers": [],
        }

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
        view = _view(project_id)
        if self.next_tool_result is not None:
            view["tool_result"] = self.next_tool_result
        return view


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

    def test_first_binding_details_are_isolated_per_session_and_project(self) -> None:
        service = FakePCBService()
        _set_service(service)
        set_current_project_id("trusted-project-a")
        inspect = DEFAULT_PCB_TOOL_REGISTRY.resolve("inspect_project")

        first_a = _execute_tool(inspect, {}, session_id="session-a")
        repeated_a = _execute_tool(inspect, {}, session_id="session-a")
        first_b = _execute_tool(inspect, {}, session_id="session-b")
        self.assertIn("binding", first_a)
        self.assertNotIn("binding", repeated_a)
        self.assertIn("binding", first_b)

        reset_session_project_context("session-a")
        rebound_a = _execute_tool(inspect, {}, session_id="session-a")
        self.assertIn("binding", rebound_a)

        set_current_project_id("trusted-project-b")
        switched_a = _execute_tool(inspect, {}, session_id="session-a")
        encoded = json.dumps(switched_a, ensure_ascii=False)
        self.assertIn("binding", switched_a)
        self.assertEqual(switched_a["project_id"], "trusted-project-b")
        self.assertNotIn("trusted-project-a", encoded)

    def test_normal_write_receipts_are_compact_and_binding_paths_do_not_repeat(
        self,
    ) -> None:
        service = FakePCBService()
        service.next_tool_result = {
            "operation": "route_net",
            "transaction_id": "20260823T120000Z-1234abcd",
            "changed": {"routes_added": 1, "components_changed": 0},
            "consistency_passed": True,
            "postconditions": [
                {"name": "native_consistency", "passed": True},
                {"name": "native_endpoint_connectivity", "passed": True},
            ],
            "progress_before": {"raw": "x" * 4_000},
            "progress_delta": {
                "classification": "improved",
                "decisive_metric": "unresolved_connection_count",
                "metrics": [
                    {
                        "name": "unresolved_connection_count",
                        "delta": -1,
                        "before": {"value": 2},
                        "after": {"value": 1},
                    }
                ],
            },
            "convergence": {"allowed": True, "action": "continue", "reason": None},
            "files": {"board": "/tmp/private/should-not-repeat.kicad_pcb"},
            "events": [{"message": "do not repeat"}],
        }
        _set_service(service)
        set_current_project_id("trusted-project-a")
        route = DEFAULT_PCB_TOOL_REGISTRY.resolve("route_net")

        summaries = [
            _execute_tool(
                route,
                {"net_id": "net_scl"},
                session_id="session-compact",
            )
            for _index in range(5)
        ]

        encoded = [
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in summaries
        ]
        self.assertLessEqual(
            statistics.median(len(item.encode()) for item in encoded), 1_024
        )
        self.assertIn("binding", summaries[0])
        self.assertNotIn("binding", summaries[1])
        for item in encoded[1:]:
            self.assertNotIn("/tmp/private", item)
            self.assertNotIn('"files"', item)
            self.assertNotIn('"events"', item)
            self.assertNotIn("progress_before", item)
        self.assertEqual(summaries[-1]["stage"], "routing")
        self.assertEqual(
            summaries[-1]["artifact_id"],
            "transaction:20260823T120000Z-1234abcd",
        )

    def test_representative_model_receipts_stay_below_the_normal_byte_target(
        self,
    ) -> None:
        service = FakePCBService()
        _set_service(service)
        set_current_project_id("trusted-project-a")
        permission = patch("pcbdraft.agent.hermes_tools._permission_mode", "workspace")
        permission.start()
        self.addCleanup(permission.stop)
        session_id = "session-representative-receipts"
        cases = (
            (
                "route_net",
                {"net_id": "net_scl"},
                {
                    "operation": "route_net",
                    "transaction_id": "20260823T120000Z-1234abcd",
                    "changed": {"routes_added": 1},
                    "consistency_passed": True,
                    "postconditions": [],
                    "progress_delta": {
                        "classification": "improved",
                        "decisive_metric": "unresolved_connection_count",
                        "metrics": [
                            {
                                "name": "unresolved_connection_count",
                                "delta": -1,
                            }
                        ],
                    },
                },
            ),
            (
                "connect_group",
                {
                    "connections": {
                        "entries": [
                            {
                                "net_id": "net_scl",
                                "component_id": "sensor",
                                "pin": "1",
                                "role": "signal",
                            }
                        ]
                    }
                },
                {
                    "operation": "connect_group",
                    "transaction_id": "20260823T120001Z-1234abcd",
                    "changed": {"connections_added": 1},
                    "consistency_passed": True,
                    "postconditions": [],
                },
            ),
            (
                "inspect_project",
                {},
                {"inspection": "project"},
            ),
            (
                "inspect_transaction",
                {"artifact_id": "transaction:20260823T120000Z-1234abcd"},
                {
                    "artifact_id": "transaction:20260823T120000Z-1234abcd",
                    "detail": {
                        "status": "failed",
                        "operation": "route_net",
                        "error_code": "native_connectivity_failed",
                    },
                },
            ),
            (
                "run_drc",
                {},
                {
                    "operation": "run_drc",
                    "state": "failed",
                    "outcome": "fail",
                    "production_ready": False,
                },
            ),
        )
        encoded: list[str] = []
        for name, arguments, tool_result in cases:
            service.next_tool_result = tool_result
            summary = _execute_tool(
                DEFAULT_PCB_TOOL_REGISTRY.resolve(name),
                arguments,
                session_id=session_id,
            )
            encoded.append(
                json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            )

        failure = PCBDraftError("/tmp/private/raw-diagnostic " + "x" * 4_000)
        failure.error_code = "native_connectivity_failed"  # type: ignore[attr-defined]
        failure.transaction_id = (  # type: ignore[attr-defined]
            "20260823T120002Z-1234abcd"
        )
        with patch.object(service, "execute_pcb_tool", side_effect=failure):
            encoded.append(
                _handler(DEFAULT_PCB_TOOL_REGISTRY.resolve("route_net"))(
                    {"net_id": "net_scl"}, session_id=session_id
                )
            )

        sizes = [len(item.encode("utf-8")) for item in encoded]
        self.assertLessEqual(statistics.median(sizes), 1_024)
        self.assertLessEqual(max(sizes), 1_024)
        for item in encoded[1:]:
            self.assertNotIn("/tmp/private", item)
            self.assertNotIn('"files"', item)
            self.assertNotIn('"events"', item)
            self.assertNotIn('"snapshot"', item)

    def test_model_run_drc_receipt_keeps_actionable_diagnostics(self) -> None:
        service = FakePCBService()
        _set_service(service)
        set_current_project_id("trusted-project-a")
        permission = patch("pcbdraft.agent.hermes_tools._permission_mode", "workspace")
        permission.start()
        self.addCleanup(permission.stop)
        service.next_tool_result = {
            "operation": "run_drc",
            "state": "completed",
            "outcome": "fail",
            "production_ready": False,
            "diagnostics": {
                "counts": {"error": 25, "warning": 0, "total": 25},
                "violation_count_seen": 25,
                "violations_truncated": True,
                "details_truncated": True,
                "remaining_violation_count": 5,
                "violations": [
                    {
                        "severity": "error",
                        "type": "unconnected_items",
                        "message": "Missing connection between items",
                        "items": [
                            {
                                "description": "F.Cu C1 pad 2 [/GND]",
                                "pos": {"x": float(index), "y": 15.5},
                            },
                            {
                                "description": "F.Cu U1 pad 2 [/GND]",
                                "pos": {"x": 16.3625, "y": 17.5},
                            },
                        ],
                    }
                    for index in range(20)
                ],
                "full_details_report": "validation/run/check.json",
                "raw_report": "validation/run/drc.raw.json",
            },
        }

        result = _execute_tool(
            DEFAULT_PCB_TOOL_REGISTRY.resolve("run_drc"),
            {},
            session_id="session-drc-diagnostics",
        )

        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["counts"]["error"], 25)
        self.assertEqual(len(diagnostics["violations"]), 20)
        self.assertTrue(diagnostics["details_truncated"])
        self.assertEqual(diagnostics["remaining_violation_count"], 5)
        self.assertEqual(diagnostics["violations"][0]["type"], "unconnected_items")
        self.assertEqual(
            diagnostics["violations"][0]["items"][1]["pos"],
            {"x": 16.3625, "y": 17.5},
        )
        self.assertEqual(diagnostics["raw_report"], "validation/run/drc.raw.json")

    def test_stage_cache_is_bound_to_revisions_and_evidence_source(self) -> None:
        class RevisingService(FakePCBService):
            def __init__(self) -> None:
                super().__init__()
                self.live_revision = 3
                self.design_revision = 2
                self.stage_calls = 0
                self.evidence_source = "validation-run:none"

            def open_project(self, project_id: str) -> dict[str, Any]:
                view = _view(project_id)
                view["state"]["revision"] = self.live_revision
                view["state"]["design_revision"] = self.design_revision
                view["project"]["design_revision"] = self.design_revision
                return view

            def inspect_engineering_stage(self, project_id: str) -> dict[str, Any]:
                self.stage_calls += 1
                return {
                    "project_id": project_id,
                    "live_revision": self.live_revision,
                    "design_revision": self.design_revision,
                    "evidence_source": self.evidence_source,
                    "stage": self.stage,
                    "release_gate_passed": False,
                    "blockers": [],
                }

        service = RevisingService()
        _set_service(service)
        set_current_project_id("trusted-project-a")

        first = model_tool_projection("session-revision")
        repeated = model_tool_projection("session-revision")
        self.assertEqual(first.stage, "routing")
        self.assertEqual(repeated.stage, "routing")
        self.assertEqual(service.stage_calls, 2)

        service.evidence_source = "validation-run:20260823T120000Z-1234abcd"
        service.stage = "erc_drc"
        evidence_source_changed = model_tool_projection("session-revision")
        self.assertEqual(evidence_source_changed.stage, "erc_drc")
        self.assertEqual(service.stage_calls, 3)

        service.live_revision += 1
        service.stage = "native_connectivity_confirmed"
        evidence_changed = model_tool_projection("session-revision")
        self.assertEqual(evidence_changed.stage, "native_connectivity_confirmed")
        self.assertEqual(service.stage_calls, 4)

        service.design_revision += 1
        service.stage = "placement"
        design_changed = model_tool_projection("session-revision")
        self.assertEqual(design_changed.stage, "placement")
        self.assertEqual(service.stage_calls, 5)

        service.evidence_source = "validation-run:20260823T120001Z-1234abcd"
        service.stage = "release_gate"
        release = model_tool_projection("session-revision")
        self.assertEqual(release.stage, "release_gate")
        self.assertIn("export_gerbers", {spec.name for spec in release.specs})

        service.evidence_source = "validation-run:20260823T120002Z-1234abcd"
        service.stage = "routing"
        no_longer_release = model_tool_projection("session-revision")
        self.assertEqual(no_longer_release.stage, "routing")
        self.assertNotIn(
            "export_gerbers", {spec.name for spec in no_longer_release.specs}
        )

    def test_failed_write_exposes_only_its_opaque_transaction_identity(self) -> None:
        service = FakePCBService()
        failure = PCBDraftError("native connectivity remains unresolved")
        failure.error_code = "native_connectivity_failed"  # type: ignore[attr-defined]
        failure.transaction_id = (  # type: ignore[attr-defined]
            "20260823T120000Z-1234abcd"
        )
        _set_service(service)
        set_current_project_id("trusted-project-a")
        route = DEFAULT_PCB_TOOL_REGISTRY.resolve("route_net")

        with patch.object(service, "execute_pcb_tool", side_effect=failure):
            result = json.loads(
                _handler(route)(
                    {"net_id": "net_scl"},
                    session_id="session-failed-receipt",
                )
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "native_connectivity_failed")
        self.assertEqual(
            result["artifact_id"],
            "transaction:20260823T120000Z-1234abcd",
        )
        self.assertNotIn("/", result["artifact_id"])


if __name__ == "__main__":
    unittest.main()
