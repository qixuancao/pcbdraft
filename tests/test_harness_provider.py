from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent_design import AgentDesignRequest, CircuitPlan
from pcbdraft.errors import PCBDraftError, ValidationError
from pcbdraft.harness_bridge import HarnessBridgeSettings
from pcbdraft.providers import (
    DeepSeekHarnessIntentProvider,
    ProviderContext,
    resolve_provider,
)
from tests.test_agent_design import agent_request_dict, circuit_plan_dict


def _intent() -> dict[str, object]:
    return {
        "request_summary": "Create a compact STM32F405 and SHT31 sensor board.",
        "design_name": "Harness sensor board",
        "layers": None,
        "board": {"width_mm": 60.0, "height_mm": 40.0},
        "assumptions": ["A regulated 3.3 V supply is available."],
        "requested_parts": ["STM32F405", "SHT31"],
        "functions": ["sensor acquisition"],
        "power": {
            "nominal_v": 3.3,
            "max_voltage_v": 3.3,
            "max_current_a": 0.5,
            "max_power_w": 1.65,
        },
        "missing_fields": [],
    }


def _fake_bridge(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            request = json.load(sys.stdin)
            capture = os.environ.get("PCBDRAFT_TEST_DSH_CAPTURE")
            if capture:
                with open(capture, "w", encoding="utf-8") as stream:
                    json.dump({"argv": sys.argv, "request": request}, stream)
            delay = float(os.environ.get("PCBDRAFT_TEST_DSH_DELAY", "0"))
            if delay:
                time.sleep(delay)
            if os.environ.get("PCBDRAFT_TEST_DSH_MALFORMED"):
                print(json.dumps({"unexpected": True}))
                raise SystemExit(0)
            if os.environ.get("PCBDRAFT_TEST_DSH_FAILURE"):
                print(json.dumps({
                    "schema": "pcbdraft-harness-provider-response",
                    "version": 1,
                    "request_id": request["request_id"],
                    "operation": request["operation"],
                    "ok": False,
                    "result": None,
                    "error": {
                        "code": "runtime_error",
                        "message": "Harness runtime rejected the bounded turn",
                        "retryable": True,
                    },
                    "metadata": {
                        "provider": "fake-dsh",
                        "model": "fake-model",
                        "finish_reason": None,
                        "structured_output": "test-schema",
                        "session_id": "fake-session",
                    },
                }))
                raise SystemExit(0)
            results = json.loads(os.environ["PCBDRAFT_TEST_DSH_RESULTS"])
            response = {
                "schema": "pcbdraft-harness-provider-response",
                "version": 1,
                "request_id": request["request_id"],
                "operation": request["operation"],
                "ok": True,
                "result": results[request["operation"]],
                "error": None,
                "metadata": {
                    "provider": "fake-dsh",
                    "model": "fake-model",
                    "finish_reason": "completed",
                    "structured_output": "test-schema",
                    "session_id": "fake-session",
                },
            }
            print(json.dumps(response))
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)


