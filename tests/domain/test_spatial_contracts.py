from __future__ import annotations

import unittest

from pcbdraft.domain.spatial_contracts import (
    anchored_rectangle,
    board_region_bounds,
    copper_layer_indices,
    rectangles_overlap,
)


class SpatialContractTests(unittest.TestCase):
    def test_named_regions_and_anchors_resolve_without_model_coordinates(self) -> None:
        self.assertEqual(board_region_bounds("top_left", 30, 18), (0, 0, 10, 6))
        self.assertEqual(
            anchored_rectangle("bottom_right", 4, 2, 30, 18, 1),
            (25, 15, 29, 17),
        )

    def test_layer_scopes_and_open_rectangle_overlap_are_deterministic(self) -> None:
        self.assertEqual(copper_layer_indices("all", 4), (0, 1, 2, 3))
        self.assertEqual(copper_layer_indices("outer", 4), (0, 3))
        self.assertTrue(rectangles_overlap((0, 0, 2, 2), (1, 1, 3, 3)))
        self.assertFalse(rectangles_overlap((0, 0, 1, 1), (1, 0, 2, 1)))


if __name__ == "__main__":
    unittest.main()
