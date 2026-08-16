from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    PCB_TOOL_SPECS,
    MCPToolAnnotations,
    PCBToolExecutor,
    PCBToolRegistry,
    ToolCall,
    call_from_view,
)
from pcbdraft.core.errors import ValidationError


def _view(*, status: str = "draft", revision: int = 3) -> dict[str, Any]:
    return {
        "project": {"id": "board", "name": "Board", "status": status},
        "state": {"revision": revision},
    }


def _feedback(
    *, summary: str = "Fix deterministic errors", findings: list[str] | None = None
) -> dict[str, Any]:
    return {
        "schema": "pcbdraft-agent-repair-feedback",
        "version": 1,
        "phase": "validation",
        "attempt": 1,
        "summary": summary,
        "findings": findings or ["net N1 is open"],
    }


def _assert_all_object_schemas_are_strict(case: unittest.TestCase, value: Any) -> None:
    if isinstance(value, Mapping):
        if value.get("type") == "object":
            properties = value.get("properties")
            required = value.get("required")
            case.assertIsInstance(properties, Mapping)
            case.assertIsInstance(required, (list, tuple))
            if not isinstance(properties, Mapping) or not isinstance(
                required, (list, tuple)
            ):
                raise AssertionError("object schema shape is invalid")
            case.assertIs(value.get("additionalProperties"), False)
            case.assertEqual(set(required), set(properties))
            case.assertEqual(len(required), len(properties))
        for item in value.values():
            _assert_all_object_schemas_are_strict(case, item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_all_object_schemas_are_strict(case, item)


class ToolService:
    def __init__(self, *, status: str = "draft", revision: int = 3) -> None:
        self.view = _view(status=status, revision=revision)
        self.calls: list[tuple[Any, ...]] = []

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return self.view

    def send_message(
        self,
        project_id: str,
        message: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.calls.append(
            ("send_message", project_id, message, timeout, expected_revision)
        )
        self.view = _view(status="awaiting_confirmation", revision=4)
        return self.view

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.calls.append(
            ("confirm_project", project_id, validate, timeout, expected_revision)
        )
        self.view = _view(status="generated", revision=4)
        return self.view

    def validate_project(
        self, project_id: str, *, timeout: float, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append(("validate_project", project_id, timeout, expected_revision))
        return self.view

    def prepare_agent_repair(
        self,
        project_id: str,
        feedback: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "prepare_agent_repair",
                project_id,
                feedback,
                timeout,
                expected_revision,
            )
        )
        return self.view

    def apply_modification(
        self, project_id: str, *, timeout: float, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append(
            ("apply_modification", project_id, timeout, expected_revision)
        )
        return self.view

    def discard_modification(
        self, project_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append(("discard_modification", project_id, expected_revision))
        return self.view

    def undo_last_modification(
        self, project_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append(("undo_last_modification", project_id, expected_revision))
        return self.view

    def generate_project_previews(
        self, project_id: str, *, timeout: float, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append(
            ("generate_project_previews", project_id, timeout, expected_revision)
        )
        return self.view

    def build_release(
        self, project_id: str, *, timeout: float, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append(("build_release", project_id, timeout, expected_revision))
        return self.view


class PCBToolingTests(unittest.TestCase):
    def test_registry_is_closed_and_declares_effect_and_risk(self) -> None:
        specs = {spec.name: spec for spec in DEFAULT_PCB_TOOL_REGISTRY.specs}

        self.assertEqual(
            set(specs),
            {
                "plan_request",
                "generate_candidate",
                "validate",
                "repair_candidate",
                "apply_candidate",
                "discard_candidate",
                "undo_last_change",
                "render_previews",
                "build_release",
            },
        )
        self.assertEqual(specs["plan_request"].effect, "conversation_write")
        self.assertEqual(specs["apply_candidate"].effect, "authoritative_write")
        self.assertEqual(specs["apply_candidate"].risk, "high")

    def test_every_exported_object_schema_is_closed_and_fully_required(self) -> None:
        for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
            with self.subTest(tool=spec.name):
                schema = spec.input_schema
                _assert_all_object_schemas_are_strict(self, schema)
                self.assertEqual(
                    schema["required"],
                    [argument.name for argument in spec.arguments],
                )

        repair = DEFAULT_PCB_TOOL_REGISTRY.resolve("repair_candidate")
        feedback = repair.input_schema["properties"]["feedback"]
        self.assertEqual(
            feedback["required"],
            ["schema", "version", "phase", "attempt", "summary", "findings"],
        )
        self.assertFalse(feedback["additionalProperties"])

    def test_openai_strict_export_omits_unsupported_unique_items(self) -> None:
        exported = DEFAULT_PCB_TOOL_REGISTRY.openai_responses_tools()

        self.assertNotIn("uniqueItems", json.dumps(exported, sort_keys=True))

    def test_schema_exports_are_fresh_and_static_specs_are_immutable(self) -> None:
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("repair_candidate")
        first = spec.to_openai_responses_tool()
        first["parameters"]["properties"]["feedback"]["properties"]["summary"][
            "type"
        ] = "integer"
        first["parameters"]["required"].clear()

        second = spec.to_openai_responses_tool()
        feedback = second["parameters"]["properties"]["feedback"]
        self.assertEqual(feedback["properties"]["summary"]["type"], "string")
        self.assertEqual(second["parameters"]["required"], ["feedback"])

        stored_schema = spec.arguments[0].schema
        with self.assertRaises(TypeError):
            stored_schema["type"] = "string"  # type: ignore[index]
        with self.assertRaises(TypeError):
            stored_schema["properties"]["summary"]["type"] = "integer"  # type: ignore[index]

    def test_openai_and_mcp_exports_share_names_schema_and_descriptions(self) -> None:
        openai_tools = DEFAULT_PCB_TOOL_REGISTRY.openai_responses_tools()
        mcp_tools = DEFAULT_PCB_TOOL_REGISTRY.mcp_tools()

        self.assertEqual(len(openai_tools), len(DEFAULT_PCB_TOOL_REGISTRY.specs))
        self.assertEqual(
            [item["name"] for item in openai_tools],
            [item["name"] for item in mcp_tools],
        )
        self.assertTrue(all(item["name"].startswith("pcb_") for item in openai_tools))
        for openai_tool, mcp_tool in zip(openai_tools, mcp_tools, strict=True):
            self.assertEqual(openai_tool["type"], "function")
            self.assertIs(openai_tool["strict"], True)
            self.assertEqual(openai_tool["parameters"], mcp_tool["inputSchema"])
            self.assertEqual(openai_tool["description"], mcp_tool["description"])
            self.assertIn("Returns:", openai_tool["description"])
            self.assertIn("Errors:", openai_tool["description"])

    def test_mcp_annotations_conservatively_reflect_effect_and_risk(self) -> None:
        tools = {item["name"]: item for item in DEFAULT_PCB_TOOL_REGISTRY.mcp_tools()}

        self.assertEqual(
            tools["pcb_plan_request"]["annotations"],
            {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        )
        self.assertEqual(
            tools["pcb_validate"]["annotations"],
            {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        )
        self.assertTrue(
            tools["pcb_generate_candidate"]["annotations"]["destructiveHint"]
        )
        self.assertTrue(tools["pcb_apply_candidate"]["annotations"]["destructiveHint"])
        self.assertTrue(
            tools["pcb_discard_candidate"]["annotations"]["destructiveHint"]
        )
        self.assertTrue(tools["pcb_repair_candidate"]["annotations"]["openWorldHint"])

    def test_tool_specs_reject_invalid_or_hidden_authority_metadata(self) -> None:
        plan = DEFAULT_PCB_TOOL_REGISTRY.resolve("plan_request")
        apply = DEFAULT_PCB_TOOL_REGISTRY.resolve("apply_candidate")

        with self.assertRaisesRegex(ValueError, "invalid effect"):
            replace(plan, effect="read")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "invalid risk"):
            replace(plan, risk="extreme")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "1-64"):
            replace(plan, name="x" * 65)
        with self.assertRaisesRegex(ValueError, "stable pcb_"):
            replace(plan, external_name="pcb_" + ("x" * 61))
        with self.assertRaisesRegex(ValueError, "hide destructive"):
            replace(
                apply,
                annotations=MCPToolAnnotations(
                    read_only=False,
                    destructive=False,
                    idempotent=False,
                    open_world=False,
                ),
            )

    def test_registry_cannot_downgrade_a_fixed_handler_contract(self) -> None:
        downgraded = replace(
            DEFAULT_PCB_TOOL_REGISTRY.resolve("apply_candidate"), risk="medium"
        )
        specs = tuple(
            downgraded if spec.name == downgraded.name else spec
            for spec in PCB_TOOL_SPECS
        )

        with self.assertRaisesRegex(ValueError, "fixed handler authority"):
            PCBToolRegistry(specs)

    def test_registry_status_contracts_match_service_entry_states(self) -> None:
        generate = DEFAULT_PCB_TOOL_REGISTRY.resolve("generate_candidate")
        repair = DEFAULT_PCB_TOOL_REGISTRY.resolve("repair_candidate")

        self.assertNotIn("generated", generate.allowed_statuses)
        self.assertTrue(
            {"released", "release_failed", "interrupted"} <= repair.allowed_statuses
        )

    def test_unknown_tool_is_rejected_before_dispatch(self) -> None:
        service = ToolService()
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="run_shell",
            project_id="board",
            source="runtime_policy",
            arguments={},
            baseline_revision=3,
        )

        with self.assertRaisesRegex(ValidationError, "unknown PCB tool"):
            executor.execute(call, timeout=12.0)

        self.assertEqual(service.calls, [])

    def test_extra_argument_is_rejected_before_dispatch(self) -> None:
        service = ToolService()

        with self.assertRaisesRegex(ValidationError, "unexpected: shell"):
            ToolCall(
                name="plan_request",
                project_id="board",
                source="user",
                arguments={"message": "Build a sensor board", "shell": "whoami"},
                baseline_revision=3,
            )

        self.assertEqual(service.calls, [])

    def test_stale_baseline_is_rejected_before_dispatch(self) -> None:
        service = ToolService(revision=4)
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="plan_request",
            project_id="board",
            source="runtime_policy",
            arguments={"message": "Build a sensor board"},
            baseline_revision=3,
        )

        with self.assertRaisesRegex(ValidationError, "stale baseline revision"):
            executor.execute(call, timeout=12.0)

        self.assertEqual(service.calls, [])

    def test_disallowed_status_is_rejected_before_dispatch(self) -> None:
        service = ToolService(status="draft")
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="generate_candidate",
            project_id="board",
            source="runtime_policy",
            arguments={},
            baseline_revision=3,
        )

        with self.assertRaisesRegex(ValidationError, "status is draft"):
            executor.execute(call, timeout=12.0)

        self.assertEqual(service.calls, [])

    def test_valid_call_returns_a_typed_audit_receipt(self) -> None:
        service = ToolService()
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = call_from_view(
            "plan_request",
            "board",
            source="runtime_policy",
            arguments={"message": "Build a sensor board"},
            view=service.view,
        )

        result = executor.execute(call, timeout=12.0)

        self.assertEqual(
            service.calls,
            [("send_message", "board", "Build a sensor board", 12.0, 3)],
        )
        self.assertEqual(result.call.source, "runtime_policy")
        self.assertEqual(len(result.call.arguments_hash), 64)
        self.assertEqual(result.spec.name, "plan_request")
        self.assertEqual((result.before_status, result.before_revision), ("draft", 3))
        self.assertEqual(
            (result.after_status, result.after_revision),
            ("awaiting_confirmation", 4),
        )

    def test_apply_forwards_timeout_and_bound_revision_to_service_cas(self) -> None:
        service = ToolService(status="change_ready")
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="apply_candidate",
            project_id="board",
            source="runtime_policy",
            arguments={},
            baseline_revision=3,
        )

        executor.execute(call, timeout=12.0)

        self.assertEqual(service.calls, [("apply_modification", "board", 12.0, 3)])

    def test_protocol_name_dispatches_to_the_same_closed_internal_handler(self) -> None:
        service = ToolService()
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="pcb_plan_request",
            project_id="board",
            source="runtime_policy",
            arguments={"message": "Build a sensor board"},
            baseline_revision=3,
        )

        result = executor.execute(call, timeout=12.0)

        self.assertEqual(
            service.calls,
            [("send_message", "board", "Build a sensor board", 12.0, 3)],
        )
        self.assertEqual(result.spec.name, "plan_request")
        self.assertEqual(result.spec.external_name, "pcb_plan_request")

    def test_repair_arguments_are_normalized_before_hash_and_dispatch(self) -> None:
        service = ToolService(status="generated")
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="repair_candidate",
            project_id="board",
            source="runtime_policy",
            arguments={
                "feedback": _feedback(
                    summary="  Fix\n deterministic errors  ",
                    findings=["  net   N1 is open  "],
                )
            },
            baseline_revision=3,
        )

        normalized = call.arguments["feedback"]
        self.assertEqual(normalized["summary"], "Fix deterministic errors")
        self.assertEqual(normalized["findings"], ["net N1 is open"])
        equivalent = ToolCall(
            name="pcb_repair_candidate",
            project_id="board",
            source="runtime_policy",
            arguments={"feedback": _feedback()},
            baseline_revision=3,
        )
        self.assertEqual(call.arguments_hash, equivalent.arguments_hash)

        executor.execute(call, timeout=12.0)

        self.assertEqual(service.calls[0][0], "prepare_agent_repair")
        self.assertEqual(service.calls[0][2], normalized)
        self.assertEqual(service.calls[0][-1], 3)

    def test_nested_argument_mutation_is_detected_before_dispatch(self) -> None:
        service = ToolService(status="generated")
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="repair_candidate",
            project_id="board",
            source="runtime_policy",
            arguments={"feedback": _feedback()},
            baseline_revision=3,
        )
        feedback = call.arguments["feedback"]
        feedback["summary"] = "tampered after approval"

        with self.assertRaisesRegex(ValidationError, "arguments changed"):
            executor.execute(call, timeout=12.0)

        self.assertEqual(service.calls, [])

    def test_duplicate_repair_findings_remain_rejected_locally(self) -> None:
        service = ToolService(status="generated")

        with self.assertRaisesRegex(ValidationError, "duplicates"):
            ToolCall(
                name="repair_candidate",
                project_id="board",
                source="runtime_policy",
                arguments={
                    "feedback": _feedback(
                        findings=["net N1 is open", "  net  N1 is open  "]
                    )
                },
                baseline_revision=3,
            )

        self.assertEqual(service.calls, [])

    def test_overlong_repair_text_is_rejected_instead_of_truncated(self) -> None:
        service = ToolService(status="generated")

        with self.assertRaisesRegex(ValidationError, "2048 character limit"):
            ToolCall(
                name="repair_candidate",
                project_id="board",
                source="runtime_policy",
                arguments={"feedback": _feedback(summary="x" * 2049)},
                baseline_revision=3,
            )

        self.assertEqual(service.calls, [])

    def test_nested_repair_arguments_remain_exact(self) -> None:
        service = ToolService(status="generated")

        with self.assertRaisesRegex(ValidationError, "invalid shape"):
            ToolCall(
                name="pcb_repair_candidate",
                project_id="board",
                source="runtime_policy",
                arguments={
                    "feedback": {
                        "schema": "pcbdraft-agent-repair-feedback",
                        "version": 1,
                        "phase": "validation",
                        "attempt": 1,
                        "summary": "Fix deterministic errors",
                        "findings": ["net N1 is open"],
                        "unexpected": "do not forward this",
                    }
                },
                baseline_revision=3,
            )

        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
