from __future__ import annotations

import unittest
from typing import Any

from pcbdraft.errors import ValidationError
from pcbdraft.kicad_pcb import (
    _add_reference_stitching_vias,
    _routing_failure_message,
)
from pcbdraft.routing import (
    GridRouter,
    RouteSegment,
    RoutingKeepout,
    RoutingPad,
    RoutingResult,
)


def _router(**overrides: Any) -> GridRouter:
    values: dict[str, Any] = {
        "board_width_mm": 10,
        "board_height_mm": 10,
        "layers": 2,
        "clearance_mm": 0.2,
        "min_track_mm": 0.2,
        "min_drill_mm": 0.3,
        "edge_clearance_mm": 0.2,
        "grid_mm": 0.2,
        "max_expansions": 100_000,
    }
    values.update(overrides)
    return GridRouter(**values)


class RoutingTests(unittest.TestCase):
    def test_straight_route_is_deterministic(self) -> None:
        pads = (
            RoutingPad("a", "N", 2, 2, 0.5, 0.5, (0,)),
            RoutingPad("b", "N", 8, 2, 0.5, 0.5, (0,)),
        )
        first = _router().route(pads)
        second = _router().route(reversed(pads))
        self.assertEqual(first, second)
        self.assertEqual(first.state, "completed")
        self.assertEqual(first.unrouted, ())
        self.assertEqual(len(first.segments), 1)
        self.assertEqual(first.vias, ())

    def test_crossing_nets_use_other_layer_and_through_vias(self) -> None:
        pads = (
            RoutingPad("a1", "A", 0.5, 5, 0.3, 0.6, (0,)),
            RoutingPad("a2", "A", 9.5, 5, 0.3, 0.6, (0,)),
            RoutingPad("b1", "B", 5, 0.5, 0.6, 0.3, (0,)),
            RoutingPad("b2", "B", 5, 9.5, 0.6, 0.3, (0,)),
        )
        result = _router().route(pads, widths={"A": 0.25, "B": 0.25})
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.unrouted, ())
        self.assertEqual({segment.layer for segment in result.segments}, {0, 1})
        self.assertEqual(len(result.vias), 2)

    def test_impossible_keepout_reports_unrouted_without_fabricating_success(
        self,
    ) -> None:
        result = _router().route(
            (
                RoutingPad("left", "N", 2, 5, 0.5, 0.5, (0,)),
                RoutingPad("right", "N", 8, 5, 0.5, 0.5, (0,)),
            ),
            keepouts=(RoutingKeepout("wall", 4.5, 0, 5.5, 10, (0, 1)),),
        )
        self.assertEqual(result.state, "heuristic")
        self.assertEqual(result.unrouted, ("N",))
        self.assertIn("could not connect", result.diagnostics[0])

    def test_router_enforces_grid_bounds_without_a_static_layer_cap(self) -> None:
        self.assertEqual(_router(layers=1).layer_count, 1)
        self.assertEqual(_router(layers=6).layer_count, 6)
        self.assertEqual(_router(layers=65).layer_count, 65)
        with self.assertRaisesRegex(ValidationError, "bounded limit"):
            _router(board_width_mm=1000, board_height_mm=1000, grid_mm=0.01)

    def test_fine_pitch_seed_becomes_the_pad_terminal(self) -> None:
        seed = RouteSegment("N", 0, 1.03, 2.07, 2.0, 2.0, 0.2)
        result = _router(grid_mm=0.1).route(
            (
                RoutingPad("fine", "N", 1.03, 2.07, 0.35, 0.6, (0,)),
                RoutingPad("far", "N", 8, 2, 0.5, 0.5, (0,)),
            ),
            seed_segments=(seed,),
        )
        self.assertEqual(result.state, "completed")
        self.assertIn(seed, result.segments)
        self.assertTrue(
            any(
                (segment.x1_mm, segment.y1_mm) == (2.0, 2.0)
                or (segment.x2_mm, segment.y2_mm) == (2.0, 2.0)
                for segment in result.segments
                if segment != seed
            )
        )

    def test_seed_must_be_anchored_to_a_real_pad(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not anchored"):
            _router().route(
                (
                    RoutingPad("a", "N", 2, 2, 0.5, 0.5, (0,)),
                    RoutingPad("b", "N", 8, 2, 0.5, 0.5, (0,)),
                ),
                seed_segments=(RouteSegment("N", 0, 3, 3, 4, 3, 0.2),),
            )

    def test_obstructed_optional_fine_pitch_seed_falls_back_to_bounded_route(
        self,
    ) -> None:
        result = _router().route(
            (
                RoutingPad("fine", "N", 2, 2, 0.35, 0.5, (0,)),
                RoutingPad("far", "N", 8, 2, 0.5, 0.5, (0,)),
                RoutingPad("foreign", "OTHER", 3, 2, 0.8, 0.8, (0,)),
            ),
            seed_segments=(RouteSegment("N", 0, 2, 2, 4, 2, 0.2),),
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.unrouted, ())
        self.assertTrue(
            any("omitted obstructed optional" in item for item in result.diagnostics)
        )

    def test_exact_seed_clearance_avoids_fine_pitch_grid_false_positive(self) -> None:
        seeds = (
            RouteSegment("A", 0, 2.0, 2.15, 4.0, 2.15, 0.2),
            RouteSegment("B", 0, 2.0, 2.65, 4.0, 2.65, 0.2),
        )
        result = _router(grid_mm=0.1, clearance_mm=0.21).route(
            (
                RoutingPad("fine-a", "A", 2.0, 2.15, 0.6, 0.3, (0,)),
                RoutingPad("fine-b", "B", 2.0, 2.65, 0.6, 0.3, (0,)),
            ),
            seed_segments=seeds,
        )
        self.assertEqual(result.state, "completed")
        self.assertTrue(set(seeds) <= set(result.segments))
        self.assertFalse(
            any("omitted obstructed optional" in item for item in result.diagnostics)
        )

    def test_unrouted_diagnostics_are_not_masked_by_reference_stitching(self) -> None:
        routing = RoutingResult(
            segments=(),
            vias=(),
            unrouted=("N\nunsafe",),
            state="heuristic",
            expanded_nodes=42,
            diagnostics=tuple(f"reason {index}" for index in range(12)),
        )
        result = _add_reference_stitching_vias(None, (), None, (), routing)  # type: ignore[arg-type]
        message = _routing_failure_message(result)
        self.assertIn("expanded_nodes=42", message)
        self.assertTrue(
            any(
                "reference-plane stitching skipped" in item
                for item in result.diagnostics
            )
        )
        self.assertIn("additional diagnostic(s) omitted", message)
        self.assertNotIn("\n", message)


if __name__ == "__main__":
    unittest.main()
