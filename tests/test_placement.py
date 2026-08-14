from __future__ import annotations

import unittest

from pcbdraft.errors import ValidationError
from pcbdraft.placement import (
    GroupConstraint,
    NearConstraint,
    PlacementItem,
    optimize_placement,
)


class PlacementTests(unittest.TestCase):
    def test_placement_resolves_overlap_deterministically(self) -> None:
        items = (
            PlacementItem("fixed", 4, 5, 2, 2, fixed=True),
            PlacementItem("movable", 4, 5, 2, 2),
        )
        first = optimize_placement(
            items,
            board_width_mm=10,
            board_height_mm=10,
            edge_clearance_mm=0.5,
            nets=(("fixed", "movable"),),
        )
        second = optimize_placement(
            reversed(items),
            board_width_mm=10,
            board_height_mm=10,
            edge_clearance_mm=0.5,
            nets=(("movable", "fixed"),),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.state, "completed")
        self.assertEqual(first.diagnostics, ())
        self.assertEqual(first.by_id()["fixed"].x_mm, 4)
        self.assertEqual(first.by_id()["movable"].x_mm, 2)

    def test_placement_honors_near_and_group_contracts(self) -> None:
        result = optimize_placement(
            (
                PlacementItem("a", 2, 5, 1, 1, fixed=True),
                PlacementItem("b", 8, 5, 1, 1),
                PlacementItem("c", 8, 7, 1, 1),
            ),
            board_width_mm=12,
            board_height_mm=10,
            edge_clearance_mm=0.5,
            near=(NearConstraint("a", "b", 3),),
            groups=(GroupConstraint(("a", "b", "c"), 6),),
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.diagnostics, ())

    def test_fixed_overlap_is_an_explicit_error(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "invalid fixed placement.*overlaps"
        ):
            optimize_placement(
                (
                    PlacementItem("a", 4, 4, 2, 2, fixed=True),
                    PlacementItem("b", 4, 4, 2, 2, fixed=True),
                ),
                board_width_mm=10,
                board_height_mm=10,
                edge_clearance_mm=0.5,
            )

    def test_placement_rejects_unknown_constraint_targets(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown items: missing"):
            optimize_placement(
                (PlacementItem("a", 2, 2, 1, 1),),
                board_width_mm=10,
                board_height_mm=10,
                edge_clearance_mm=0.5,
                near=(NearConstraint("a", "missing", 2),),
            )


if __name__ == "__main__":
    unittest.main()
