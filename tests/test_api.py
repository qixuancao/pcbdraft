from __future__ import annotations

import unittest

from copperwright.api import handle_request
from tests.requirements_factory import controller_requirements_dict


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
        self.assertIn("project.generate", result["methods"])
        self.assertIn("sync.preview", result["methods"])
        self.assertIn("benchmark.run", result["methods"])
        self.assertIn("release.verify", result["methods"])
        self.assertEqual(
            result["accepted_scope"]["high_risk_domains"], "explicitly_rejected"
        )
        self.assertEqual(result["accepted_scope"]["layers"], [2, 4])
        self.assertEqual(
            result["generation_profiles"][0]["id"],
            "low_voltage_i2c_controller_v1",
        )
        self.assertIn(
            "spi", result["scope_policy"]["recognized_without_bundled_generator"]
        )

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
        self.assertEqual(response["result"]["design"]["schema"], "copperwright-ir")
        self.assertEqual(len(response["result"]["content_hash"]), 64)

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
