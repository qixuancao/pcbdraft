from __future__ import annotations

import unittest

from pcb_agent.ir import Design
from pcb_agent.parts import PartGraph
from tests.design_factory import minimal_design_dict


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
        self.assertEqual([part.id for part in result], ["microchip.attiny402-ssn"])
        self.assertTrue(all(part.trust == "rule_validated" for part in result))


if __name__ == "__main__":
    unittest.main()
