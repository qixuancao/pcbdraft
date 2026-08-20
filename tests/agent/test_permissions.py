from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from pcbdraft.agent.permissions import (
    PCBToolGateway,
    PermissionBroker,
    ToolPermissionError,
)
from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    PCBToolExecutor,
    ToolCall,
)
from pcbdraft.core.errors import ValidationError


def _schema_example(schema: Mapping[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)):
        return enum[0]
    any_of = schema.get("anyOf")
    if isinstance(any_of, (list, tuple)):
        return _schema_example(any_of[0])
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise AssertionError("object schema has no properties")
        return {name: _schema_example(value) for name, value in properties.items()}
    if schema_type == "array":
        return (
            [_schema_example(schema["items"])]
            if int(schema.get("minItems", 0)) > 0
            else []
        )
    if schema_type == "number":
        return 1.0
    if schema_type == "integer":
        return 1
    if schema_type == "boolean":
        return True
    if schema_type == "null":
        return None
    return "x"


def _call(
    name: str,
    *,
    source: str = "runtime_policy",
    arguments: dict[str, object] | None = None,
) -> ToolCall:
    return ToolCall(
        name=name,
        project_id="board",
        source=source,  # type: ignore[arg-type]
        arguments=arguments or {},
        baseline_revision=4,
    )


class PermissionBrokerTests(unittest.TestCase):
    def test_every_flat_tool_has_one_consistent_mode_decision(self) -> None:
        for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
            call = _call(
                spec.name,
                source="model",
                arguments=_schema_example(spec.input_schema),
            )
            expected_review = (
                "ask"
                if spec.effect == "authoritative_write" or spec.risk == "high"
                else "allow"
            )
            expected_read_only = "allow" if spec.effect == "read" else "deny"
            for mode, expected in (
                ("workspace", "allow"),
                ("review", expected_review),
                ("read_only", expected_read_only),
            ):
                with self.subTest(tool=spec.name, mode=mode):
                    self.assertEqual(
                        PermissionBroker(mode).decide(call, spec).action,  # type: ignore[arg-type]
                        expected,
                    )

    def test_workspace_mode_allows_in_scope_project_writes(self) -> None:
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("apply_candidate")

        verdict = PermissionBroker("workspace").decide(_call("apply_candidate"), spec)

        self.assertEqual(verdict.action, "allow")
        self.assertIn("current PCB project", verdict.reason)

    def test_review_mode_asks_only_for_autonomous_authoritative_write(self) -> None:
        broker = PermissionBroker("review")
        generate = DEFAULT_PCB_TOOL_REGISTRY.resolve("generate_candidate")
        validate = DEFAULT_PCB_TOOL_REGISTRY.resolve("validate")

        self.assertEqual(
            broker.decide(_call("generate_candidate"), generate).action,
            "ask",
        )
        self.assertEqual(broker.decide(_call("validate"), validate).action, "allow")
        self.assertEqual(
            broker.decide(_call("generate_candidate", source="user"), generate).action,
            "ask",
        )
        self.assertEqual(
            broker.decide(
                _call("generate_candidate", source="user"),
                generate,
                trusted_user_action=True,
            ).action,
            "allow",
        )

    def test_trusted_user_action_cannot_be_claimed_by_runtime_source(self) -> None:
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("generate_candidate")

        with self.assertRaisesRegex(ValidationError, "trusted user action"):
            PermissionBroker("review").decide(
                _call("generate_candidate"),
                spec,
                trusted_user_action=True,
            )

    def test_permission_decision_rejects_a_mismatched_spec(self) -> None:
        generate = DEFAULT_PCB_TOOL_REGISTRY.resolve("generate_candidate")

        with self.assertRaisesRegex(ValidationError, "mismatched tool spec"):
            PermissionBroker().decide(_call("validate"), generate)

    def test_read_only_mode_denies_every_durable_tool(self) -> None:
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("validate")

        verdict = PermissionBroker("read_only").decide(_call("validate"), spec)

        self.assertEqual(verdict.action, "deny")

    def test_read_only_mode_allows_factual_flat_reads_only(self) -> None:
        broker = PermissionBroker("read_only")

        self.assertEqual(
            broker.decide(
                _call("inspect_design"),
                DEFAULT_PCB_TOOL_REGISTRY.resolve("inspect_design"),
            ).action,
            "allow",
        )
        self.assertEqual(
            broker.decide(
                _call(
                    "add_component",
                    arguments={
                        "value": {
                            "id": "part",
                            "reference": "R1",
                            "part_id": "part",
                            "value": "1k",
                            "block_id": "block",
                        }
                    },
                ),
                DEFAULT_PCB_TOOL_REGISTRY.resolve("add_component"),
            ).action,
            "deny",
        )

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "permission mode"):
            PermissionBroker("everything")  # type: ignore[arg-type]

    def test_gateway_stops_review_and_read_only_before_dispatch(self) -> None:
        class Service:
            def __init__(self) -> None:
                self.view = {
                    "project": {"id": "board", "status": "awaiting_confirmation"},
                    "state": {"revision": 4},
                }
                self.calls: list[str] = []

            def open_project(self, project_id: str) -> dict[str, object]:
                if project_id != "board":
                    raise AssertionError("unexpected project")
                return self.view

            def confirm_project(
                self, *args: object, **kwargs: object
            ) -> dict[str, object]:
                del args, kwargs
                self.calls.append("confirm")
                return self.view

        for mode in ("review", "read_only"):
            with self.subTest(mode=mode):
                service = Service()
                gateway = PCBToolGateway(
                    PCBToolExecutor(service),  # type: ignore[arg-type]
                    PermissionBroker(mode),  # type: ignore[arg-type]
                )
                with self.assertRaises(ToolPermissionError):
                    gateway.execute(_call("generate_candidate"), timeout=12.0)
                self.assertEqual(service.calls, [])

    def test_gateway_enforces_flat_read_evidence_and_authoritative_boundaries(
        self,
    ) -> None:
        class Service:
            def __init__(self) -> None:
                self.view = {
                    "project": {"id": "board", "status": "generated"},
                    "state": {"revision": 4},
                }
                self.calls: list[str] = []

            def open_project(self, project_id: str) -> dict[str, object]:
                if project_id != "board":
                    raise AssertionError("unexpected project")
                return self.view

            def execute_pcb_tool(
                self,
                project_id: str,
                tool_name: str,
                arguments: dict[str, object],
                *,
                timeout: float,
                expected_revision: int,
            ) -> dict[str, object]:
                del project_id, arguments, timeout, expected_revision
                self.calls.append(tool_name)
                return self.view

        cases = (
            ("review", "inspect_design", True),
            ("review", "run_drc", True),
            ("review", "set_board_outline", False),
            ("read_only", "inspect_design", True),
            ("read_only", "run_drc", False),
            ("workspace", "set_board_outline", True),
        )
        for mode, name, allowed in cases:
            with self.subTest(mode=mode, tool=name):
                service = Service()
                gateway = PCBToolGateway(
                    PCBToolExecutor(service),  # type: ignore[arg-type]
                    PermissionBroker(mode),  # type: ignore[arg-type]
                )
                call = _call(
                    name,
                    source="model",
                    arguments=_schema_example(
                        DEFAULT_PCB_TOOL_REGISTRY.resolve(name).input_schema
                    ),
                )
                if allowed:
                    result = gateway.execute(call, timeout=12.0)
                    self.assertEqual(result.call.name, name)
                    self.assertEqual(service.calls, [name])
                else:
                    with self.assertRaises(ToolPermissionError):
                        gateway.execute(call, timeout=12.0)
                    self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
