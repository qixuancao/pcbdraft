from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.agent.design import CircuitPlan
from pcbdraft.agent.repair import (
    generation_feedback,
    validation_feedback_from_levels,
)
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.project import sha256_file
from pcbdraft.interfaces.web import create_app_server
from pcbdraft.model.providers import (
    BuiltinIntentProvider,
    OpenAICompatibleIntentProvider,
    OpenAICompatibleSettings,
    ProviderContext,
    interpretation_schema,
    validate_interpretation,
)
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.jobs import JOB_SCHEMA, JOB_VERSION, JobRunner
from tests.agent.test_design import circuit_plan_dict, indicator_plan_dict


class GenericPlanningProvider:
    """Deterministic test double for the model-backed generic planner."""

    provider_id = "generic-test-planner"
    supports_planning = True

    def __init__(self) -> None:
        self.repair_feedbacks: list[dict[str, object]] = []

    def diagnostic(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "available": True,
            "planning": "test double",
            "secret_storage": "none",
        }

    def interpret(
        self,
        context: ProviderContext,
        *,
        project_dir: Path,
        run_dir: Path,
        timeout: float,
    ) -> dict[str, object]:
        del project_dir, run_dir, timeout
        request = context.request
        lowered = request.casefold()
        parts = [part for part in ("STM32F405", "SHT31") if part.casefold() in lowered]
        functions = (
            ["sensor acquisition"]
            if parts
            else ["embedded control"]
            if "controller" in lowered
            else []
        )
        layer_match = __import__("re").search(
            r"(?:\b(\d+)\s*[- ]?layers?\b|(\d+)\s*层)", lowered
        )
        layers = (
            int(layer_match.group(1) or layer_match.group(2)) if layer_match else None
        )
        return validate_interpretation(
            {
                "request_summary": request,
                "design_name": context.project_name,
                "layers": layers,
                "board": {"width_mm": 80.0, "height_mm": 50.0},
                "assumptions": ["A regulated 3.3 V supply is available."],
                "requested_parts": parts,
                "functions": functions,
                "power": {
                    "nominal_v": 3.3,
                    "max_voltage_v": 3.3,
                    "max_current_a": 0.5,
                    "max_power_w": 1.65,
                },
                "missing_fields": [],
            }
        )

    def plan(
        self,
        request: object,
        *,
        symbol_context: dict[str, list[dict[str, object]]],
        project_dir: Path,
        run_dir: Path,
        timeout: float,
    ) -> CircuitPlan:
        del project_dir, run_dir, timeout
        self.last_symbol_context = symbol_context
        plan = circuit_plan_dict()
        plan["design_id"] = request.design_id  # type: ignore[attr-defined]
        return CircuitPlan.from_dict(plan)

    def revise_plan(
        self,
        request: object,
        previous_plan: CircuitPlan,
        feedback: dict[str, object],
        *,
        symbol_context: dict[str, list[dict[str, object]]],
        project_dir: Path,
        run_dir: Path,
        timeout: float,
    ) -> CircuitPlan:
        del previous_plan, symbol_context, project_dir, run_dir, timeout
        self.repair_feedbacks.append(feedback)
        plan = circuit_plan_dict()
        plan["design_id"] = request.design_id  # type: ignore[attr-defined]
        plan["notes"] = [
            "Pin, power, and layout review remain required.",
            f"Revised from retained tool evidence on attempt {feedback['attempt']}.",
        ]
        return CircuitPlan.from_dict(plan)


