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


def _schema_example(schema: Mapping[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), (list, tuple)):
        return schema["enum"][0]
    if isinstance(schema.get("anyOf"), (list, tuple)):
        return _schema_example(schema["anyOf"][0])
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties") or {}
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


def _arguments_for(name: str) -> dict[str, Any]:
    return _schema_example(DEFAULT_PCB_TOOL_REGISTRY.resolve(name).input_schema)


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

    def execute_pcb_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "execute_pcb_tool",
                project_id,
                tool_name,
                arguments,
                timeout,
                expected_revision,
            )
        )
        return self.view


class PCBToolingTests(unittest.TestCase):
    def test_registry_is_closed_and_declares_effect_and_risk(self) -> None:
        specs = {spec.name: spec for spec in DEFAULT_PCB_TOOL_REGISTRY.specs}

        self.assertEqual(len(specs), 57)
        self.assertTrue(
            {
                "create_project",
                "inspect_design",
                "search_parts",
                "describe_part",
                "register_kicad_part",
                "add_component",
                "set_board_outline",
                "route_net",
                "run_drc",
                "render_board",
                "export_gerbers",
            }
            <= set(specs)
        )
        self.assertFalse({"list_projects", "open_project"} & set(specs))
        self.assertFalse(
            {
                "plan_request",
                "generate_candidate",
                "apply_design_change_set",
                "generate_fresh_project",
                "modify_existing_project",
            }
            & set(specs)
        )
        self.assertEqual(specs["inspect_design"].effect, "read")
        self.assertEqual(specs["set_board_outline"].effect, "authoritative_write")
        self.assertEqual(specs["set_board_outline"].risk, "high")
        self.assertEqual(
            DEFAULT_PCB_TOOL_REGISTRY.schema_fingerprint(),
            "34f249bc137275e4846aa47f4e805f9fc28ffdc20e5596d1e01c620cbc8fe372",
        )
        self.assertTrue(
            all(
                "operation" not in spec.input_schema["properties"]
                for spec in specs.values()
            )
        )
        encoded = json.dumps(
            [spec.input_schema for spec in specs.values()], sort_keys=True
        )
        self.assertNotIn("value_json", encoded)
        self.assertNotIn("changes_json", encoded)
        self.assertNotIn(
            "components",
            specs["add_block"].input_schema["properties"]["value"]["properties"],
        )
        self.assertNotIn(
            "endpoints",
            specs["add_net"].input_schema["properties"]["value"]["properties"],
        )
        self.assertNotIn(
            "provenance",
            specs["add_block"].input_schema["properties"]["value"]["properties"],
        )
        self.assertNotIn(
            "provenance",
            specs["add_constraint"].input_schema["properties"]["value"]["properties"],
        )
        outline = specs["set_board_outline"].input_schema["properties"]
        self.assertEqual(outline["width_mm"]["type"], "number")
        self.assertEqual(outline["height_mm"]["type"], "number")
        self.assertEqual(outline["width_mm"]["exclusiveMinimum"], 0)
        self.assertEqual(outline["height_mm"]["exclusiveMinimum"], 0)
        place = specs["place_footprint"].input_schema["properties"]
        self.assertEqual(place["side"]["enum"], ["front", "back"])
        via = specs["add_via"].input_schema["properties"]
        self.assertEqual(via["diameter_mm"]["exclusiveMinimum"], 0)
        self.assertEqual(via["drill_mm"]["exclusiveMinimum"], 0)
        self.assertEqual(via["from_layer"]["minimum"], 0)
        self.assertEqual(via["to_layer"]["minimum"], 0)

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
        for name, nested_property in (
            ("add_component", "reference"),
            ("add_constraint", "params"),
        ):
            with self.subTest(tool=name):
                spec = DEFAULT_PCB_TOOL_REGISTRY.resolve(name)
                first = spec.to_openai_responses_tool()
                value = first["parameters"]["properties"]["value"]
                original_type = value["properties"][nested_property]["type"]
                value["properties"][nested_property]["type"] = "integer"
                first["parameters"]["required"].clear()

                second = spec.to_openai_responses_tool()
                fresh_value = second["parameters"]["properties"]["value"]
                self.assertEqual(
                    fresh_value["properties"][nested_property]["type"],
                    original_type,
                )
                self.assertEqual(second["parameters"]["required"], ["value"])

                stored_schema = spec.arguments[0].schema
                with self.assertRaises(TypeError):
                    stored_schema["type"] = "string"  # type: ignore[index]
                with self.assertRaises(TypeError):
                    stored_schema["properties"][nested_property]["type"] = "integer"  # type: ignore[index]

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
            tools["pcb_inspect_project"]["annotations"],
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )
        self.assertEqual(
            tools["pcb_check_semantics"]["annotations"],
            {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        )
        self.assertTrue(tools["pcb_add_component"]["annotations"]["destructiveHint"])
        self.assertTrue(
            tools["pcb_set_board_outline"]["annotations"]["destructiveHint"]
        )
        self.assertFalse(tools["pcb_run_drc"]["annotations"]["openWorldHint"])

    def test_tool_specs_reject_invalid_or_hidden_authority_metadata(self) -> None:
        plan = DEFAULT_PCB_TOOL_REGISTRY.resolve("inspect_project")
        apply = DEFAULT_PCB_TOOL_REGISTRY.resolve("set_board_outline")

        with self.assertRaisesRegex(ValueError, "invalid effect"):
            replace(plan, effect="invalid")  # type: ignore[arg-type]
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
            DEFAULT_PCB_TOOL_REGISTRY.resolve("set_board_outline"), risk="medium"
        )
        specs = tuple(
            downgraded if spec.name == downgraded.name else spec
            for spec in PCB_TOOL_SPECS
        )

        with self.assertRaisesRegex(ValueError, "fixed handler authority"):
            PCBToolRegistry(specs)

    def test_flat_write_status_contracts_require_a_materialized_design(self) -> None:
        add_component = DEFAULT_PCB_TOOL_REGISTRY.resolve("add_component")
        run_drc = DEFAULT_PCB_TOOL_REGISTRY.resolve("run_drc")

        self.assertNotIn("draft", add_component.allowed_statuses)
        self.assertNotIn("awaiting_confirmation", run_drc.allowed_statuses)
        self.assertIn("generated", add_component.allowed_statuses)

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

    def test_flat_tool_dispatches_one_concrete_service_operation(self) -> None:
        service = ToolService(status="generated")
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="inspect_design",
            project_id="board",
            source="model",
            arguments={},
            baseline_revision=3,
        )

        result = executor.execute(call, timeout=12.0)

        self.assertEqual(
            service.calls,
            [("execute_pcb_tool", "board", "inspect_design", {}, 12.0, 3)],
        )
        self.assertEqual(result.spec.name, "inspect_design")

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

    def test_every_flat_write_rejects_a_stale_baseline_before_dispatch(self) -> None:
        for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
            if spec.effect == "read" or spec.name == "create_project":
                continue
            with self.subTest(tool=spec.name):
                service = ToolService(status="generated", revision=4)
                executor = PCBToolExecutor(service)  # type: ignore[arg-type]
                call = ToolCall(
                    name=spec.name,
                    project_id="board",
                    source="model",
                    arguments=_arguments_for(spec.name),
                    baseline_revision=3,
                )
                with self.assertRaisesRegex(ValidationError, "stale baseline revision"):
                    executor.execute(call, timeout=12.0)
                self.assertEqual(service.calls, [])

    def test_every_flat_write_rejects_disallowed_status_before_dispatch(self) -> None:
        for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
            if spec.effect == "read" or spec.name == "create_project":
                continue
            with self.subTest(tool=spec.name):
                service = ToolService(status="invalid-test-status")
                executor = PCBToolExecutor(service)  # type: ignore[arg-type]
                call = ToolCall(
                    name=spec.name,
                    project_id="board",
                    source="runtime_policy",
                    arguments=_arguments_for(spec.name),
                    baseline_revision=3,
                )

                with self.assertRaisesRegex(
                    ValidationError, "status is invalid-test-status"
                ):
                    executor.execute(call, timeout=12.0)

                self.assertEqual(service.calls, [])

    def test_valid_flat_call_returns_a_typed_audit_receipt(self) -> None:
        service = ToolService(status="generated")
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = call_from_view(
            "inspect_design",
            "board",
            source="model",
            arguments={},
            view=service.view,
        )

        result = executor.execute(call, timeout=12.0)

        self.assertEqual(
            service.calls,
            [("execute_pcb_tool", "board", "inspect_design", {}, 12.0, 3)],
        )
        self.assertEqual(result.call.source, "model")
        self.assertEqual(len(result.call.arguments_hash), 64)
        self.assertEqual(result.spec.name, "inspect_design")
        self.assertEqual(
            (result.before_status, result.before_revision), ("generated", 3)
        )
        self.assertEqual(
            (result.after_status, result.after_revision),
            ("generated", 3),
        )

    def test_every_flat_write_dispatches_one_fixed_revision_bound_service_call(
        self,
    ) -> None:
        for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
            if spec.effect == "read" or spec.name == "create_project":
                continue
            with self.subTest(tool=spec.name):
                service = ToolService(status="generated")
                executor = PCBToolExecutor(service)  # type: ignore[arg-type]
                arguments = _arguments_for(spec.name)
                result = executor.execute(
                    ToolCall(
                        name=spec.name,
                        project_id="board",
                        source="model",
                        arguments=arguments,
                        baseline_revision=3,
                    ),
                    timeout=12.0,
                )
                self.assertEqual(
                    service.calls,
                    [
                        (
                            "execute_pcb_tool",
                            "board",
                            spec.name,
                            arguments,
                            12.0,
                            3,
                        )
                    ],
                )
                self.assertEqual(result.spec, spec)
                self.assertEqual(result.call.name, spec.name)
                self.assertEqual(result.call.arguments, arguments)
                self.assertEqual(result.before_revision, 3)
                self.assertEqual(result.after_revision, 3)

    def test_protocol_name_dispatches_to_the_same_closed_internal_handler(self) -> None:
        service = ToolService()
        executor = PCBToolExecutor(service)  # type: ignore[arg-type]
        call = ToolCall(
            name="pcb_inspect_design",
            project_id="board",
            source="model",
            arguments={},
            baseline_revision=3,
        )

        result = executor.execute(call, timeout=12.0)

        self.assertEqual(
            service.calls,
            [("execute_pcb_tool", "board", "inspect_design", {}, 12.0, 3)],
        )
        self.assertEqual(result.spec.name, "inspect_design")
        self.assertEqual(result.spec.external_name, "pcb_inspect_design")

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

        with self.assertRaisesRegex(ValidationError, "audit-only"):
            executor.execute(call, timeout=12.0)
        self.assertEqual(service.calls, [])

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

        with self.assertRaisesRegex(ValidationError, "strict schema"):
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

        with self.assertRaisesRegex(ValidationError, "strict schema"):
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
