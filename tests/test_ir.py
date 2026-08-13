from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from copperwright.errors import ValidationError
from copperwright.ir import Design, load_design, save_design
from tests.design_factory import minimal_design_dict


class SemanticIRTests(unittest.TestCase):
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