class ApplicationConversationTests(unittest.TestCase):
    def test_completed_l1_topology_failure_is_bounded_repair_feedback(self) -> None:
        feedback = validation_feedback_from_levels(
            [
                {
                    "level": "L1",
                    "checks": [
                        {
                            "id": "l1.agent_plan_electrical_preflight",
                            "state": "completed",
                            "outcome": "fail",
                            "summary": "LED polarity is reversed",
                        },
                        {
                            "id": "l1.component_evidence_qualification",
                            "state": "human_required",
                            "outcome": "unknown",
                            "summary": "datasheet review remains required",
                        },
                    ],
                }
            ],
            attempt=1,
        )
        self.assertIsNotNone(feedback)
        self.assertEqual(len(feedback["findings"]), 1)
        self.assertIn("LED polarity", feedback["findings"][0])

    def test_generic_request_uses_default_stackup_without_asking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = GenericPlanningProvider()
            service = ApplicationService(temporary, provider=provider)
            view = service.create_project(
                "Generic sensor",
                "Create an STM32F405 board with an SHT31 sensor",
            )
            project_id = view["project"]["id"]
            proposal = view["conversation"]["proposal"]
            self.assertEqual(view["project"]["status"], "awaiting_confirmation")
            self.assertFalse((service.project_root(project_id) / "design").exists())
            self.assertEqual(proposal["clarifications"], [])
            self.assertEqual(proposal["decisions"]["layers"], 2)
            self.assertEqual(proposal["scope"]["decision"], "attempted")
            self.assertEqual(proposal["planning"]["state"], "ready")
            self.assertEqual(
                proposal["brief"]["identity"]["requested_parts"],
                ["SHT31", "STM32F405"],
            )
            symbols = {
                entry["symbol"]
                for entry in proposal["brief"]["identity"]["planned_symbols"]
            }
            self.assertIn("MCU_ST_STM32F4:STM32F405RGTx", symbols)
            self.assertIn("Sensor_Humidity:SHT31-DIS", symbols)
            self.assertIn("_runtime_primitives", provider.last_symbol_context)
            preflight = proposal["brief"]["plan_review"]
            self.assertNotIn("attempt_allowed", preflight)
            self.assertGreater(preflight["summary"]["attention_required"], 0)
            self.assertNotIn("release_allowed", preflight)
            root = service.project_root(project_id)
            self.assertTrue((root / "pending-agent-request.json").is_file())
            self.assertTrue((root / "pending-circuit-plan.json").is_file())
            self.assertTrue((root / "pending-parts.pcbdraft.json").is_file())

            reopened = ApplicationService(temporary, provider=provider)
            self.assertEqual(
                reopened.open_project(project_id)["conversation"]["proposal"], proposal
            )

    def test_explicit_six_layer_request_is_preserved_without_a_question(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider=GenericPlanningProvider())
            view = service.create_project(
                "Six layer sensor",
                "Create a 6-layer STM32F405 board with an SHT31 sensor",
            )

        proposal = view["conversation"]["proposal"]
        self.assertEqual(view["project"]["status"], "awaiting_confirmation")
        self.assertEqual(proposal["clarifications"], [])
        self.assertEqual(proposal["decisions"]["layers"], 6)
        self.assertEqual(proposal["scope"]["decision"], "attempted")

    def test_explicit_65_layer_request_is_preserved_until_kicad_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider=GenericPlanningProvider())
            view = service.create_project(
                "65 layer sensor",
                "Create a 65-layer STM32F405 board with an SHT31 sensor",
            )

        proposal = view["conversation"]["proposal"]
        self.assertEqual(view["project"]["status"], "awaiting_confirmation")
        self.assertEqual(proposal["clarifications"], [])
        self.assertEqual(proposal["decisions"]["layers"], 65)
        self.assertEqual(proposal["scope"]["decision"], "attempted")

    def test_builtin_preserves_unknown_named_parts_and_requests_a_planner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            view = service.create_project(
                "Generic request",
                "Create a 2-layer STM32F405 board with an SHT31 sensor",
            )
            proposal = view["conversation"]["proposal"]
            self.assertEqual(view["project"]["status"], "planning_required")
            self.assertEqual(proposal["scope"]["decision"], "attempted")
            self.assertEqual(proposal["requested_parts"], ["SHT31", "STM32F405"])
            self.assertIsNone(proposal["brief"])
            self.assertNotIn("unsupported", proposal["planning"]["message"].casefold())

    def test_complex_domain_request_reaches_normal_planning_and_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider=GenericPlanningProvider())
            view = service.create_project(
                "Mains controller",
                "Build a 2-layer 230V mains medical RF controller",
            )
            self.assertEqual(view["project"]["status"], "awaiting_confirmation")
            self.assertEqual(
                view["conversation"]["proposal"]["scope"]["decision"],
                "attempted",
            )
            warnings = " ".join(view["conversation"]["proposal"]["scope"]["warnings"])
            for domain in ("mains", "medical", "rf"):
                self.assertIn(domain, warnings)
            self.assertEqual(
                view["conversation"]["proposal"]["planning"]["state"], "ready"
            )

    def test_builtin_chinese_complex_request_is_retained_for_a_planner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            view = service.create_project(
                "高风险控制器",
                "做一块 2 层高压市电医疗控制板，带射频天线",
            )
            self.assertEqual(view["project"]["status"], "planning_required")
            proposal = view["conversation"]["proposal"]
            self.assertEqual(proposal["scope"]["decision"], "attempted")
            warnings = " ".join(proposal["scope"]["warnings"])
            for domain in ("high_voltage", "mains", "medical", "rf"):
                self.assertIn(domain, warnings)
            self.assertNotIn("outside the automated", proposal["planning"]["message"])

    def test_failed_generic_generation_retains_request_plan_and_part_graph(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider=GenericPlanningProvider())
            view = service.create_project(
                "Retained generic attempt",
                "Create a 2-layer STM32F405 board with an SHT31 sensor",
            )
            project_id = view["project"]["id"]
            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=ValidationError("router left I2C_SDA unrouted"),
                ),
                self.assertRaisesRegex(ValidationError, "I2C_SDA"),
            ):
                service.confirm_project(project_id, validate=False)
            failed = service.open_project(project_id)
            self.assertEqual(failed["project"]["status"], "generation_failed")
            attempt = failed["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertEqual(attempt["assurance"], "provisional")
            self.assertIn("I2C_SDA", attempt["error"])
            attempt_root = Path(attempt["root"])
            self.assertTrue((attempt_root / "request.json").is_file())
            self.assertTrue((attempt_root / "circuit-plan.json").is_file())
            self.assertTrue((attempt_root / "design.pcbir.json").is_file())
            self.assertTrue((attempt_root / "parts.pcbdraft.json").is_file())

    def test_failed_generation_can_compile_a_bounded_replacement_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = GenericPlanningProvider()
            service = ApplicationService(temporary, provider=provider)
            view = service.create_project(
                "Repairable generic attempt",
                "Create a 2-layer STM32F405 board with an SHT31 sensor",
            )
            project_id = view["project"]["id"]
            failure = ValidationError("router left I2C_SDA unrouted")
            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=failure,
                ),
                self.assertRaisesRegex(ValidationError, "I2C_SDA"),
            ):
                service.confirm_project(project_id, validate=False)
            failed = service.open_project(project_id)

            repaired = service.prepare_agent_repair(
                project_id,
                generation_feedback(failed, failure, attempt=1),
                timeout=10,
            )

            self.assertEqual(repaired["project"]["status"], "awaiting_confirmation")
            self.assertEqual(len(provider.repair_feedbacks), 1)
            self.assertEqual(provider.repair_feedbacks[0]["phase"], "generation")
            pending = json.loads(
                (
                    service.project_root(project_id) / "pending-circuit-plan.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("retained tool evidence", " ".join(pending["notes"]))
            self.assertEqual(
                service.events(project_id)[-1]["kind"], "repair.plan_ready"
            )

    def test_unchanged_repair_plan_is_rejected_without_regeneration(self) -> None:
        class NoOpRepairProvider(GenericPlanningProvider):
            def revise_plan(
                self,
                request: object,
                previous_plan: CircuitPlan,
                *args: object,
                **kwargs: object,
            ) -> CircuitPlan:
                del request, args, kwargs
                return previous_plan

        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider=NoOpRepairProvider())
            view = service.create_project(
                "No-op repair",
                "Create a 2-layer STM32F405 board with an SHT31 sensor",
            )
            project_id = view["project"]["id"]
            failure = ValidationError("router left I2C_SDA unrouted")
            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=failure,
                ),
                self.assertRaises(ValidationError),
            ):
                service.confirm_project(project_id, validate=False)
            failed = service.open_project(project_id)

            with self.assertRaisesRegex(ValidationError, "unchanged"):
                service.prepare_agent_repair(
                    project_id,
                    generation_feedback(failed, failure, attempt=1),
                    timeout=10,
                )
            self.assertEqual(
                service.open_project(project_id)["project"]["status"],
                "repair_failed",
            )

    def test_generated_repair_is_staged_applied_and_undoable(self) -> None:
        class IndicatorRepairProvider(GenericPlanningProvider):
            def plan(self, request: object, **kwargs: object) -> CircuitPlan:
                del kwargs
                value = indicator_plan_dict()
                value["design_id"] = request.design_id  # type: ignore[attr-defined]
                return CircuitPlan.from_dict(value)

            def revise_plan(
                self,
                request: object,
                previous_plan: CircuitPlan,
                feedback: dict[str, object],
                **kwargs: object,
            ) -> CircuitPlan:
                del previous_plan, kwargs
                value = indicator_plan_dict()
                value["design_id"] = request.design_id  # type: ignore[attr-defined]
                value["notes"] = [
                    "LED current requires review.",
                    f"Repair attempt {feedback['attempt']} used retained evidence.",
                ]
                return CircuitPlan.from_dict(value)

        failed_levels = [
            {
                "level": "L2",
                "name": "deterministic",
                "state": "completed",
                "outcome": "fail",
                "checks": [
                    {
                        "id": "kicad.erc",
                        "level": "L2",
                        "state": "completed",
                        "outcome": "fail",
                        "summary": "injected repair trigger",
                        "evidence": [],
                        "blocks_candidate": True,
                        "blocks_production": True,
                    }
                ],
            }
        ]
        passed_levels = [
            {
                "level": "L2",
                "name": "deterministic",
                "state": "completed",
                "outcome": "pass",
                "checks": [
                    {
                        "id": "kicad.erc",
                        "level": "L2",
                        "state": "completed",
                        "outcome": "pass",
                        "summary": "clean candidate",
                        "evidence": [],
                        "blocks_candidate": False,
                        "blocks_production": False,
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider=IndicatorRepairProvider())
            view = service.create_project(
                "Atomic repair", "Create a small LED indicator board"
            )
            project_id = view["project"]["id"]
            with patch.object(
                service,
                "generate_project_previews",
                side_effect=lambda value, **kwargs: service.open_project(value),
            ):
                generated = service.confirm_project(
                    project_id, validate=False, timeout=90
                )
            before_hash = generated["design"]["content_hash"]
            feedback = validation_feedback_from_levels(failed_levels, attempt=1)
            assert feedback is not None

            def fake_validation(
                project: object, *, output: Path, timeout: float, **kwargs: object
            ) -> SimpleNamespace:
                del project, timeout, kwargs
                output.mkdir(parents=True)
                report = output / "validation.json"
                report.write_text(
                    json.dumps({"levels": passed_levels}, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    report_path=report,
                    report_sha256=sha256_file(report),
                    candidate_ready=True,
                    production_ready=False,
                )

            with patch(
                "pcbdraft.services.application.validate_managed_project",
                side_effect=fake_validation,
            ):
                staged = service.prepare_agent_repair(project_id, feedback, timeout=90)
            self.assertEqual(staged["project"]["status"], "change_ready")
            self.assertEqual(
                service.open_project(project_id)["design"]["content_hash"],
                before_hash,
            )
            with patch.object(
                service,
                "generate_project_previews",
                side_effect=lambda value, **kwargs: service.open_project(value),
            ):
                applied = service.apply_modification(project_id)
            self.assertEqual(applied["project"]["status"], "validated")
            self.assertNotEqual(applied["design"]["content_hash"], before_hash)
            undone = service.undo_last_modification(project_id)
            self.assertEqual(undone["design"]["content_hash"], before_hash)

    def test_secrets_are_redacted_before_provider_and_storage(self) -> None:
        sentinel = "test-provider-secret-value-123456789"
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("PCBDRAFT_TEST_API_KEY")
            os.environ["PCBDRAFT_TEST_API_KEY"] = sentinel
            try:
                service = ApplicationService(temporary, provider_name="builtin")
                view = service.create_project(
                    "Secret check",
                    f"Create a 2-layer SHT31 sensor; api_key={sentinel}",
                )
            finally:
                if previous is None:
                    os.environ.pop("PCBDRAFT_TEST_API_KEY", None)
                else:
                    os.environ["PCBDRAFT_TEST_API_KEY"] = previous
            self.assertEqual(view["project"]["status"], "planning_required")
            combined = b""
            for item in Path(temporary).rglob("*"):
                if item.is_file():
                    combined += item.read_bytes()
            self.assertNotIn(sentinel.encode(), combined)
            self.assertIn(b"[REDACTED]", combined)

    def test_untrusted_provider_shape_is_rejected(self) -> None:
        valid = BuiltinIntentProvider().interpret(
            context=ProviderContext("2-layer SHT31 sensor", "Sensor", {}),
            project_dir=Path.cwd(),
            run_dir=Path.cwd(),
            timeout=1,
        )
        self.assertEqual(valid["layers"], 2)
        invalid = dict(valid)
        invalid["side_effect"] = "write KiCad"
        with self.assertRaisesRegex(ValidationError, "intent schema"):
            validate_interpretation(invalid)

    def test_intent_schema_uses_supported_keywords(self) -> None:
        schema = interpretation_schema()
        serialized = json.dumps(schema, sort_keys=True)
        self.assertNotIn("uniqueItems", serialized)
        self.assertEqual(
            schema["properties"]["layers"]["anyOf"][0],
            {"type": "integer", "minimum": 1},
        )
        self.assertFalse(
            any(
                key not in {"type", "enum"}
                for key in schema["properties"]["missing_fields"]["items"]
            )
        )

    def test_builtin_provider_does_not_claim_a_fixed_board_type(self) -> None:
        provider = BuiltinIntentProvider()
        with tempfile.TemporaryDirectory() as temporary:
            result = provider.interpret(
                ProviderContext(
                    "Build a 2-layer UART controller with 5V input and an AP2112 LDO",
                    "Generic intent",
                    {},
                ),
                project_dir=Path(temporary),
                run_dir=Path(temporary),
                timeout=1,
            )
        self.assertEqual(result["layers"], 2)
        self.assertIn("UART serial interface", result["functions"])
        self.assertNotIn("proposed_profile", result)

    def test_builtin_provider_preserves_any_explicit_layer_count(self) -> None:
        provider = BuiltinIntentProvider()
        with tempfile.TemporaryDirectory() as temporary:
            result = provider.interpret(
                ProviderContext(
                    "Build a 6-layer UART controller with 5V input",
                    "Six layer controller",
                    {},
                ),
                project_dir=Path(temporary),
                run_dir=Path(temporary),
                timeout=1,
            )
        self.assertEqual(result["layers"], 6)
        self.assertNotIn("layers", result["missing_fields"])

    def test_builtin_provider_keeps_a_chinese_generic_request_generic(self) -> None:
        provider = BuiltinIntentProvider()
        with tempfile.TemporaryDirectory() as temporary:
            result = provider.interpret(
                ProviderContext(
                    "做一块 2 层控制板，使用 STM32F405 和 SHT31 传感器，"
                    "提供 I2C，3.3伏，最大 200毫安",
                    "中文通用请求",
                    {},
                ),
                project_dir=Path(temporary),
                run_dir=Path(temporary),
                timeout=1,
            )
        self.assertEqual(result["layers"], 2)
        self.assertEqual(result["requested_parts"], ["SHT31", "STM32F405"])
        self.assertIn("sensor acquisition", result["functions"])
        self.assertIn("I2C bus", result["functions"])
        self.assertEqual(result["power"]["max_current_a"], 0.2)

    def test_openai_compatible_provider_keeps_secret_runtime_only(self) -> None:
        captured: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                captured["path"] = self.path
                captured["authorization"] = self.headers.get("Authorization")
                captured["request"] = json.loads(self.rfile.read(length))
                intent = {
                    "request_summary": "Create a 2-layer UART controller",
                    "design_name": "UART controller",
                    "layers": 2,
                    "board": {"width_mm": None, "height_mm": None},
                    "assumptions": ["Externally regulated 5 V input"],
                    "requested_parts": ["AP2112"],
                    "functions": ["UART serial interface"],
                    "power": {
                        "nominal_v": 5.0,
                        "max_voltage_v": 5.0,
                        "max_current_a": 0.1,
                        "max_power_w": 0.5,
                    },
                    "missing_fields": [],
                }
                response = json.dumps(
                    {"choices": [{"message": {"content": json.dumps(intent)}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        key_name = "PCBDRAFT_TEST_PROVIDER_KEY"
        sentinel = "test-only-provider-secret-123456789"
        previous = os.environ.get(key_name)
        os.environ[key_name] = sentinel
        try:
            provider = OpenAICompatibleIntentProvider(
                OpenAICompatibleSettings(
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="local-test-model",
                    api_key_env=key_name,
                )
            )
            with tempfile.TemporaryDirectory() as temporary:
                result = provider.interpret(
                    ProviderContext(
                        "Create a 2-layer UART controller", "UART controller", {}
                    ),
                    project_dir=Path(temporary),
                    run_dir=Path(temporary) / "provider-run",
                    timeout=5,
                )
                self.assertEqual(result["requested_parts"], ["AP2112"])
                retained = tuple(Path(temporary).rglob("*"))
                self.assertTrue(retained)
                self.assertFalse(
                    any(
                        item.is_file() and sentinel in item.read_text(encoding="utf-8")
                        for item in retained
                    )
                )
            diagnostic = provider.diagnostic()
        finally:
            if previous is None:
                os.environ.pop(key_name, None)
            else:
                os.environ[key_name] = previous
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(captured["path"], "/v1/chat/completions")
        self.assertEqual(captured["authorization"], f"Bearer {sentinel}")
        request = captured["request"]
        self.assertIsInstance(request, dict)
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertNotIn(sentinel, json.dumps(diagnostic, sort_keys=True))
        self.assertTrue(diagnostic["secret_present"])

    def test_restart_recovers_transient_project_and_job_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            view = service.create_draft("Restart recovery")
            project_id = view["project"]["id"]
            root = service.project_root(project_id)
            state_path = root / "project.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "interpreting"
            state_path.write_text(
                json.dumps(state, sort_keys=True) + "\n", encoding="utf-8"
            )
            job_id = "20260813T000000Z-acde1234"
            job_path = root / "jobs" / f"{job_id}.json"
            job_path.write_text(
                json.dumps(
                    {
                        "schema": JOB_SCHEMA,
                        "version": JOB_VERSION,
                        "id": job_id,
                        "project_id": project_id,
                        "action": "message",
                        "args": {"text": "2 layers", "timeout": 180.0},
                        "status": "running",
                        "attempt": 1,
                        "retry_of": None,
                        "created_at": "2026-08-13T00:00:00Z",
                        "started_at": "2026-08-13T00:00:01Z",
                        "completed_at": None,
                        "cancel_requested_at": None,
                        "result": None,
                        "error": None,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = ApplicationService(temporary, provider_name="builtin")
            self.assertEqual(
                restarted.open_project(project_id)["project"]["status"],
                "interrupted",
            )
            runner = JobRunner(restarted, workers=1)
            try:
                recovered = runner.get(project_id, job_id)
                self.assertEqual(recovered["status"], "interrupted")
                self.assertIn("stopped", recovered["error"])
            finally:
                runner.shutdown()
            events = restarted.events(project_id)
            self.assertEqual(events[-1]["kind"], "operation.interrupted")

    def test_queued_job_can_be_cancelled_and_retried_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            blocker_id = service.create_draft("Worker blocker")["project"]["id"]
            target_id = service.create_draft("Retry target")["project"]["id"]
            runner = JobRunner(service, workers=1)
            started = threading.Event()
            release = threading.Event()
            original_dispatch = runner._dispatch

            def dispatch(job: dict[str, object]) -> dict[str, object]:
                if job["project_id"] == blocker_id:
                    started.set()
                    self.assertTrue(release.wait(timeout=5))
                    return service.open_project(blocker_id)
                return original_dispatch(job)

            runner._dispatch = dispatch  # type: ignore[method-assign]
            try:
                runner.submit(blocker_id, "message", {"text": "2 layers"})
                self.assertTrue(started.wait(timeout=5))
                queued = runner.submit(
                    target_id,
                    "message",
                    {"text": "Create a 2-layer SHT31 sensor"},
                )
                cancelled = runner.cancel(target_id, queued["id"])
                self.assertEqual(cancelled["status"], "cancel_requested")
                self.assertEqual(
                    service.open_project(target_id)["conversation"]["messages"], []
                )
                release.set()

                for _ in range(250):
                    cancelled = runner.get(target_id, queued["id"])
                    if cancelled["status"] == "cancelled":
                        break
                    time.sleep(0.02)
                self.assertEqual(cancelled["status"], "cancelled")
                retried = runner.retry(target_id, queued["id"])
                self.assertEqual(retried["retry_of"], queued["id"])
                self.assertEqual(retried["attempt"], 2)
                for _ in range(250):
                    retried = runner.get(target_id, retried["id"])
                    if retried["status"] not in {
                        "queued",
                        "running",
                        "cancel_requested",
                    }:
                        break
                    time.sleep(0.02)
                self.assertEqual(retried["status"], "completed")
                self.assertEqual(
                    service.open_project(target_id)["project"]["status"],
                    "planning_required",
                )
            finally:
                release.set()
                runner.shutdown()


class BrowserSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_app_server(
            host="127.0.0.1",
            port=0,
            workspace=self.temporary.name,
            provider="builtin",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = self.server.base_url

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        body: dict[str, object] | None = None,
        origin: str | None = None,
        csrf: str | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        if csrf is not None:
            headers["X-PCBDraft-CSRF"] = csrf
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return (
                    response.status,
                    json.loads(response.read()),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read()), dict(exc.headers)
            finally:
                exc.close()

    def test_loopback_bootstrap_security_headers_and_csrf(self) -> None:
        status, bootstrap, headers = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["schema"], "pcbdraft-browser-bootstrap")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        csrf = str(bootstrap["csrf_token"])

        status, error, _ = self.request(
            "/api/projects",
            body={"name": "Sensor", "request": "TMP102 I2C sensor"},
            origin="https://evil.example",
            csrf=csrf,
        )
        self.assertEqual(status, 403)
        self.assertIn("same-origin", error["error"]["message"])

        status, created, _ = self.request(
            "/api/projects",
            body={"name": "Sensor", "request": "TMP102 I2C sensor"},
            origin=self.base,
            csrf=csrf,
        )
        self.assertEqual(status, 202)
        project_id = created["project"]["project"]["id"]
        job_id = created["job"]["id"]
        for _ in range(100):
            status, view, _ = self.request(f"/api/projects/{project_id}")
            self.assertEqual(status, 200)
            job = next(item for item in view["jobs"] if item["id"] == job_id)
            if (
                job["status"] not in {"queued", "running", "cancel_requested"}
                and view["project"]["status"] != "draft"
            ):
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(view["project"]["status"], "planning_required")

    def test_nonloopback_bind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "loopback"):
            create_app_server(
                host="0.0.0.0",
                port=0,
                workspace=self.temporary.name,
                provider="builtin",
            )

    def test_static_browser_shell_has_safe_setup_and_actionable_validation(
        self,
    ) -> None:
        web = files("pcbdraft").joinpath("web")
        html = web.joinpath("index.html").read_text(encoding="utf-8")
        script = web.joinpath("app.js").read_text(encoding="utf-8")
        self.assertIn('id="setup-dialog"', html)
        self.assertIn("/connect", html)
        self.assertNotIn("OPENAI_API_KEY", html)
        self.assertNotIn('id="provider-api-key"', html)
        self.assertIn("Findings and unavailable checks", script)
        self.assertIn("validation_report", script)
        self.assertIn('node("strong", "", validation.candidate_ready', script)


if __name__ == "__main__":
    unittest.main()
