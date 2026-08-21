from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.design import (
    AgentDesignRequest,
    CircuitPlan,
    LocalKiCadPartResolver,
    circuit_plan_schema,
    compile_agent_plan,
    planner_symbol_context,
)
from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.component_qualification import ComponentQualificationReport
from pcbdraft.domain.semantic_rules import evaluate_semantic_rules
from pcbdraft.kicad.schematic import generate_schematic
from pcbdraft.services.managed import materialize_managed_design, open_managed_project
from pcbdraft.verification.validation import validate_managed_project


def agent_request_dict() -> dict[str, object]:
    return {
        "schema": "pcbdraft-agent-design-request",
        "version": 1,
        "design_id": "generic-stm32-sht31",
        "name": "Generic STM32 sensor board",
        "revision": "A",
        "request_summary": "An STM32F405 reads an SHT31 sensor over I2C.",
        "scope": {
            "domains": ["i2c", "low_voltage_mcu", "sensor", "simple_control"],
            "max_voltage_v": 3.3,
            "max_current_a": 0.5,
            "max_power_w": 1.65,
            "layers": 2,
            "intended_use": "prototype",
            "risk_class": "prototype",
        },
        "board": {
            "width_mm": 80.0,
            "height_mm": 50.0,
            "layers": 2,
            "thickness_mm": 1.6,
            "edge_clearance_mm": 0.5,
            "min_track_mm": 0.2,
            "min_clearance_mm": 0.15,
            "min_drill_mm": 0.3,
            "finish": "enig",
        },
        "assumptions": ["A regulated 3.3 V supply is available."],
        "requested_parts": ["STM32F405", "SHT31"],
        "functions": ["sensor acquisition"],
        "power": {
            "nominal_v": 3.3,
            "max_voltage_v": 3.3,
            "max_current_a": 0.5,
            "max_power_w": 1.65,
        },
        "source": {"locator": "tests/agent/test_design.py"},
    }


def circuit_plan_dict() -> dict[str, object]:
    return {
        "schema": "pcbdraft-circuit-plan",
        "version": 1,
        "design_id": "generic-stm32-sht31",
        "summary": "A provisional STM32F405/SHT31 I2C topology.",
        "assumptions": [],
        "notes": ["Pin, power, and layout review remain required."],
        "components": [
            {
                "id": "mcu",
                "reference": "U1",
                "symbol": "MCU_ST_STM32F4:STM32F405RGTx",
                "value": "STM32F405",
                "role": "microcontroller",
                "footprint": None,
                "on_board": True,
                "exact_name": "STM32F405",
            },
            {
                "id": "sensor",
                "reference": "U2",
                "symbol": "Sensor_Humidity:SHT31-DIS",
                "value": "SHT31",
                "role": "humidity_sensor",
                "footprint": None,
                "on_board": True,
                "exact_name": "SHT31",
            },
        ],
        "nets": [
            {
                "id": "gnd",
                "name": "GND",
                "net_class": "power",
                "intent": "Common return.",
                "endpoints": [
                    {"component": "mcu", "pin": "12", "role": "return"},
                    {"component": "sensor", "pin": "8", "role": "return"},
                ],
            },
            {
                "id": "v3v3",
                "name": "3V3",
                "net_class": "power",
                "intent": "Regulated logic supply.",
                "endpoints": [
                    {"component": "mcu", "pin": "19", "role": "load"},
                    {"component": "sensor", "pin": "5", "role": "load"},
                ],
            },
            {
                "id": "i2c_sda",
                "name": "I2C_SDA",
                "net_class": "signal",
                "intent": "I2C data.",
                "endpoints": [
                    {"component": "mcu", "pin": "58", "role": "controller"},
                    {"component": "sensor", "pin": "1", "role": "peripheral"},
                ],
            },
            {
                "id": "i2c_scl",
                "name": "I2C_SCL",
                "net_class": "signal",
                "intent": "I2C clock.",
                "endpoints": [
                    {"component": "mcu", "pin": "59", "role": "controller"},
                    {"component": "sensor", "pin": "4", "role": "peripheral"},
                ],
            },
        ],
    }


