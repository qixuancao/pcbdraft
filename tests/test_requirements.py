from __future__ import annotations

import copy
import unittest
from pathlib import Path

from pcb_agent.blocks import BlockRegistry
from pcb_agent.errors import ValidationError
from pcb_agent.ir import Design
from pcb_agent.parts import PartGraph
from pcb_agent.requirements import RequirementsSpec, compile_requirements
from pcb_agent.semantic_rules import evaluate_semantic_rules
from tests.requirements_factory import controller_requirements_dict


class RequirementsCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = PartGraph.bundled()
        cls.registry = BlockRegistry.bundled(cls.graph)

    def test_compiles_deterministic_typed_design_with_verified_blocks(self) -> None:
        value = controller_requirements_dict()
        first = compile_requirements(
            RequirementsSpec.from_dict(value), graph=self.graph, registry=self.registry
        )
        value["functions"].reverse()
        second = compile_requirements(
            RequirementsSpec.from_dict(value), graph=self.graph, registry=self.registry
        )
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(len(first.components), 12)
        self.assertEqual(
            {block.id for block in first.blocks},
            {
                "qwiic_power_input",
                "attiny402_core",
                "tmp102_i2c_sensor",
                "gpio_status_led",
            },
        )
        self.assertEqual(
            {net.name for net in first.nets},
            {"3V3", "GND", "I2C_SDA", "I2C_SCL", "UPDI", "LED_CTRL", "LED_ANODE"},
        )
        self.assertEqual(self.graph.validate_design(first, check_libraries=True), [])
        self.assertIn("requirements_hash", first.metadata)
        self.assertTrue(
            any(constraint.kind == "decoupling" for constraint in first.constraints)
        )
        self.assertEqual(
            {constraint.kind for constraint in first.constraints}
            >= {"i2c_electrical_budget", "source_ownership"},
            True,
        )
        interface = first.interfaces[0]
        self.assertEqual(interface.params["bus_capacitance_pf_max"], 200.0)
        self.assertEqual(interface.params["external_pullups"], "forbidden")
        self.assertAlmostEqual(interface.params["rise_time_limit_ns"], 1000.0)
        constraints = {constraint.id: constraint for constraint in first.constraints}
        self.assertEqual(
            constraints["decouple_mcu"].params["distance_metric"],
            "minimum_relevant_copper_pad_edge_gap",
        )
        self.assertEqual(
            constraints["routing_i2c"].params["min_reference_stitching_vias"], 2
        )
        self.assertEqual(constraints["power_budget"].params["max_power_w"], 0.36)
        self.assertEqual(
            constraints["power_budget"].params["envelope"],
            "simultaneous_declared_scope_maxima",
        )
        sense = next(
            endpoint
            for net in first.nets
            if net.id == "net_3v3"
            for endpoint in net.endpoints
            if endpoint.component == "updi_j2"
        )
        self.assertEqual(sense.role, "voltage_sense")

    def test_every_rule_validated_block_declares_parts_evidence_and_tests(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for definition in self.registry.definitions():
            self.assertEqual(definition.verification_state, "rule_validated")
            self.assertTrue(definition.evidence)
            self.assertTrue(definition.verification_tests)
            for relative in definition.verification_tests:
                self.assertTrue((repository / relative).is_file(), relative)
            for part_id in definition.required_parts:
                self.assertEqual(self.graph.get(part_id).id, part_id)
            instance = self.registry.instantiate(definition.id)
            self.assertEqual(instance.block.version, definition.version)
            self.assertTrue(instance.components)
            self.assertEqual(set(instance.ports), set(definition.ports))

    def test_missing_function_and_unsafe_supply_are_rejected(self) -> None:
        value = controller_requirements_dict()
        value["functions"] = [
            function
            for function in value["functions"]
            if function["kind"] != "status_indicator"
        ]
        with self.assertRaisesRegex(ValidationError, "requires functions"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

        value = controller_requirements_dict()
        value["power"]["max_v"] = 5.0
        with self.assertRaisesRegex(ValidationError, "requires a power maximum"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

    def test_board_rules_must_be_compatible_with_fine_pitch_package(self) -> None:
        value = controller_requirements_dict()
        value["board"]["min_clearance_mm"] = 0.2
        with self.assertRaisesRegex(
            ValidationError, "footprint_clearance_incompatible"
        ):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

    def test_high_risk_scope_is_never_silently_compiled(self) -> None:
        value = copy.deepcopy(controller_requirements_dict())
        value["scope"]["domains"].append("mains")
        value["scope"]["max_voltage_v"] = 325
        value["scope"]["max_power_w"] = 1000
        with self.assertRaisesRegex(
            ValidationError, "outside the automated acceptance scope"
        ):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

    def test_unknown_requirement_fields_and_functions_are_rejected(self) -> None:
        value = controller_requirements_dict()
        value["functions"][0]["execute_this"] = "untrusted instruction"
        with self.assertRaisesRegex(ValidationError, "fields"):
            RequirementsSpec.from_dict(value)

        value = controller_requirements_dict()
        value["functions"][0]["parameters"]["ignored"] = True
        with self.assertRaisesRegex(ValidationError, "exactly match"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

    def test_i2c_electrical_contract_is_bounded(self) -> None:
        value = controller_requirements_dict()
        value["interfaces"][0]["bus_capacitance_pf_max"] = 300
        with self.assertRaisesRegex(ValidationError, "rise-time budget"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

        value = controller_requirements_dict()
        value["interfaces"][0]["external_pullups"] = "unbounded"
        with self.assertRaisesRegex(ValidationError, "external_pullups=forbidden"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

    def test_power_scope_must_describe_one_unambiguous_envelope(self) -> None:
        value = controller_requirements_dict()
        value["scope"]["max_power_w"] = 0.33
        with self.assertRaisesRegex(ValidationError, "simultaneous maximum envelope"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

        value = controller_requirements_dict()
        value["power"]["max_current_a"] = 0.09
        with self.assertRaisesRegex(ValidationError, "simultaneous maximum envelope"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )

    def test_malformed_constraint_params_become_findings_not_exceptions(self) -> None:
        design = compile_requirements(
            RequirementsSpec.from_dict(controller_requirements_dict()),
            graph=self.graph,
            registry=self.registry,
        )
        document = design.to_dict()
        next(
            constraint
            for constraint in document["constraints"]
            if constraint["id"] == "routing_i2c"
        )["params"]["width_mm"] = "not-a-number"
        hostile = Design.from_dict(document, validate=False)
        findings = evaluate_semantic_rules(hostile, self.graph)
        self.assertIn(
            "intent.invalid_constraint_params",
            {finding.code for finding in findings},
        )

    def test_recognized_domain_without_bundled_generator_is_explicitly_rejected(
        self,
    ) -> None:
        value = controller_requirements_dict()
        value["scope"]["domains"].append("spi")
        with self.assertRaisesRegex(ValidationError, "does not implement domains: spi"):
            compile_requirements(
                RequirementsSpec.from_dict(value),
                graph=self.graph,
                registry=self.registry,
            )
        value = controller_requirements_dict()
        value["functions"][0]["kind"] = "pcie"
        with self.assertRaisesRegex(
            ValidationError, "unsupported requirements function"
        ):
            RequirementsSpec.from_dict(value)


if __name__ == "__main__":
    unittest.main()
