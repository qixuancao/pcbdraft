from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design, load_design, save_design
from tests.support.design_factory import minimal_design_dict


class SemanticIRTests(unittest.TestCase):
    def test_v1_read_preserves_bytes_until_a_successful_write(self) -> None:
        legacy = Design.from_dict(minimal_design_dict())

        self.assertEqual(legacy.version, 1)
        self.assertNotIn("native_intent", legacy.to_dict())
        self.assertEqual(legacy.clone().canonical_bytes(), legacy.canonical_bytes())

    def test_v2_native_intent_round_trips_deterministically(self) -> None:
        value = minimal_design_dict()
        value["version"] = 2
        value["native_intent"] = {
            "outline": [],
            "footprint_poses": [],
            "routes": [],
            "vias": [],
            "unrouted_nets": ["net_out"],
            "provenance": "pcbdraft",
            "geometry_revision": 1,
        }

        design = Design.from_dict(value)

        self.assertEqual(design.version, 2)
        self.assertEqual(design.native_intent.unrouted_nets, ("net_out",))
        self.assertEqual(Design.from_dict(design.to_dict()), design)

    def test_v2_requires_complete_native_intent_and_rejects_v1_geometry(self) -> None:
        missing = minimal_design_dict()
        missing["version"] = 2
        with self.assertRaisesRegex(ValidationError, "requires native intent"):
            Design.from_dict(missing)

        legacy = minimal_design_dict()
        legacy["native_intent"] = {
            "outline": [],
            "footprint_poses": [],
            "routes": [],
            "vias": [],
            "unrouted_nets": [],
            "provenance": "pcbdraft",
            "geometry_revision": 0,
        }
        with self.assertRaisesRegex(ValidationError, "v1 cannot contain"):
            Design.from_dict(legacy)

    def test_native_via_must_be_on_board_and_meet_drill_minimum(self) -> None:
        value = minimal_design_dict()
        value["version"] = 2
        value["native_intent"] = {
            "outline": [],
            "footprint_poses": [],
            "routes": [],
            "vias": [
                {
                    "id": "via_out",
                    "net": "net_out",
                    "x_mm": 21.0,
                    "y_mm": 1.0,
                    "diameter_mm": 0.4,
                    "drill_mm": 0.2,
                    "from_layer": 0,
                    "to_layer": 1,
                }
            ],
            "unrouted_nets": [],
            "provenance": "pcbdraft",
            "geometry_revision": 1,
        }
        with self.assertRaisesRegex(ValidationError, "native_geometry_outside_board"):
            Design.from_dict(value)

        value["native_intent"]["vias"][0]["x_mm"] = 10.0
        with self.assertRaisesRegex(ValidationError, "via_drill_below_minimum"):
            Design.from_dict(value)

    def test_round_trip_is_byte_deterministic_and_order_independent(self) -> None:
        value = minimal_design_dict()
        first = Design.from_dict(value)
        value["components"].reverse()
        value["nets"].reverse()
        second = Design.from_dict(value)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.content_hash(), second.content_hash())
        self.assertEqual(json.loads(first.canonical_bytes()), first.to_dict())

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.pcbir.json"
            save_design(path, first)
            self.assertEqual(load_design(path), first)
            self.assertEqual(path.read_bytes(), first.canonical_bytes())

    def test_unknown_field_and_nonfinite_numbers_are_rejected(self) -> None:
        value = minimal_design_dict()
        value["surprise"] = "not forward-compatible ambiguity"
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            Design.from_dict(value)
        value = minimal_design_dict()
        value["board"]["width_mm"] = float("nan")
        with self.assertRaisesRegex(ValidationError, "positive finite"):
            Design.from_dict(value)

    def test_cross_reference_and_multi_net_pin_errors_are_rejected(self) -> None:
        value = minimal_design_dict()
        value["nets"][1]["endpoints"].append(
            {"component": "load_r", "pin": "1", "role": "signal"}
        )
        with self.assertRaisesRegex(ValidationError, "pin_on_multiple_nets"):
            Design.from_dict(value)
        value = minimal_design_dict()
        value["components"][0]["block_id"] = "missing"
        with self.assertRaisesRegex(ValidationError, "missing_block"):
            Design.from_dict(value)

    def test_clone_is_semantically_independent(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        clone = design.clone()
        self.assertEqual(design, clone)
        value = copy.deepcopy(clone.to_dict())
        value["metadata"]["new"] = True
        changed = Design.from_dict(value)
        self.assertNotEqual(design.content_hash(), changed.content_hash())


if __name__ == "__main__":
    unittest.main()