def indicator_request_dict() -> dict[str, object]:
    request = copy.deepcopy(agent_request_dict())
    request.update(
        {
            "design_id": "generic-led-indicator",
            "name": "Generic LED indicator",
            "request_summary": "A low-voltage LED indicator board with a two-pin power input.",
            "requested_parts": [],
            "functions": ["status indicator"],
            "scope": {
                "domains": ["simple_control"],
                "max_voltage_v": 3.3,
                "max_current_a": 0.02,
                "max_power_w": 0.066,
                "layers": 2,
                "intended_use": "prototype",
                "risk_class": "prototype",
            },
            "power": {
                "nominal_v": 3.3,
                "max_voltage_v": 3.3,
                "max_current_a": 0.02,
                "max_power_w": 0.066,
            },
        }
    )
    return request


def indicator_plan_dict() -> dict[str, object]:
    return {
        "schema": "pcbdraft-circuit-plan",
        "version": 1,
        "design_id": "generic-led-indicator",
        "summary": "A generic connector, series resistor, and LED topology.",
        "assumptions": ["The external source is regulated to 3.3 V."],
        "notes": ["LED current and resistor value require human review."],
        "components": [
            {
                "id": "capacitor",
                "reference": "C1",
                "symbol": "Device:C",
                "value": "100n",
                "role": "supply_bypass",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
                "on_board": True,
                "exact_name": None,
            },
            {
                "id": "input",
                "reference": "J1",
                "symbol": "Connector_Generic:Conn_01x02",
                "value": "POWER",
                "role": "power_input_connector",
                "footprint": "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
                "on_board": True,
                "exact_name": None,
            },
            {
                "id": "resistor",
                "reference": "R1",
                "symbol": "Device:R",
                "value": "1k",
                "role": "led_current_limit",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "on_board": True,
                "exact_name": None,
            },
            {
                "id": "led",
                "reference": "D1",
                "symbol": "Device:LED",
                "value": "LED",
                "role": "indicator",
                "footprint": "LED_SMD:LED_0603_1608Metric",
                "on_board": True,
                "exact_name": None,
            },
        ],
        "nets": [
            {
                "id": "gnd",
                "name": "GND",
                "net_class": "power",
                "intent": "Common return.",
                "endpoints": [
                    {"component": "capacitor", "pin": "2", "role": "return"},
                    {"component": "input", "pin": "2", "role": "return"},
                    {"component": "led", "pin": "1", "role": "return"},
                ],
            },
            {
                "id": "v3v3",
                "name": "3V3",
                "net_class": "power",
                "intent": "External regulated source.",
                "endpoints": [
                    {"component": "capacitor", "pin": "1", "role": "load"},
                    {"component": "input", "pin": "1", "role": "source"},
                    {"component": "resistor", "pin": "1", "role": "load"},
                ],
            },
            {
                "id": "led_a",
                "name": "LED_A",
                "net_class": "signal",
                "intent": "Current-limited LED anode.",
                "endpoints": [
                    {"component": "resistor", "pin": "2", "role": "load"},
                    {"component": "led", "pin": "2", "role": "load"},
                ],
            },
        ],
    }


