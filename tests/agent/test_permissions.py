from __future__ import annotations

import unittest

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


def _call(name: str, *, source: str = "runtime_policy") -> ToolCall:
    return ToolCall(
        name=name,
        project_id="board",
        source=source,  # type: ignore[arg-type]
        arguments={},
        baseline_revision=4,
    )


class PermissionBrokerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
