from __future__ import annotations

import copy
import unittest

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design
from pcbdraft.domain.operations import ChangeSet, apply_change_set, semantic_diff
from tests.support.design_factory import minimal_design_dict


def change_set(
    design: Design, operations: list[dict], *, change_id: str = "change_1"
) -> ChangeSet:
    return ChangeSet.from_dict(
        {
            "schema": "pcbdraft-change-set",
            "version": 1,
            "id": change_id,
            "base_hash": design.content_hash(),
            "intent": "Exercise typed semantic operations.",
            "actor": "unit-test",
            "operations": operations,
            "provenance": ["tests/domain/test_operations.py"],
        }
    )


def operation(
    op: str, args: dict, *, expected: dict | None = None, operation_id: str = "op_1"
) -> dict:
    return {
        "id": operation_id,
        "op": op,
        "args": args,
        "expected": expected or {},
        "reason": "Unit-test operation.",
    }


class SemanticOperationTests(unittest.TestCase):
    def test_update_preview_has_object_and_field_level_impact(self) -> None:
        before = Design.from_dict(minimal_design_dict())
        changes = change_set(
            before,
            [
                operation(
                    "update_component",
                    {"component_id": "load_r", "changes": {"value": "4k7"}},
                    expected={"value": "4.7k", "part_id": "yageo.rc0603fr-074k7l"},
                )
            ],
        )
        after = apply_change_set(before, changes)
        self.assertEqual(
            next(c for c in after.components if c.id == "load_r").value, "4k7"
        )
        preview = semantic_diff(before, after)
        self.assertEqual(preview["summary"]["objects_modified"], 1)
        fields = preview["collections"]["components"]["modified"][0]["fields"]
        self.assertEqual(fields["value"], {"before": "4.7k", "after": "4k7"})

    def test_base_hash_and_field_preconditions_detect_conflicts(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        wrong_base = copy.deepcopy(
            change_set(
                design, [operation("set_metadata", {"key": "x", "value": 1})]
            ).to_dict()
        )
        wrong_base["base_hash"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "base hash conflicts"):
            apply_change_set(design, ChangeSet.from_dict(wrong_base))
        stale = change_set(
            design,
            [
                operation(
                    "update_component",
                    {"component_id": "load_r", "changes": {"value": "5k"}},
                    expected={"value": "stale"},
                )
            ],
        )
        with self.assertRaisesRegex(ValidationError, "precondition failed"):
            apply_change_set(design, stale)

    def test_invalid_later_operation_does_not_mutate_input(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        original = design.canonical_bytes()
        changes = change_set(
            design,
            [
                operation(
                    "set_metadata", {"key": "first", "value": True}, operation_id="op_1"
                ),
                operation(
                    "connect",
                    {
                        "net_id": "net_out",
                        "endpoint": {
                            "component": "load_r",
                            "pin": "1",
                            "role": "signal",
                        },
                    },
                    operation_id="op_2",
                ),
            ],
        )
        with self.assertRaisesRegex(ValidationError, "already connected"):
            apply_change_set(design, changes)
        self.assertEqual(design.canonical_bytes(), original)

    def test_disconnect_then_remove_is_explicit_and_validated(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        changes = change_set(
            design,
            [
                operation(
                    "disconnect",
                    {
                        "net_id": "net_out",
                        "endpoint": {
                            "component": "load_r",
                            "pin": "2",
                            "role": "signal",
                        },
                    },
                    operation_id="op_1",
                ),
                operation("remove_net", {"id": "net_out"}, operation_id="op_2"),
            ],
        )
        with self.assertRaisesRegex(ValidationError, "required_pin_unconnected"):
            # IR mutation itself is valid; the part graph validator owns this contract.
            from pcbdraft.domain.parts import PartGraph

            result = apply_change_set(design, changes)
            PartGraph.bundled().assert_design(result)


if __name__ == "__main__":
    unittest.main()
