from __future__ import annotations

import copy
import unittest
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.blocks import BlockRegistry
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.profiles import build_requirements, product_profiles
from pcbdraft.domain.requirements import RequirementsSpec, compile_requirements
from pcbdraft.domain.semantic_rules import evaluate_semantic_rules
from tests.support.requirements_factory import controller_requirements_dict


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
        self.assertEqual(constraints["power_budget"].params["max_power_w"], 0.3465)
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
        repository = Path(__file__).resolve().parents[2]
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

    def test_legacy_fixture_reports_its_actual_unimplemented_domain(self) -> None:
        value = copy.deepcopy(controller_requirements_dict())
        value["scope"]["domains"].append("mains")
        value["scope"]["max_voltage_v"] = 325
        value["scope"]["max_power_w"] = 1000
        with self.assertRaisesRegex(
            ValidationError, "does not implement domains: mains"
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

    def test_legacy_fixture_reports_a_mismatched_domain_or_function(
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

    def test_legacy_fixture_profiles_compile_with_distinct_executable_contracts(
        self,
    ) -> None:
        expected = {
            "low_voltage_i2c_controller_v1": ({"i2c"}, "i2c_electrical_budget"),
            "low_voltage_spi_environment_v1": ({"spi"}, "spi_electrical_budget"),
            "low_voltage_uart_ldo_controller_v1": (
                {"uart"},
                "ldo_regulation_budget",
            ),
        }
        self.assertEqual({profile.id for profile in product_profiles()}, set(expected))
        hashes: set[str] = set()
        for profile in product_profiles():
            spec = build_requirements(
                profile.id,
                design_name=profile.title,
                design_id=profile.id,
                layers=2,
                width_mm=45,
                height_mm=30,
                source_locator="test-product-profile",
                source_date="2026-08-13",
            )
            design = compile_requirements(
                spec, graph=self.graph, registry=self.registry
            )
            interface_kinds, constraint_kind = expected[profile.id]
            self.assertEqual(design.metadata["profile"], profile.id)
            self.assertEqual({item.kind for item in design.interfaces}, interface_kinds)
            self.assertIn(constraint_kind, {item.kind for item in design.constraints})
            self.assertEqual(
                evaluate_semantic_rules(design, self.graph, approximate_geometry=False),
                (),
            )
            hashes.add(design.content_hash())
        self.assertEqual(len(hashes), 3)

    def test_product_profiles_reject_unverified_board_geometry(self) -> None:
        with self.assertRaisesRegex(ValidationError, "45 mm × 30 mm"):
            build_requirements(
                "low_voltage_spi_environment_v1",
                design_name="Unverified envelope",
                design_id="unverified_envelope",
                layers=2,
                width_mm=60,
                height_mm=40,
                source_locator="test-product-profile",
                source_date="2026-08-13",
            )


if __name__ == "__main__":
    unittest.main()