def indicator_plan_v2_dict() -> dict[str, object]:
    plan = copy.deepcopy(indicator_plan_dict())
    plan["version"] = 2
    for net in plan["nets"]:
        net["power_domain"] = "logic_3v3" if net["id"] in {"gnd", "v3v3"} else None
        net["interface"] = "status_led" if net["id"] == "led_a" else None
    plan.update(
        {
            "blocks": [
                {
                    "id": "prototype",
                    "kind": "board_system",
                    "name": "Indicator prototype",
                    "intent": "Top-level functional assembly.",
                    "parent": None,
                    "components": [],
                },
                {
                    "id": "power_entry",
                    "kind": "power_entry",
                    "name": "Power entry",
                    "intent": "Accept and bypass the external supply.",
                    "parent": "prototype",
                    "components": ["capacitor", "input"],
                },
                {
                    "id": "status_indicator",
                    "kind": "status_indicator",
                    "name": "Status indicator",
                    "intent": "Limit current and emit visible status.",
                    "parent": "prototype",
                    "components": ["led", "resistor"],
                },
            ],
            "power_domains": [
                {
                    "id": "logic_3v3",
                    "nominal_v": 3.3,
                    "min_v": 3.3,
                    "max_v": 3.3,
                    "max_current_a": 0.02,
                    "source": {
                        "component": "input",
                        "pin": "1",
                        "role": "source",
                    },
                    "intent": "Regulated indicator supply.",
                }
            ],
            "interfaces": [
                {
                    "id": "status_led",
                    "kind": "status_indicator",
                    "power_domain": "logic_3v3",
                    "members": [
                        {"component": "led", "pin": "2", "role": "load"},
                        {"component": "resistor", "pin": "2", "role": "load"},
                    ],
                    "controller": None,
                    "parameters": [],
                    "intent": "Current-limited visible indicator path.",
                }
            ],
            "constraints": [
                {
                    "id": "indicator_group",
                    "kind": "functional_group",
                    "targets": ["led", "resistor"],
                    "parameters": [{"name": "max_diameter_mm", "value": 15.0}],
                    "severity": "required",
                    "rationale": "Keep the series element near the indicator.",
                },
                {
                    "id": "indicator_route_width",
                    "kind": "routing",
                    "targets": ["led_a"],
                    "parameters": [{"name": "width_mm", "value": 0.25}],
                    "severity": "required",
                    "rationale": "Retain a normal prototype signal width.",
                },
                {
                    "id": "input_pinout",
                    "kind": "connector_pinout",
                    "targets": ["input"],
                    "parameters": [
                        {"name": "pin.1", "value": "v3v3"},
                        {"name": "pin.2", "value": "gnd"},
                        {"name": "require_complete", "value": True},
                    ],
                    "severity": "required",
                    "rationale": "Preserve the external power connector contract.",
                },
                {
                    "id": "status_net_label",
                    "kind": "net_label",
                    "targets": ["led_a"],
                    "parameters": [{"name": "label", "value": "LED_A"}],
                    "severity": "required",
                    "rationale": "Preserve the reviewed status-net identity.",
                },
                {
                    "id": "indicator_region",
                    "kind": "placement_region",
                    "targets": ["led", "resistor"],
                    "parameters": [{"name": "region", "value": "right"}],
                    "severity": "required",
                    "rationale": "Place the visible indicator in the right board third.",
                },
                {
                    "id": "center_reserved",
                    "kind": "board_keepout",
                    "targets": ["board"],
                    "parameters": [
                        {"name": "anchor", "value": "center"},
                        {"name": "height_mm", "value": 2.0},
                        {"name": "layers", "value": "all"},
                        {"name": "width_mm", "value": 2.0},
                    ],
                    "severity": "required",
                    "rationale": "Reserve a small central routing and placement area.",
                },
            ],
            "assertions": [
                {
                    "id": "indicator_series_path",
                    "kind": "components_share_net",
                    "targets": ["led", "resistor"],
                    "minimum": None,
                    "maximum": None,
                    "severity": "required",
                    "rationale": "The resistor and LED must share their series net.",
                }
            ],
        }
    )
    return plan


