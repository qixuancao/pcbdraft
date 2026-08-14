from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcbdraft.api import handle_request
from tests.requirements_factory import controller_requirements_dict
from tests.test_agent_design import (
    agent_request_dict,
    circuit_plan_dict,
    indicator_plan_dict,
    indicator_request_dict,
)


class JsonRpcApiTests(unittest.TestCase):
    def test_capabilities_are_versioned_and_high_level(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "runtime.capabilities",
                "params": {},
            }
        )
        result = response["result"]
        self.assertEqual(result["api_version"], "1.0")
        self.assertIn("agent.plan.compile", result["methods"])
        self.assertIn("agent.project.generate", result["methods"])
        self.assertIn("symbols.find", result["methods"])
        self.assertIn("sync.preview", result["methods"])
        self.assertIn("benchmark.run", result["methods"])
        self.assertIn("release.verify", result["methods"])
        self.assertNotIn("accepted_scope", result)
        self.assertNotIn("scope_policy", result)
        self.assertEqual(
            result["generation"]["layers"],
            "agent_selected_or_user_specified; checked by installed KiCad during generation",
        )
        self.assertEqual(
            result["generation"]["domain_requests"],
            "attempted_without_preemptive_rejection",
        )
        self.assertEqual(
            result["generation"]["component_libraries"],
            "installed_stock_kicad_only",
        )
        self.assertEqual(
            result["agent_runtime"]["plan_schema"], "pcbdraft-circuit-plan"
        )
        self.assertIn("requirements.compile", result["legacy_fixture_methods"])

    def test_requirements_compile_returns_typed_design_without_writes(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": "compile",
                "method": "requirements.compile",
                "params": {"requirements": controller_requirements_dict()},
            }
        )
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["design"]["schema"], "pcbdraft-ir")
        self.assertEqual(len(response["result"]["content_hash"]), 64)

    def test_dsh_agent_request_prepare_keeps_agent_selected_stackup(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": "dsh-prepare",
                "method": "agent.request.prepare",
                "params": {
                    "request_summary": "Design a 6-layer 3.3 V SPI BME280 sensor board",
                    "design_name": "BME280 sensor board",
                    "layers": 6,
                    "requested_parts": ["BME280"],
                    "functions": ["SPI sensor interface"],
                },
            }
        )
        self.assertNotIn("error", response)
        result = response["result"]
        request = result["request"]
        self.assertEqual(request["board"]["layers"], 6)
        self.assertEqual(request["scope"]["layers"], 6)
        self.assertEqual(request["requested_parts"], ["BME280"])
        self.assertIn("_runtime_primitives", result["symbol_context"])
        self.assertEqual(
            result["plan_schema"]["properties"]["schema"]["const"],
            "pcbdraft-circuit-plan",
        )

    def test_generic_agent_plan_compiles_local_symbols_without_profiles(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": "agent-compile",
                "method": "agent.plan.compile",
                "params": {
                    "request": agent_request_dict(),
                    "plan": circuit_plan_dict(),
                },
            }
        )
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertEqual(result["assurance"], "provisional")
        self.assertEqual(result["design"]["metadata"]["generator"], "agent_plan_v1")
        self.assertEqual(
            result["component_qualification"]["summary"]["pad_mapping_failures"],
            0,
        )
        self.assertNotIn("attempt_allowed", result["plan_review"])
        self.assertNotIn("release_allowed", result["plan_review"])
        self.assertIn(
            "power.input_pin_coverage",
            {finding["id"] for finding in result["plan_review"]["findings"]},
        )
        symbols = {item["symbol"] for item in result["part_catalog"]["parts"]}
        self.assertIn("MCU_ST_STM32F4:STM32F405RGTx", symbols)

    def test_generic_agent_project_keeps_circuit_plan_in_managed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": "agent-generate",
                    "method": "agent.project.generate",
                    "params": {
                        "request": indicator_request_dict(),
                        "plan": indicator_plan_dict(),
                        "output": str(Path(temporary) / "project"),
                    },
                }
            )
            self.assertNotIn("error", response)
            result = response["result"]
            self.assertIn("circuit_plan", result["files"])
            self.assertIn("component_qualification", result["files"])
            self.assertNotIn("attempt_allowed", result["plan_review"])
            self.assertNotIn("release_allowed", result["plan_review"])

    def test_protocol_rejects_unknown_methods_and_parameters(self) -> None:
        unknown = handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "mouse.click", "params": {}}
        )
        self.assertEqual(unknown["error"]["code"], -32601)
        extra = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "runtime.capabilities",
                "params": {"ignored": True},
            }
        )
        self.assertEqual(extra["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
