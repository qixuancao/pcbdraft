from __future__ import annotations

import unittest
from dataclasses import replace

from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.domain.ir import Design
from pcbdraft.domain.operations import ChangeSet, apply_change_set
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.consistency import (
    NativeBoardProjection,
    NativeReferenceTextPose,
    compare_native_operation_delta,
)
from pcbdraft.kicad.pcb import _component_job
from pcbdraft.services.application import ApplicationService
from tests.support.design_factory import minimal_design_dict


class FootprintReferenceToolTests(unittest.TestCase):
    def test_move_reference_is_a_strict_absolute_position_tool(self) -> None:
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("move_footprint_reference")

        self.assertEqual(spec.external_name, "pcb_move_footprint_reference")
        self.assertEqual(
            spec.input_schema["required"], ["component_id", "x_mm", "y_mm"]
        )
        self.assertFalse(spec.input_schema["additionalProperties"])

    def test_move_reference_persists_into_the_native_component_job(self) -> None:
        before = Design.from_dict(minimal_design_dict())
        operation = ApplicationService._flat_semantic_operation(
            "move_footprint_reference",
            {"component_id": "load_r", "x_mm": 15.0, "y_mm": 18.0},
            before,
        )
        change = ChangeSet.from_dict(
            {
                "schema": "pcbdraft-change-set",
                "version": 1,
                "id": "move_reference",
                "base_hash": before.content_hash(),
                "intent": "Move one footprint reference off copper.",
                "actor": "unit-test",
                "operations": [operation],
                "provenance": ["tests/kicad/test_footprint_reference.py"],
            }
        )

        candidate = apply_change_set(before, change)
        component = next(item for item in candidate.components if item.id == "load_r")
        self.assertEqual(
            component.attributes["footprint_reference"],
            {"visible": True, "x_mm": 15.0, "y_mm": 18.0},
        )

        placement = component.placement
        assert placement is not None
        job = _component_job(
            candidate,
            component,
            PartGraph.bundled().get(component.part_id),
            {
                component.id: {
                    "x_mm": placement.x_mm,
                    "y_mm": placement.y_mm,
                    "rotation_deg": placement.rotation_deg,
                    "side": placement.side,
                    "fixed": placement.fixed,
                }
            },
        )
        self.assertEqual(
            job["reference_text"],
            {"visible": True, "x_mm": 15.0, "y_mm": 18.0},
        )

        before_board = NativeBoardProjection("evaluated", (), (), (), 0, "passed")
        after_board = replace(
            before_board,
            reference_text_poses=(NativeReferenceTextPose("R1", True, 15.0, 18.0),),
        )
        report = compare_native_operation_delta(
            "move_footprint_reference",
            {"component_id": "load_r", "x_mm": 15.0, "y_mm": 18.0},
            before,
            candidate,
            before_board,
            after_board,
        )
        self.assertTrue(report.passed, report.to_dict())

        stale_report = compare_native_operation_delta(
            "move_footprint_reference",
            {"component_id": "load_r", "x_mm": 15.0, "y_mm": 18.0},
            before,
            candidate,
            before_board,
            replace(
                before_board,
                reference_text_poses=(NativeReferenceTextPose("R1", True, 14.0, 14.1),),
            ),
        )
        self.assertFalse(stale_report.passed)

        before_with_unrelated = replace(
            before_board,
            reference_text_poses=(
                NativeReferenceTextPose("R1", False, 10.0, 10.0),
                NativeReferenceTextPose("R2", True, 16.0, 10.0),
            ),
        )
        target_only_report = compare_native_operation_delta(
            "move_footprint_reference",
            {"component_id": "load_r", "x_mm": 15.0, "y_mm": 18.0},
            before,
            candidate,
            before_with_unrelated,
            replace(
                before_board,
                reference_text_poses=(
                    NativeReferenceTextPose("R1", True, 15.0, 18.0),
                    NativeReferenceTextPose("R2", True, 16.0, 10.0),
                ),
            ),
        )
        self.assertTrue(target_only_report.passed, target_only_report.to_dict())

        after_with_unrelated_change = replace(
            before_board,
            reference_text_poses=(
                NativeReferenceTextPose("R1", True, 15.0, 18.0),
                NativeReferenceTextPose("R2", True, 17.0, 10.0),
            ),
        )
        unrelated_report = compare_native_operation_delta(
            "move_footprint_reference",
            {"component_id": "load_r", "x_mm": 15.0, "y_mm": 18.0},
            before,
            candidate,
            before_with_unrelated,
            after_with_unrelated_change,
        )
        self.assertFalse(unrelated_report.passed)
        unrelated_check = next(
            item
            for item in unrelated_report.checks
            if item.name == "no_unrelated_native_delta"
        )
        self.assertFalse(unrelated_check.passed)


if __name__ == "__main__":
    unittest.main()
