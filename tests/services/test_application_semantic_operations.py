from __future__ import annotations

import ast
import copy
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design
from pcbdraft.domain.operations import PlaceGroupEntry
from pcbdraft.domain.parts import PartGraph
from pcbdraft.services import application, application_semantic_operations
from pcbdraft.services.application import ApplicationService
from tests.support.design_factory import minimal_design_dict


class ApplicationSemanticOperationsCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.design = Design.from_dict(minimal_design_dict())
        self.graph = PartGraph.bundled().with_footprint_overrides(self.design)

    def test_extracted_module_has_no_application_reverse_import(self) -> None:
        source = Path(application_semantic_operations.__file__).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("pcbdraft.services.application", imports)

    def test_legacy_method_shapes_and_error_text_remain_stable(self) -> None:
        self.assertIsInstance(
            inspect.getattr_static(ApplicationService, "_entry_mapping"),
            staticmethod,
        )
        self.assertIsInstance(
            inspect.getattr_static(ApplicationService, "_parameter_mapping"),
            staticmethod,
        )
        self.assertIsInstance(
            inspect.getattr_static(ApplicationService, "_flat_semantic_operations"),
            classmethod,
        )
        self.assertIsInstance(
            inspect.getattr_static(ApplicationService, "_flat_semantic_operation"),
            classmethod,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "changes contains a duplicate or invalid field",
        ):
            ApplicationService._entry_mapping(
                {
                    "entries": [
                        {"field": "value", "value": 1},
                        {"field": "value", "value": 2},
                    ]
                },
                "changes",
            )
        with self.assertRaisesRegex(
            ValidationError,
            "flat PCB write is not implemented: unknown_tool",
        ):
            ApplicationService._flat_semantic_operation(
                "unknown_tool",
                {},
                self.design,
            )

    def test_group_wrapper_resolves_legacy_parser_and_class_patch_paths(self) -> None:
        entry = SimpleNamespace(to_tool_arguments=lambda: {"component_id": "load_r"})
        parsed = (entry,)
        operation = {"id": "patched"}
        with (
            patch.object(
                application,
                "parse_connect_group",
                return_value=parsed,
            ) as parser,
            patch.object(ApplicationService, "_validate_connect_group") as validator,
            patch.object(
                ApplicationService,
                "_flat_semantic_operation",
                return_value=operation,
            ) as builder,
        ):
            result = ApplicationService._flat_semantic_operations(
                "connect_group",
                {"connections": {"entries": []}},
                self.design,
                graph=self.graph,
            )

        self.assertEqual(result, [operation])
        parser.assert_called_once_with({"entries": []})
        validator.assert_called_once_with(parsed, self.design, self.graph)
        builder.assert_called_once_with(
            "connect_pin",
            {"component_id": "load_r"},
            self.design,
        )

        with (
            patch.object(
                application,
                "parse_place_group",
                return_value=parsed,
            ) as parser,
            patch.object(ApplicationService, "_validate_place_group") as validator,
            patch.object(
                ApplicationService,
                "_flat_semantic_operation",
                return_value=operation,
            ) as builder,
        ):
            result = ApplicationService._flat_semantic_operations(
                "place_group",
                {"placements": {"entries": []}},
                self.design,
                graph=self.graph,
            )

        self.assertEqual(result, [operation])
        parser.assert_called_once_with({"entries": []})
        validator.assert_called_once_with(parsed, self.design, self.graph)
        builder.assert_called_once_with(
            "place_footprint",
            {"component_id": "load_r"},
            self.design,
        )

    def test_operation_wrapper_resolves_legacy_entropy_copy_and_mapping_paths(
        self,
    ) -> None:
        value = {"id": "new_component", "nested": {"value": 1}}
        with (
            patch.object(
                application.secrets, "token_hex", return_value="abc123"
            ) as token,
            patch.object(
                application.copy,
                "deepcopy",
                wraps=copy.deepcopy,
            ) as deep_copy,
        ):
            operation = ApplicationService._flat_semantic_operation(
                "add_component",
                {"value": value},
                self.design,
            )

        self.assertEqual(operation["id"], "op_abc123")
        self.assertEqual(operation["args"], {"value": value})
        self.assertIsNot(operation["args"]["value"], value)
        token.assert_called_once_with(6)
        deep_copy.assert_called_once_with(value)

        with patch.object(
            ApplicationService,
            "_entry_mapping",
            return_value={"value": "patched"},
        ) as mapping:
            operation = ApplicationService._flat_semantic_operation(
                "update_component",
                {"component_id": "load_r", "changes": {"entries": []}},
                self.design,
            )
        self.assertEqual(operation["args"]["changes"], {"value": "patched"})
        mapping.assert_called_once_with({"entries": []}, "changes")

        with patch.object(
            ApplicationService,
            "_parameter_mapping",
            return_value={"speed": "patched"},
        ) as mapping:
            operation = ApplicationService._flat_semantic_operation(
                "add_interface",
                {"value": {"id": "new_interface", "params": []}},
                self.design,
            )
        self.assertEqual(operation["args"]["value"]["params"], {"speed": "patched"})
        mapping.assert_called_once_with([], "interface.params")

    def test_place_validation_resolves_legacy_routed_net_patch_path(self) -> None:
        entry = PlaceGroupEntry("load_r", 11.0, 12.0, 90.0, "front")
        with (
            patch.object(
                application,
                "_routed_component_nets",
                return_value=("net_out",),
            ) as routed_nets,
            self.assertRaisesRegex(
                ValidationError,
                "component retains routed copper on net_out: load_r",
            ),
        ):
            ApplicationService._validate_place_group(
                (entry,),
                self.design,
                self.graph,
            )
        routed_nets.assert_called_once_with(self.design, "load_r")


if __name__ == "__main__":
    unittest.main()
