from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent_design import (
    AgentDesignRequest,
    CircuitPlan,
    LocalKiCadPartResolver,
    circuit_plan_schema,
    compile_agent_plan,
    planner_symbol_context,
)
from pcbdraft.cli import main as cli_main
from pcbdraft.component_qualification import ComponentQualificationReport
from pcbdraft.errors import ValidationError
from pcbdraft.kicad_schematic import generate_schematic
from pcbdraft.managed import materialize_managed_design, open_managed_project
from pcbdraft.validation import validate_managed_project


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
        "source": {"locator": "tests/test_agent_design.py"},
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


class GenericAgentDesignTests(unittest.TestCase):
    def test_provider_schema_constants_have_explicit_json_types(self) -> None:
        properties = circuit_plan_schema()["properties"]
        self.assertEqual(
            properties["schema"],
            {"type": "string", "const": "pcbdraft-circuit-plan"},
        )
        self.assertEqual(properties["version"], {"type": "integer", "const": 1})

    def test_stock_plan_schema_does_not_require_procurement_metadata(self) -> None:
        schema = circuit_plan_schema()
        component = schema["properties"]["components"]["items"]
        self.assertNotIn("manufacturer", component["properties"])
        self.assertNotIn("mpn", component["properties"])
        self.assertNotIn("manufacturer", component["required"])
        self.assertNotIn("mpn", component["required"])
        CircuitPlan.from_dict(indicator_plan_dict())

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

    def test_cli_stock_generation_reports_route_without_claiming_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            plan_path = root / "plan.json"
            request_path.write_text(
                json.dumps(indicator_request_dict()), encoding="utf-8"
            )
            plan_path.write_text(json.dumps(indicator_plan_dict()), encoding="utf-8")
            captured = StringIO()
            with redirect_stdout(captured):
                exit_code = cli_main(
                    [
                        "agent-generate",
                        str(request_path),
                        str(plan_path),
                        str(root / "project"),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            result = json.loads(captured.getvalue())
            self.assertEqual(result["routing"]["state"], "completed")
            self.assertEqual(result["routing"]["unrouted"], [])
            self.assertEqual(result["validation"], "not_run")
            self.assertNotIn("plan_review", result)
            self.assertTrue(Path(result["root"]).is_dir())

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
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            CircuitPlan.from_dict(raw)

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
                patch("pcbdraft.managed.generate_pcb", side_effect=failure),
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

    def test_cli_generic_failure_retains_the_reviewed_plan_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            plan_path = root / "plan.json"
            request_path.write_text(json.dumps(agent_request_dict()), encoding="utf-8")
            plan_path.write_text(json.dumps(circuit_plan_dict()), encoding="utf-8")
            captured = StringIO()
            failure = ValidationError(
                "bounded router left 1 net(s) unrouted (TEST); "
                "expanded_nodes=10; reasons: deterministic test obstruction"
            )
            with (
                patch("pcbdraft.managed.generate_pcb", side_effect=failure),
                redirect_stdout(captured),
            ):
                exit_code = cli_main(
                    [
                        "agent-generate",
                        str(request_path),
                        str(plan_path),
                        str(root / "generated"),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 2)
            result = json.loads(captured.getvalue())
            attempt = Path(result["retained_attempt"])
            self.assertNotIn("plan_review", result)
            self.assertTrue(result["error"])
            self.assertTrue((attempt / "attempt.json").is_file())
            self.assertTrue((attempt / "circuit-plan.json").is_file())
            self.assertTrue(
                (attempt / "native" / "generic-stm32-sht31.kicad_sch").is_file()
            )


if __name__ == "__main__":
    unittest.main()