class GenericAgentDesignTests(unittest.TestCase):
    def test_provider_schema_constants_have_explicit_json_types(self) -> None:
        properties = circuit_plan_schema()["properties"]
        self.assertEqual(
            properties["schema"],
            {"type": "string", "const": "pcbdraft-circuit-plan"},
        )
        self.assertEqual(properties["version"], {"type": "integer", "const": 2})
        self.assertTrue(
            {"blocks", "power_domains", "interfaces", "constraints", "assertions"}
            <= set(circuit_plan_schema()["required"])
        )

    def test_stock_plan_schema_does_not_require_procurement_metadata(self) -> None:
        schema = circuit_plan_schema()
        component = schema["properties"]["components"]["items"]
        self.assertNotIn("manufacturer", component["properties"])
        self.assertNotIn("mpn", component["properties"])
        self.assertNotIn("manufacturer", component["required"])
        self.assertNotIn("mpn", component["required"])
        CircuitPlan.from_dict(indicator_plan_dict())

    def test_legacy_plan_round_trip_remains_byte_compatible(self) -> None:
        source = indicator_plan_dict()
        plan = CircuitPlan.from_dict(source)
        serialized = plan.to_dict()
        self.assertEqual(plan.version, 1)
        self.assertNotIn("blocks", serialized)
        self.assertNotIn("power_domains", serialized)
        self.assertNotIn("interface", serialized["nets"][0])
        self.assertEqual(
            CircuitPlan.from_dict(serialized).canonical_bytes(),
            plan.canonical_bytes(),
        )

    def test_boolean_plan_version_is_rejected(self) -> None:
        source = indicator_plan_dict()
        source["version"] = True
        with self.assertRaisesRegex(ValidationError, "unsupported circuit plan"):
            CircuitPlan.from_dict(source)

    def test_v2_plan_populates_hierarchical_semantic_ir(self) -> None:
        plan = CircuitPlan.from_dict(indicator_plan_v2_dict())
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()), plan
        )

        design = compilation.design
        self.assertEqual(design.metadata["generator"], "agent_plan_v2")
        self.assertEqual(
            design.metadata["block_hierarchy"],
            [
                {"id": "power_entry", "parent": "prototype"},
                {"id": "prototype", "parent": None},
                {"id": "status_indicator", "parent": "prototype"},
            ],
        )
        self.assertEqual(
            {block.id for block in design.blocks},
            {"power_entry", "prototype", "status_indicator"},
        )
        self.assertEqual([domain.id for domain in design.power_domains], ["logic_3v3"])
        self.assertEqual(
            [interface.id for interface in design.interfaces], ["status_led"]
        )
        self.assertEqual(
            next(net for net in design.nets if net.id == "led_a").interface,
            "status_led",
        )
        self.assertEqual(
            {constraint.kind for constraint in design.constraints},
            {
                "assertion",
                "board_keepout",
                "connector_pinout",
                "functional_group",
                "manufacturing_rules",
                "net_label",
                "placement_region",
                "routing",
            },
        )
        outcomes = {
            finding.id: finding.outcome for finding in compilation.review.findings
        }
        self.assertEqual(outcomes["assertion.indicator_series_path"], "pass")
        serialized = plan.to_dict()
        self.assertEqual(serialized["version"], 2)
        self.assertIn("blocks", serialized)
        self.assertEqual(
            CircuitPlan.from_dict(serialized).canonical_bytes(),
            plan.canonical_bytes(),
        )

    def test_power_domain_source_role_is_metadata_not_physical_identity(self) -> None:
        source = indicator_plan_v2_dict()
        source["power_domains"][0]["source"]["role"] = "vcc"

        plan = CircuitPlan.from_dict(source)

        self.assertEqual(plan.power_domains[0].source.role, "vcc")

    def test_v2_plan_rejects_incomplete_block_coverage(self) -> None:
        source = indicator_plan_v2_dict()
        source["blocks"][1]["components"].remove("input")
        with self.assertRaisesRegex(ValidationError, "cover every component"):
            CircuitPlan.from_dict(source)

    def test_v2_failed_assertion_is_reported_before_generation(self) -> None:
        source = indicator_plan_v2_dict()
        source["assertions"] = [
            {
                "id": "too_many_led_endpoints",
                "kind": "net_endpoint_count",
                "targets": ["led_a"],
                "minimum": 3,
                "maximum": None,
                "severity": "required",
                "rationale": "Exercise deterministic assertion failure.",
            }
        ]
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(source),
        )
        finding = next(
            item
            for item in compilation.review.findings
            if item.id == "assertion.too_many_led_endpoints"
        )
        self.assertEqual(finding.outcome, "fail")
        self.assertIn("below minimum", finding.summary)
        semantic_finding = next(
            item
            for item in evaluate_semantic_rules(
                compilation.design,
                compilation.graph,
                allow_provisional=True,
            )
            if item.code == "intent.assertion"
            and item.object_id == "assert.too_many_led_endpoints"
        )
        self.assertIn("below minimum", semantic_finding.message)

    def test_v2_plan_rejects_cyclic_blocks_and_unbound_interfaces(self) -> None:
        cyclic = indicator_plan_v2_dict()
        cyclic["blocks"][0]["parent"] = "status_indicator"
        with self.assertRaisesRegex(ValidationError, "hierarchy contains a cycle"):
            CircuitPlan.from_dict(cyclic)

        unbound = indicator_plan_v2_dict()
        next(net for net in unbound["nets"] if net["id"] == "led_a")["interface"] = None
        with self.assertRaisesRegex(ValidationError, "assigned to at least one net"):
            CircuitPlan.from_dict(unbound)

    def test_v2_connector_pinout_is_recomputed_from_local_symbol_pins(self) -> None:
        alphanumeric = indicator_plan_v2_dict()
        alphanumeric_pinout = next(
            item for item in alphanumeric["constraints"] if item["id"] == "input_pinout"
        )
        next(
            item
            for item in alphanumeric_pinout["parameters"]
            if item["name"] == "pin.1"
        )["name"] = "pin.A1"
        parsed = CircuitPlan.from_dict(alphanumeric)
        parsed_pinout = next(
            item for item in parsed.constraints if item.id == "input_pinout"
        )
        self.assertIn("pin.A1", parsed_pinout.parameters)

        source = indicator_plan_v2_dict()
        pinout = next(
            item for item in source["constraints"] if item["id"] == "input_pinout"
        )
        next(item for item in pinout["parameters"] if item["name"] == "pin.1")[
            "value"
        ] = "led_a"
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(source),
        )
        finding = next(
            item
            for item in evaluate_semantic_rules(
                compilation.design,
                compilation.graph,
                allow_provisional=True,
            )
            if item.code == "intent.connector_pinout"
        )
        self.assertIn("pin 1 expected led_a", finding.message)

    def test_v2_rejects_label_drift_and_undersized_differential_gap(self) -> None:
        label_drift = indicator_plan_v2_dict()
        label = next(
            item
            for item in label_drift["constraints"]
            if item["id"] == "status_net_label"
        )
        label["parameters"][0]["value"] = "STATUS_CHANGED"
        with self.assertRaisesRegex(ValidationError, "label disagrees"):
            CircuitPlan.from_dict(label_drift)

        pair = indicator_plan_v2_dict()
        pair["constraints"].append(
            {
                "id": "test_pair",
                "kind": "differential_pair",
                "targets": ["gnd", "v3v3"],
                "parameters": [
                    {"name": "gap_mm", "value": 0.1},
                    {"name": "gap_tolerance_mm", "value": 0.05},
                    {"name": "max_length_mismatch_mm", "value": 1.0},
                    {"name": "min_coupled_length_ratio", "value": 0.8},
                    {"name": "width_mm", "value": 0.25},
                ],
                "severity": "release_blocking",
                "rationale": "Exercise board-rule enforcement.",
            }
        )
        with self.assertRaisesRegex(ValidationError, "gap is below the board minimum"):
            compile_agent_plan(
                AgentDesignRequest.from_dict(indicator_request_dict()),
                CircuitPlan.from_dict(pair),
            )

        conflict = indicator_plan_v2_dict()
        conflict["constraints"].append(
            {
                "id": "conflicting_pair",
                "kind": "differential_pair",
                "targets": ["gnd", "led_a"],
                "parameters": [
                    {"name": "gap_mm", "value": 0.2},
                    {"name": "gap_tolerance_mm", "value": 0.05},
                    {"name": "max_length_mismatch_mm", "value": 1.0},
                    {"name": "min_coupled_length_ratio", "value": 0.8},
                    {"name": "width_mm", "value": 0.3},
                ],
                "severity": "release_blocking",
                "rationale": "Exercise conflicting-width rejection.",
            }
        )
        with self.assertRaisesRegex(ValidationError, "conflicting route widths"):
            compile_agent_plan(
                AgentDesignRequest.from_dict(indicator_request_dict()),
                CircuitPlan.from_dict(conflict),
            )

    def test_v2_keepout_does_not_pass_when_native_receipt_is_missing(self) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(indicator_plan_v2_dict()),
        )
        finding = next(
            item
            for item in evaluate_semantic_rules(
                compilation.design,
                compilation.graph,
                routing={"constraint_metrics": {}},
                allow_provisional=True,
            )
            if item.code == "intent.board_keepout"
        )
        self.assertIn("evidence is missing", finding.message)

    def test_installed_kicad_symbols_are_discovered_without_a_profile(self) -> None:
        resolver = LocalKiCadPartResolver()
        stm32 = resolver.find("STM32F405")
        sht31 = resolver.find("SHT31")
        self.assertTrue(any(item.symbol.endswith(":STM32F405RGTx") for item in stm32))
        self.assertEqual(sht31[0].symbol, "Sensor_Humidity:SHT31-DIS")

    def test_basic_runtime_primitives_include_stock_footprint_choices(self) -> None:
        request = AgentDesignRequest.from_dict(indicator_request_dict())
        context = planner_symbol_context(request)["_runtime_primitives"]
        footprints = {entry["symbol"]: entry["footprint"] for entry in context}
        self.assertEqual(footprints["Device:R"], "Resistor_SMD:R_0603_1608Metric")
        self.assertEqual(footprints["Device:C"], "Capacitor_SMD:C_0603_1608Metric")
        self.assertEqual(footprints["Device:LED"], "LED_SMD:LED_0603_1608Metric")
        self.assertEqual(
            footprints["Connector_Generic:Conn_01x02"],
            "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
        )

    def test_plan_rejects_a_footprint_missing_from_stock_kicad(self) -> None:
        plan_data = indicator_plan_dict()
        plan_data["components"][1]["footprint"] = "Vendor_Private:R_0603"
        with self.assertRaisesRegex(ValidationError, "part.footprint_unavailable"):
            compile_agent_plan(
                AgentDesignRequest.from_dict(indicator_request_dict()),
                CircuitPlan.from_dict(plan_data),
            )

    def test_generic_plan_creates_project_local_part_records(self) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(agent_request_dict()),
            CircuitPlan.from_dict(circuit_plan_dict()),
        )
        records = [
            compilation.graph.get(component.part_id)
            for component in compilation.design.components
        ]
        self.assertEqual(
            {record.symbol for record in records},
            {"MCU_ST_STM32F4:STM32F405RGTx", "Sensor_Humidity:SHT31-DIS"},
        )
        self.assertTrue(all(record.trust == "extracted" for record in records))
        self.assertEqual(
            len(compilation.graph.to_dict()["parts"]),
            len(circuit_plan_dict()["components"]),
        )
        self.assertEqual(compilation.design.metadata["generator"], "agent_plan_v1")
        self.assertEqual(compilation.design.metadata["assurance"], "provisional")
        qualification = compilation.review.qualification.to_dict()
        self.assertEqual(qualification["schema"], "pcbdraft-component-qualification")
        self.assertEqual(qualification["summary"]["pad_mapping_failures"], 0)
        by_reference = {
            entry["reference"]: entry for entry in qualification["components"]
        }
        self.assertEqual(by_reference["U1"]["datasheet"]["state"], "reference_only")
        self.assertEqual(by_reference["U1"]["overall_state"], "local_library_only")

    def test_plan_rejects_symbol_to_footprint_pad_mismatch(self) -> None:
        plan = copy.deepcopy(circuit_plan_dict())
        plan["components"][1]["footprint"] = "Resistor_SMD:R_0603_1608Metric"
        with self.assertRaisesRegex(ValidationError, "footprint_pad_mapping"):
            compile_agent_plan(
                AgentDesignRequest.from_dict(agent_request_dict()),
                CircuitPlan.from_dict(plan),
            )

    def test_component_qualification_rejects_inconsistent_or_malformed_evidence(
        self,
    ) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(indicator_plan_dict()),
        )
        inconsistent = compilation.review.qualification.to_dict()
        inconsistent["summary"]["pad_mapping_failures"] = 99
        with self.assertRaisesRegex(ValidationError, "derived fields"):
            ComponentQualificationReport.from_dict(inconsistent)

        malformed = compilation.review.qualification.to_dict()
        malformed["components"][0]["datasheet"]["state"] = []
        with self.assertRaisesRegex(ValidationError, "datasheet.state"):
            ComponentQualificationReport.from_dict(malformed)

    def test_preflight_detects_reversed_ground_referenced_led(self) -> None:
        plan = copy.deepcopy(indicator_plan_dict())
        ground = next(net for net in plan["nets"] if net["id"] == "gnd")
        led_path = next(net for net in plan["nets"] if net["id"] == "led_a")
        next(item for item in ground["endpoints"] if item["component"] == "led")[
            "pin"
        ] = "2"
        next(item for item in led_path["endpoints"] if item["component"] == "led")[
            "pin"
        ] = "1"
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(plan),
        )
        outcomes = {
            finding.id: finding.outcome for finding in compilation.review.findings
        }
        self.assertEqual(outcomes["electrical.led_ground_polarity"], "fail")

    def test_preflight_reports_missing_electrical_evidence_without_refusing_attempt(
        self,
    ) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(agent_request_dict()),
            CircuitPlan.from_dict(circuit_plan_dict()),
        )
        review = compilation.review.to_dict()
        outcomes = {item["id"]: item["outcome"] for item in review["findings"]}
        self.assertNotIn("attempt_allowed", review)
        self.assertNotIn("release_allowed", review)
        self.assertEqual(outcomes["power.input_pin_coverage"], "fail")
        self.assertEqual(outcomes["power.rail_source_evidence"], "fail")
        self.assertEqual(outcomes["interface.i2c_pullup_evidence"], "fail")
        self.assertEqual(outcomes["power.decoupling_evidence"], "fail")
        self.assertEqual(outcomes["parts.footprint_pad_mapping"], "pass")
        self.assertEqual(
            outcomes["parts.datasheet_and_identity_qualification"], "unknown"
        )
        self.assertEqual(outcomes["constraints.board_manufacturing_envelope"], "pass")

    def test_generic_managed_project_persists_reviewed_circuit_plan(self) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(indicator_plan_dict()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = materialize_managed_design(
                compilation.request,
                compilation.design,
                root / "project",
                graph=compilation.graph,
                plan=compilation.plan,
            )
            self.assertTrue((generated.project.root / "circuit-plan.json").is_file())
            self.assertTrue(
                (generated.project.root / "component-qualification.json").is_file()
            )
            self.assertIn("circuit_plan", generated.project.manifest["files"])
            self.assertIn(
                "component_qualification", generated.project.manifest["files"]
            )
            reopened = open_managed_project(generated.project.root)
            self.assertIsNotNone(reopened.plan)
            self.assertIsNotNone(reopened.qualification)
            self.assertEqual(reopened.plan.to_dict(), compilation.plan.to_dict())
            self.assertEqual(
                reopened.qualification.to_dict(),
                compilation.review.qualification.to_dict(),
            )
            self.assertEqual(reopened.drift(), ())

    def test_v2_spatial_and_identity_constraints_reach_native_validation(
        self,
    ) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(indicator_plan_v2_dict()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = materialize_managed_design(
                compilation.request,
                compilation.design,
                root / "project",
                graph=compilation.graph,
                plan=compilation.plan,
            )
            self.assertEqual(generated.pcb.routing.state, "completed")
            self.assertEqual(generated.pcb.routing.unrouted, ())
            metrics = generated.project.manifest["generation"]["pcb"][
                "constraint_metrics"
            ]
            self.assertEqual(metrics["indicator_region"]["outcome"], "pass")
            self.assertEqual(metrics["center_reserved"]["outcome"], "pass")
            self.assertEqual(metrics["center_reserved"]["copper_violations"], [])

            validation = validate_managed_project(
                generated.project,
                output=root / "validation",
            )
            checks = {
                check.id: check.outcome
                for level in validation.levels
                for check in level.checks
            }
            for check_id in (
                "l3.center_reserved",
                "l3.indicator_region",
                "l3.input_pinout",
                "l3.status_net_label",
            ):
                self.assertEqual(checks[check_id], "pass", check_id)

    @unittest.skipUnless(shutil.which("kicad-cli"), "real KiCad CLI unavailable")
    def test_stock_kicad_example_routes_and_passes_real_erc_drc(self) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(indicator_request_dict()),
            CircuitPlan.from_dict(indicator_plan_dict()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = materialize_managed_design(
                compilation.request,
                compilation.design,
                root / "project",
                graph=compilation.graph,
                plan=compilation.plan,
            )
            self.assertEqual(generated.pcb.routing.state, "completed")
            self.assertEqual(generated.pcb.routing.unrouted, ())
            self.assertGreaterEqual(len(generated.pcb.routing.vias), 1)

            reports = {}
            commands = {
                "erc": [
                    "kicad-cli",
                    "sch",
                    "erc",
                    "--format",
                    "json",
                    "--output",
                    str(root / "erc.json"),
                    str(generated.project.schematic_path),
                ],
                "drc": [
                    "kicad-cli",
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--output",
                    str(root / "drc.json"),
                    "--schematic-parity",
                    str(generated.project.board_path),
                ],
            }
            for name, argv in commands.items():
                result = subprocess.run(
                    argv,
                    cwd=generated.project.root,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stderr.decode("utf-8", errors="replace"),
                )
                reports[name] = json.loads((root / f"{name}.json").read_text())

            self.assertEqual(
                [
                    violation
                    for sheet in reports["erc"]["sheets"]
                    for violation in sheet["violations"]
                ],
                [],
            )
            self.assertEqual(reports["drc"]["violations"], [])
            self.assertEqual(reports["drc"]["unconnected_items"], [])
            self.assertEqual(reports["drc"]["schematic_parity"], [])

            validation = validate_managed_project(
                generated.project, output=root / "validation"
            )
            self.assertTrue(validation.candidate_ready)
            self.assertFalse(validation.production_ready)
            checks = {
                check.id: check for level in validation.levels for check in level.checks
            }
            self.assertEqual(checks["l0.local_stock_library_data"].outcome, "pass")
            self.assertFalse(checks["l0.local_stock_library_data"].blocks_candidate)
            self.assertEqual(checks["l1.footprint_pad_qualification"].outcome, "pass")
            self.assertEqual(
                checks["l1.component_evidence_qualification"].outcome, "unknown"
            )
            self.assertEqual(
                checks["l1.agent_plan_electrical_preflight"].outcome, "pass"
            )
            self.assertEqual(checks["l4.bom_lifecycle"].outcome, "unknown")
            self.assertFalse(checks["l4.bom_lifecycle"].blocks_candidate)

    def test_plan_cannot_drop_a_user_named_part_or_inject_raw_kicad(self) -> None:
        missing = copy.deepcopy(circuit_plan_dict())
        missing["components"] = [missing["components"][1]]
        for net in missing["nets"]:
            net["endpoints"] = [
                endpoint
                for endpoint in net["endpoints"]
                if endpoint["component"] == "sensor"
            ]
        with self.assertRaisesRegex(ValidationError, "preserve explicitly requested"):
            compile_agent_plan(
                AgentDesignRequest.from_dict(agent_request_dict()),
                CircuitPlan.from_dict(missing),
            )

        raw = copy.deepcopy(circuit_plan_dict())
        raw["components"][0]["x_mm"] = 12
        plan = CircuitPlan.from_dict(raw)
        # Unknown model-authored fields are ignored (tolerant plan boundary);
        # the deterministic compiler remains the authority on geometry.
        self.assertIsNone(getattr(plan.components[0], "x_mm", None))

    def test_generic_plan_generates_a_native_kicad_schematic(self) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(agent_request_dict()),
            CircuitPlan.from_dict(circuit_plan_dict()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            generated = generate_schematic(
                compilation.design,
                Path(temporary) / "generic.kicad_sch",
                graph=compilation.graph,
            )
            self.assertTrue(generated.path.is_file())
            native = generated.path.read_text(encoding="utf-8")
            self.assertIn("STM32F405RGTx", native)
            self.assertIn("SHT31-DIS", native)

    def test_generic_native_failure_retains_schematic_and_semantic_evidence(
        self,
    ) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(agent_request_dict()),
            CircuitPlan.from_dict(circuit_plan_dict()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "generated"
            retained = root / "retained-attempt"
            failure = ValidationError(
                "bounded router left 1 net(s) unrouted (TEST); "
                "expanded_nodes=10; reasons: deterministic test obstruction"
            )
            with (
                patch("pcbdraft.services.managed.generate_pcb", side_effect=failure),
                self.assertRaisesRegex(ValidationError, "bounded router"),
            ):
                materialize_managed_design(
                    compilation.request,
                    compilation.design,
                    output,
                    graph=compilation.graph,
                    plan=compilation.plan,
                    retain_failed_attempt=retained,
                )
            self.assertFalse(output.exists())
            for relative in (
                "requirements.pcbreq.json",
                "circuit-plan.json",
                "component-qualification.json",
                "design.pcbir.json",
                "parts.pcbdraft.json",
                "generic-stm32-sht31.kicad_sch",
            ):
                self.assertTrue((retained / relative).is_file(), relative)

    @unittest.skipUnless(shutil.which("kicad-cli"), "real KiCad CLI unavailable")
    def test_fine_pitch_generic_plan_completes_bounded_routing(self) -> None:
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(agent_request_dict()),
            CircuitPlan.from_dict(circuit_plan_dict()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            generated = materialize_managed_design(
                compilation.request,
                compilation.design,
                Path(temporary) / "project",
                graph=compilation.graph,
                plan=compilation.plan,
            )
            self.assertEqual(generated.pcb.routing.state, "completed")
            self.assertEqual(generated.pcb.routing.unrouted, ())
            self.assertGreater(generated.pcb.routing.expanded_nodes, 0)


if __name__ == "__main__":
    unittest.main()
