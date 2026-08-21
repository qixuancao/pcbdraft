from __future__ import annotations

import unittest

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from tests.support.design_factory import minimal_design_dict


class TrustedPartGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = PartGraph.bundled()

    def test_seed_catalog_resolves_real_kicad_symbol_and_footprint(self) -> None:
        part = self.graph.get("microchip.attiny402-ssn")
        resolution = self.graph.resolve_libraries(part)
        self.assertTrue(resolution["symbol"].available, resolution["symbol"].reason)
        self.assertTrue(
            resolution["footprint"].available, resolution["footprint"].reason
        )
        self.assertEqual(part.pin("6").footprint_pad, "6")
        self.assertIn("updi", part.pin("6").functions)
        vtref = self.graph.get("samtec.tsw-103-07-g-s").pin("2")
        self.assertEqual(vtref.name, "VTREF")
        self.assertIn("voltage_sense", vtref.functions)

    def test_valid_design_satisfies_part_and_rating_contracts(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        self.assertEqual(self.graph.validate_design(design, check_libraries=True), [])

    def test_wrong_pin_and_required_unconnected_are_deterministic_findings(
        self,
    ) -> None:
        value = minimal_design_dict()
        value["nets"][1]["endpoints"][0]["pin"] = "99"
        design = Design.from_dict(value)
        codes = {issue.code for issue in self.graph.validate_design(design)}
        self.assertIn("part.pin_missing", codes)
        self.assertIn("part.required_pin_unconnected", codes)

    def test_selector_never_returns_untrusted_or_inactive_by_default(self) -> None:
        result = self.graph.find(
            kind="microcontroller", function="i2c_sda", min_voltage_v=3.6
        )
        self.assertIn("microchip.attiny402-ssn", [part.id for part in result])
        self.assertTrue(all(part.trust == "rule_validated" for part in result))

    def test_factual_search_includes_stable_ids_and_catalog_count(self) -> None:
        matches = self.graph.search("attiny402")

        self.assertGreater(len(self.graph), 0)
        self.assertTrue(matches)
        self.assertTrue(all(part.id for part in matches))

    def test_installed_kicad_record_is_strict_and_merge_never_redefines(self) -> None:
        value = {
            "id": "kicad.generic-led-5mm-green",
            "kind": "led",
            "description": "Green 5 mm LED",
            "symbol": "Device:LED",
            "footprint": "LED_THT:LED_D5.0mm",
            "bom": True,
            "pins": [
                {
                    "number": "1",
                    "name": "K",
                    "electrical_type": "passive",
                    "functions": ["cathode"],
                    "required": True,
                    "footprint_pad": "1",
                },
                {
                    "number": "2",
                    "name": "A",
                    "electrical_type": "passive",
                    "functions": ["anode"],
                    "required": True,
                    "footprint_pad": "2",
                },
            ],
        }
        record = PartGraph.installed_kicad_part(value, footprint_sha256="a" * 64)
        extended = self.graph.merged([record], source="test")

        self.assertEqual(extended.get(record.id), record)
        self.assertEqual(extended.search("green 5 mm")[0].id, record.id)
        changed = dict(value)
        changed["description"] = "Different"
        collision = PartGraph.installed_kicad_part(changed, footprint_sha256="a" * 64)
        with self.assertRaisesRegex(ValidationError, "redefine canonical part id"):
            extended.merged([collision])


if __name__ == "__main__":
    unittest.main()
