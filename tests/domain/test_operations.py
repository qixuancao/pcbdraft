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
    def test_add_update_remove_component_maintains_block_membership(self) -> None:
        before = Design.from_dict(minimal_design_dict())
        added = apply_change_set(
            before,
            change_set(
                before,
                [
                    operation(
                        "add_block",
                        {
                            "value": {
                                "id": "other",
                                "kind": "logic",
                                "name": "Other",
                                "version": "1",
                                "intent": "Second block.",
                                "components": [],
                                "provenance": ["user_spec"],
                            }
                        },
                        operation_id="op_1",
                    ),
                    operation(
                        "add_component",
                        {
                            "value": {
                                "id": "new_r",
                                "reference": "R2",
                                "part_id": "yageo.rc0603fr-074k7l",
                                "value": "4.7k",
                                "block_id": "power_block",
                            }
                        },
                        operation_id="op_2",
                    ),
                ],
            ),
        )
        moved = apply_change_set(
            added,
            change_set(
                added,
                [
                    operation(
                        "update_component",
                        {"component_id": "new_r", "changes": {"block_id": "other"}},
                    )
                ],
                change_id="change_2",
            ),
        )
        removed = apply_change_set(
            moved,
            change_set(
                moved,
                [operation("remove_component", {"id": "new_r"})],
                change_id="change_3",
            ),
        )

        self.assertIn(
            "new_r", next(b for b in added.blocks if b.id == "power_block").components
        )
        self.assertNotIn(
            "new_r",
            next(b for b in moved.blocks if b.id == "power_block").components,
        )
        self.assertIn(
            "new_r", next(b for b in moved.blocks if b.id == "other").components
        )
        self.assertNotIn(
            "new_r", next(b for b in removed.blocks if b.id == "other").components
        )

    def test_remove_net_requires_explicit_disconnect_and_unroute(self) -> None:
        before = Design.from_dict(minimal_design_dict())
        with self.assertRaisesRegex(ValidationError, "connected endpoints"):
            apply_change_set(
                before,
                change_set(before, [operation("remove_net", {"id": "net_out"})]),
            )

        routed = apply_change_set(
            before,
            change_set(
                before,
                [
                    operation(
                        "route_net",
                        {
                            "net_id": "net_out",
                            "segments": [
                                {
                                    "id": "route_out_1",
                                    "net": "net_out",
                                    "layer": 0,
                                    "x1_mm": 1.0,
                                    "y1_mm": 1.0,
                                    "x2_mm": 2.0,
                                    "y2_mm": 1.0,
                                    "width_mm": 0.25,
                                }
                            ],
                            "vias": [],
                        },
                    )
                ],
            ),
        )
        disconnected_document = routed.to_dict()
        next(net for net in disconnected_document["nets"] if net["id"] == "net_out")[
            "endpoints"
        ] = []
        disconnected = Design.from_dict(disconnected_document)
        with self.assertRaisesRegex(ValidationError, "retained copper"):
            apply_change_set(
                disconnected,
                change_set(disconnected, [operation("remove_net", {"id": "net_out"})]),
            )

    def test_disconnect_requires_the_exact_endpoint_role(self) -> None:
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
                            "role": "power",
                        },
                    },
                )
            ],
        )
        with self.assertRaisesRegex(ValidationError, "different endpoint role"):
            apply_change_set(design, changes)

    def test_first_atomic_write_promotes_v1_and_records_native_outline(self) -> None:
        before = Design.from_dict(minimal_design_dict())
        changes = change_set(
            before,
            [operation("set_board_outline", {"width_mm": 30, "height_mm": 24})],
        )

        after = apply_change_set(before, changes)

        self.assertEqual(after.version, 2)
        self.assertEqual((after.board.width_mm, after.board.height_mm), (30.0, 24.0))
        self.assertEqual(len(after.native_intent.outline), 4)
        self.assertEqual(after.native_intent.geometry_revision, 1)
        self.assertEqual(before.version, 1)

    def test_route_and_unroute_replace_only_the_target_net_geometry(self) -> None:
        before = Design.from_dict(minimal_design_dict())
        routed = apply_change_set(
            before,
            change_set(
                before,
                [
                    operation(
                        "route_net",
                        {
                            "net_id": "net_out",
                            "segments": [
                                {
                                    "id": "route_out_1",
                                    "net": "net_out",
                                    "layer": 0,
                                    "x1_mm": 1.0,
                                    "y1_mm": 1.0,
                                    "x2_mm": 2.0,
                                    "y2_mm": 1.0,
                                    "width_mm": 0.25,
                                }
                            ],
                            "vias": [],
                        },
                    )
                ],
            ),
        )
        unrouted = apply_change_set(
            routed,
            change_set(
                routed,
                [operation("unroute_net", {"net_id": "net_out"})],
                change_id="change_2",
            ),
        )

        self.assertEqual(len(routed.native_intent.routes), 1)
        self.assertEqual(unrouted.native_intent.routes, ())
        self.assertEqual(unrouted.native_intent.unrouted_nets, ("net_out",))
        self.assertEqual(unrouted.native_intent.geometry_revision, 2)

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
