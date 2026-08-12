from __future__ import annotations

import copy
import unittest

from pcb_agent.blocks import BlockRegistry
from pcb_agent.errors import ValidationError
from pcb_agent.parts import PartGraph
from pcb_agent.requirements import RequirementsSpec, compile_requirements
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

    def test_every_rule_validated_block_declares_parts_evidence_and_tests(self) -> None:
        for definition in self.registry.definitions():
            self.assertEqual(definition.verification_state, "rule_validated")
            self.assertTrue(definition.evidence)
            self.assertTrue(definition.verification_tests)
            for part_id in definition.required_parts:
                self.assertEqual(self.graph.get(part_id).id, part_id)
            instance = self.registry.instantiate(definition.id)
            self.assertEqual(instance.block.version, definition.version)
            self.assertTrue(instance.components)

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
        value["functions"][0]["kind"] = "pcie"
        with self.assertRaisesRegex(
            ValidationError, "unsupported requirements function"
        ):
            RequirementsSpec.from_dict(value)


if __name__ == "__main__":
    unittest.main()
