from __future__ import annotations

import unittest
from types import SimpleNamespace

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.assertions import evaluate_assertion
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.semantic_rules import _coverage_findings
from pcbdraft.services.application_semantic_operations import _flat_semantic_operations


class ConstraintWriteContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.design = SimpleNamespace(
            board=SimpleNamespace(
                edge_clearance_mm=0.5,
                min_clearance_mm=0.2,
                min_drill_mm=0.3,
                min_track_mm=0.2,
            ),
            constraints=(),
            requirements=(),
            power_domains=(),
            interfaces=(),
            components=(),
            nets=(),
        )
        self.graph = None

    @staticmethod
    def _value(
        constraint_id: str,
        kind: str,
        targets: list[str],
        params: dict[str, object],
    ) -> dict[str, object]:
        return {
            "id": constraint_id,
            "kind": kind,
            "targets": targets,
            "params": [
                {"name": name, "value": value} for name, value in params.items()
            ],
            "severity": "required",
            "rationale": "Exercise the write contract.",
        }

    def _add(self, value: dict[str, object]) -> list[dict[str, object]]:
        return _flat_semantic_operations(
            "add_constraint",
            {"value": value},
            self.design,
            graph=self.graph,
        )

    def _update(self, value: dict[str, object]) -> list[dict[str, object]]:
        return _flat_semantic_operations(
            "update_constraint",
            {"value": value},
            self.design,
            graph=self.graph,
        )

    def test_assertion_requires_supported_predicate_and_rejects_manual_review(self):
        value = self._value(
            "connector_review",
            "assertion",
            ["load_r"],
            {"human_mechanical_review_required": True},
        )
        with self.assertRaisesRegex(
            ValidationError,
            "supported predicate.*human/mechanical review",
        ):
            self._add(value)

        value["params"] = [{"name": "predicate", "value": "all_power_inputs_connected"}]
        self.assertEqual(self._add(value)[0]["op"], "upsert_constraint")

    def test_manufacturing_requires_verified_fields_and_board_values(self):
        value = self._value(
            "manufacturing",
            "manufacturing_rules",
            ["board"],
            {
                "layers": 2,
                "min_track_mm": 0.2,
                "min_clearance_mm": 0.2,
            },
        )
        with self.assertRaisesRegex(
            ValidationError,
            "manufacturing_rules constraint params.*missing.*edge_clearance_mm",
        ):
            self._add(value)

        value["params"] = [
            {"name": name, "value": number}
            for name, number in {
                "edge_clearance_mm": 0.5,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "min_track_mm": 0.25,
            }.items()
        ]
        with self.assertRaisesRegex(
            ValidationError,
            "does not match the board contract.*min_track_mm",
        ):
            self._add(value)

        value["params"] = [
            {"name": name, "value": number}
            for name, number in {
                "edge_clearance_mm": 0.5,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "min_track_mm": 0.2,
            }.items()
        ]
        self.assertEqual(self._add(value)[0]["op"], "upsert_constraint")

        update_value = self._value(
            "manufacturing",
            "manufacturing_rules",
            ["board"],
            {
                "edge_clearance_mm": 0.5,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "min_track_mm": 0.2,
            },
        )
        self.design.constraints = (SimpleNamespace(id="manufacturing"),)
        self.assertEqual(self._update(update_value)[0]["op"], "upsert_constraint")

        update_value["params"] = [{"name": "min_clearance_mm", "value": 0.2}]
        with self.assertRaisesRegex(ValidationError, "missing.*edge_clearance_mm"):
            self._update(update_value)

    def test_current_limit_requires_explicit_electrical_inputs(self):
        value = self._value(
            "led_current",
            "current_limit",
            ["load_r", "net_out"],
            {"supply_v": 3.3, "max_current_a": 0.005},
        )
        with self.assertRaisesRegex(
            ValidationError, "current_limit.*missing.*forward_v"
        ):
            self._add(value)

        for name, bad_values, message in (
            ("supply_v", [0, float("nan"), float("inf")], "supply_v"),
            ("max_current_a", [0, -0.001], "max_current_a"),
            ("forward_v", [-0.1, 3.3, 4.0], "forward_v"),
        ):
            for bad in bad_values:
                with self.subTest(parameter=name, value=bad):
                    value["params"] = [
                        {"name": "supply_v", "value": 3.3},
                        {"name": "forward_v", "value": 2.1},
                        {"name": "max_current_a", "value": 0.005},
                        {"name": "resistance_ohm", "value": 4700},
                    ]
                    next(item for item in value["params"] if item["name"] == name)[
                        "value"
                    ] = bad
                    with self.assertRaisesRegex(ValidationError, message):
                        self._add(value)

        value["params"] = [
            {"name": name, "value": number}
            for name, number in {
                "supply_v": 3.3,
                "forward_v": 2.1,
                "max_current_a": 0.005,
                "resistance_ohm": 4700,
            }.items()
        ]
        self.assertEqual(self._add(value)[0]["op"], "upsert_constraint")

    def test_routing_requires_validator_width_and_existing_nets_before_write(self):
        self.design.nets = (
            SimpleNamespace(id="net_signal"),
            SimpleNamespace(id="net_gnd"),
        )
        value = self._value(
            "route_signal",
            "routing",
            ["net_signal"],
            {
                "all_other_clearance_mm": 0.2,
                "pad_to_pad_clearance_mm": 0.19,
                "via_drill_min_mm": 0.3,
            },
        )
        with self.assertRaisesRegex(ValidationError, "missing width_mm"):
            self._add(value)
        self.assertEqual(self.design.constraints, ())

        for params, message in (
            ({"width_mm": 0.2, "pad_to_slot_clearance_mm": 0.18}, "unsupported"),
            ({"width_mm": 0.19}, "board.min_track_mm"),
            ({"width_mm": 0.25, "neckdown_width_mm": 0.1}, "board.min_track_mm"),
            ({"width_mm": 0.25, "max_length_mm": 0}, "max_length_mm"),
            ({"width_mm": 0.25, "auto_route": "yes"}, "auto_route"),
            (
                {"width_mm": 0.25, "min_reference_stitching_vias": 1},
                "continuous_reference_net",
            ),
        ):
            with self.subTest(params=params):
                value["params"] = [
                    {"name": name, "value": number} for name, number in params.items()
                ]
                with self.assertRaisesRegex(ValidationError, message):
                    self._add(value)

        valid = {
            "width_mm": 0.25,
            "auto_route": True,
            "neckdown_width_mm": 0.2,
            "continuous_reference_net": "net_gnd",
            "reference_connection_policy": "ensure_connected",
            "min_reference_stitching_vias": 0,
        }
        value["params"] = [
            {"name": name, "value": number} for name, number in valid.items()
        ]
        self.assertEqual(self._add(value)[0]["args"]["value"]["params"], valid)
        self.design.constraints = (SimpleNamespace(id="route_signal"),)
        bad_update = self._value("route_signal", "routing", ["net_signal"], {})
        with self.assertRaisesRegex(ValidationError, "missing width_mm"):
            self._update(bad_update)
        self.assertEqual(len(self.design.constraints), 1)
        self.assertEqual(self._update(value)[0]["op"], "upsert_constraint")

        value["targets"] = ["missing_net"]
        with self.assertRaisesRegex(ValidationError, "existing net IDs"):
            self._update(value)

    def test_placement_region_requires_named_region_and_component_targets(self):
        self.design.components = (SimpleNamespace(id="u13"),)
        value = self._value(
            "top_region", "placement_region", ["u13"], {"layer": "F.Cu", "side": "top"}
        )
        with self.assertRaisesRegex(
            ValidationError, "exactly one supported named region"
        ):
            self._add(value)
        self.assertEqual(self.design.constraints, ())

        for region in ("F.Cu", "upper", [], None):
            with self.subTest(region=region):
                value["params"] = [{"name": "region", "value": region}]
                with self.assertRaisesRegex(ValidationError, "supported named region"):
                    self._add(value)

        value["params"] = [{"name": "region", "value": "top"}]
        self.assertEqual(self._add(value)[0]["op"], "upsert_constraint")
        self.design.constraints = (SimpleNamespace(id="top_region"),)
        value["targets"] = ["unknown_component"]
        with self.assertRaisesRegex(ValidationError, "existing component IDs"):
            self._update(value)
        value["targets"] = ["u13"]
        self.assertEqual(self._update(value)[0]["op"], "upsert_constraint")

    def test_historical_invalid_constraint_still_loads_and_fails_semantics(self):
        from pcbdraft.domain.ir import Constraint

        loaded = Constraint.from_dict(
            {
                "id": "old_invalid_assertion",
                "kind": "assertion",
                "targets": ["load_r"],
                "params": {"human_mechanical_review_required": True},
                "severity": "required",
                "rationale": "Retained historical data.",
                "provenance": ["user_spec"],
            },
            "$.constraints[0]",
        )
        self.assertEqual(
            evaluate_assertion(self.design, self.graph, loaded),
            "assertion predicate is missing or unsupported",
        )

    def test_missing_led_current_limit_feedback_is_actionable(self):
        graph = PartGraph.bundled()
        design = SimpleNamespace(
            components=(SimpleNamespace(id="led1", part_id="liteon.ltst-c190kgkt"),),
            interfaces=(),
            nets=(),
            constraints=(),
        )
        finding = next(
            item
            for item in _coverage_findings(design, graph, set())
            if item.code == "intent.required_constraint_missing"
            and item.object_id == "led"
        )
        details = dict(finding.details)
        self.assertEqual(details["required_params"], "forward_v,max_current_a,supply_v")
        self.assertIn("do not invent", details["target_guidance"])


if __name__ == "__main__":
    unittest.main()