class DeepSeekHarnessProviderTests(unittest.TestCase):
    def test_all_provider_operations_use_one_versioned_stdin_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "fake-bridge"
            capture = root / "capture.json"
            _fake_bridge(bridge)
            plan_value = circuit_plan_dict()
            revised_value = circuit_plan_dict()
            revised_value["notes"] = ["Revised from deterministic generation evidence."]
            results = {
                "interpret": _intent(),
                "plan": plan_value,
                "revise_plan": revised_value,
            }
            provider = DeepSeekHarnessIntentProvider(
                HarnessBridgeSettings(
                    executable=str(bridge), provider="fake-dsh", model="fake-model"
                )
            )
            request = AgentDesignRequest.from_dict(agent_request_dict())
            previous = CircuitPlan.from_dict(circuit_plan_dict())
            feedback = {
                "schema": "pcbdraft-agent-repair-feedback",
                "version": 1,
                "phase": "generation",
                "attempt": 1,
                "summary": "Generation failed.",
                "findings": ["A deterministic route could not be completed."],
            }
            environment = {
                "PCBDRAFT_TEST_DSH_RESULTS": json.dumps(results),
                "PCBDRAFT_TEST_DSH_CAPTURE": str(capture),
            }
            with patch.dict(os.environ, environment, clear=False):
                intent = provider.interpret(
                    ProviderContext(
                        "Build this private-user-phrase sensor board.",
                        "Harness board",
                        {},
                    ),
                    project_dir=root,
                    run_dir=root / "intent-run",
                    timeout=5,
                )
                plan = provider.plan(
                    request,
                    symbol_context={},
                    project_dir=root,
                    run_dir=root / "plan-run",
                    timeout=5,
                )
                revised = provider.revise_plan(
                    request,
                    previous,
                    feedback,
                    symbol_context={},
                    project_dir=root,
                    run_dir=root / "repair-run",
                    timeout=5,
                )

            self.assertIsNone(intent["layers"])
            self.assertEqual(plan.design_id, request.design_id)
            self.assertIn("Revised", revised.notes[0])
            for name in ("intent", "plan", "repair"):
                receipts = list((root / f"{name}-run").glob("harness-*-receipt.json"))
                self.assertEqual(len(receipts), 1)
                receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
                self.assertEqual(receipt["version"], 1)
                self.assertEqual(receipt["metadata"]["provider"], "fake-dsh")

            captured = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(captured["argv"], [str(bridge)])
            self.assertNotIn("private-user-phrase", json.dumps(captured["argv"]))
            self.assertEqual(
                captured["request"]["schema"],
                "pcbdraft-harness-provider-request",
            )
            self.assertEqual(captured["request"]["operation"], "revise_plan")
            self.assertIn("findings", captured["request"]["prompt"])

    def test_malformed_bridge_response_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "fake-bridge"
            _fake_bridge(bridge)
            provider = DeepSeekHarnessIntentProvider(
                HarnessBridgeSettings(executable=str(bridge))
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PCBDRAFT_TEST_DSH_RESULTS": json.dumps(
                            {"interpret": _intent()}
                        ),
                        "PCBDRAFT_TEST_DSH_MALFORMED": "1",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(ValidationError, "response fields"),
            ):
                provider.interpret(
                    ProviderContext("Build a board", "Board", {}),
                    project_dir=root,
                    run_dir=root / "run",
                    timeout=5,
                )

    def test_bridge_timeout_kills_the_bounded_provider_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "fake-bridge"
            _fake_bridge(bridge)
            provider = DeepSeekHarnessIntentProvider(
                HarnessBridgeSettings(executable=str(bridge))
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PCBDRAFT_TEST_DSH_RESULTS": json.dumps(
                            {"interpret": _intent()}
                        ),
                        "PCBDRAFT_TEST_DSH_DELAY": "2",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(PCBDraftError, "timed out"),
            ):
                provider.interpret(
                    ProviderContext("Build a board", "Board", {}),
                    project_dir=root,
                    run_dir=root / "run",
                    timeout=0.1,
                )

    def test_model_result_still_has_to_pass_pcbdraft_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "fake-bridge"
            _fake_bridge(bridge)
            invalid = _intent()
            invalid["invented_field"] = True
            provider = DeepSeekHarnessIntentProvider(
                HarnessBridgeSettings(executable=str(bridge))
            )
            with (
                patch.dict(
                    os.environ,
                    {"PCBDRAFT_TEST_DSH_RESULTS": json.dumps({"interpret": invalid})},
                    clear=False,
                ),
                self.assertRaisesRegex(ValidationError, "intent schema"),
            ):
                provider.interpret(
                    ProviderContext("Build a board", "Board", {}),
                    project_dir=root,
                    run_dir=root / "run",
                    timeout=5,
                )

    def test_explicit_provider_resolves_a_configured_external_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge = Path(temporary) / "fake-bridge"
            _fake_bridge(bridge)
            with patch.dict(
                os.environ,
                {"PCBDRAFT_DSH_BRIDGE": str(bridge)},
                clear=False,
            ):
                provider = resolve_provider("deepseek-harness")
            self.assertIsInstance(provider, DeepSeekHarnessIntentProvider)
            self.assertTrue(provider.diagnostic()["available"])

    def test_structured_bridge_failure_is_reported_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "fake-bridge"
            _fake_bridge(bridge)
            provider = DeepSeekHarnessIntentProvider(
                HarnessBridgeSettings(executable=str(bridge))
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PCBDRAFT_TEST_DSH_RESULTS": "{}",
                        "PCBDRAFT_TEST_DSH_FAILURE": "1",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(PCBDraftError, "runtime_error"),
            ):
                provider.interpret(
                    ProviderContext("Build a board", "Board", {}),
                    project_dir=root,
                    run_dir=root / "run",
                    timeout=5,
                )


if __name__ == "__main__":
    unittest.main()
