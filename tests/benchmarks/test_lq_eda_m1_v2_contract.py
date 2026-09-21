from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "benchmarks/lq-eda/15th-province-p1-m1-v2"


class LqEdaM1V2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads((TASK / "contract.json").read_text(encoding="utf-8"))
        cls.prompt_path = TASK / "input/prompt.txt"
        cls.prompt = cls.prompt_path.read_text(encoding="utf-8")

    def test_task_identity_and_public_contract_boundary(self) -> None:
        self.assertEqual(self.contract["schema"], "pcbdraft-lq-eda-task-contract")
        self.assertEqual(self.contract["version"], 1)
        self.assertEqual(self.contract["input_revision"], 2)
        self.assertEqual(self.contract["task_id"], "15th-province-p1-m1-v2")
        self.assertEqual(self.contract["status"], "input_contract_only_not_run")
        self.assertFalse(
            self.contract["scoring_boundary"]["reference_netlist_included"]
        )
        self.assertEqual(
            self.contract["scoring_boundary"]["answer_key"], "private holdout only"
        )
        self.assertNotIn("expected_nets", self.contract)
        self.assertNotIn("answer_key", self.contract["input"])

    def test_prompt_and_custom_resources_are_hash_bound_byte_copies(self) -> None:
        prompt_hash = hashlib.sha256(self.prompt_path.read_bytes()).hexdigest()
        self.assertEqual(prompt_hash, self.contract["input"]["prompt_sha256"])

        for group in ("custom_symbols", "custom_footprints"):
            for item in self.contract["input"][group]:
                path = TASK / item["path"]
                source = ROOT / "benchmarks/lq-eda" / item["byte_copy_of"]
                self.assertTrue(path.is_file(), path)
                self.assertTrue(source.is_file(), source)
                self.assertEqual(path.read_bytes(), source.read_bytes())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"]
                )

    def test_v2_capacitor_and_connector_wording_is_unambiguous(self) -> None:
        components = {item["ref"]: item for item in self.contract["components"]}
        self.assertEqual(len(components), 9)
        for reference in ("C13", "C14"):
            self.assertEqual(components[reference]["symbol"], "Device:C")
            self.assertEqual(
                components[reference]["footprint"],
                "Capacitor_SMD:C_1206_3216Metric",
            )
        self.assertIn("non-polarized `Device:C`", self.prompt)
        self.assertNotIn("positive side", self.prompt.lower())
        self.assertIn("stock-footprint adaptation", self.prompt)
        self.assertNotIn("pitch-equivalent", self.prompt)
        self.assertTrue(
            any("ZX-XH2.54-2PZZ" in item for item in self.contract["limitations"])
        )

    def test_acceptance_matrix_separates_automated_and_unknown_gates(self) -> None:
        matrix = self.contract["acceptance_matrix"]
        automated = {item["id"]: item for item in matrix["automated"]}
        manual = {item["id"]: item for item in matrix["manual_or_unknown"]}
        self.assertTrue(matrix["unknown_is_not_pass"])
        self.assertIn("topology_endpoint_sets", automated)
        self.assertIn("erc_drc_receipts", automated)
        self.assertIn("engineering_candidate", automated)
        self.assertIn("schematic_pcb_parity", automated)
        self.assertIn("zone_coverage_and_pad_slot", manual)
        self.assertIn("silkscreen_geometry_and_legibility", manual)
        self.assertIn("unspecified_electrical_parameters", manual)
        self.assertIn("independent_of", automated["engineering_candidate"])
        self.assertIn(
            "topology_endpoint_sets",
            automated["engineering_candidate"]["independent_of"],
        )
        self.assertIn(
            "not establish actual GND-zone connectivity/effective coverage",
            automated["native_rules_and_connectivity"]["check"],
        )

    def test_public_rules_and_network_scope_are_unchanged(self) -> None:
        contract = self.contract
        self.assertEqual(
            contract["network_names"],
            ["VBUS", "VBAT", "PROG", "STAT", "GND", "LED4_A", "LED5_A"],
        )
        self.assertEqual(
            contract["rules"],
            {
                "layers": 2,
                "minimum_track_width_mil": 10,
                "pad_to_pad_clearance_mil": 7.5,
                "pad_to_slot_clearance_mil": 7,
                "other_clearance_mil": 8,
                "via_outer_diameter_min_mil": 25,
                "via_drill_min_mil": 15,
                "all_components_on": "F.Cu",
                "silkscreen": "F.SilkS",
                "ground_copper_zones": ["F.Cu", "B.Cu"],
                "unconnected_items": 0,
            },
        )
        self.assertIn("LED4_A", self.prompt)
        self.assertIn("LED5_A", self.prompt)
        self.assertIn("do not invent those values", self.prompt)


if __name__ == "__main__":
    unittest.main()
