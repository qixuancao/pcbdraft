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

from pcb_agent.application import ApplicationService
from pcb_agent.errors import ValidationError
from pcb_agent.jobs import JOB_SCHEMA, JOB_VERSION, JobRunner
from pcb_agent.providers import (
    BuiltinIntentProvider,
    OpenAICompatibleIntentProvider,
    OpenAICompatibleSettings,
    ProviderContext,
    interpretation_schema,
    validate_interpretation,
)
from pcb_agent.webapp import create_app_server


class ApplicationConversationTests(unittest.TestCase):
    def test_supported_request_stops_at_reviewable_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            view = service.create_project(
                "Greenhouse sensor",
                "Create a TMP102 I2C temperature sensor board",
            )
            project_id = view["project"]["id"]
            self.assertEqual(view["project"]["status"], "needs_clarification")
            self.assertFalse((service.project_root(project_id) / "design").exists())
            self.assertEqual(
                view["conversation"]["proposal"]["clarifications"][0]["id"],
                "layers",
            )

            view = service.send_message(project_id, "2 layers")
            proposal = view["conversation"]["proposal"]
            self.assertEqual(view["project"]["status"], "awaiting_confirmation")
            self.assertEqual(proposal["scope"]["decision"], "supported")
            self.assertTrue(proposal["brief"]["confirmation_required"])
            self.assertEqual(len(proposal["brief"]["bom"]), 9)
            self.assertGreaterEqual(len(proposal["brief"]["constraints"]), 10)
            self.assertFalse((service.project_root(project_id) / "design").exists())

            reopened = ApplicationService(temporary, provider_name="builtin")
            self.assertEqual(
                reopened.open_project(project_id)["conversation"]["proposal"],
                proposal,
            )
            self.assertGreaterEqual(len(reopened.events(project_id)), 4)

    def test_unsupported_high_risk_request_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            view = service.create_project(
                "Mains controller",
                "Build a 230V mains medical RF controller",
            )
            self.assertEqual(view["project"]["status"], "unsupported")
            self.assertEqual(
                view["conversation"]["proposal"]["scope"]["decision"],
                "unsupported",
            )
            self.assertIsNone(view["design"])

    def test_unverified_board_envelope_is_rejected_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="builtin")
            view = service.create_project(
                "Oversized sensor",
                "Create a 2-layer 60 x 40 mm BME280 SPI environmental board",
            )
            self.assertEqual(view["project"]["status"], "unsupported")
            self.assertEqual(
                view["conversation"]["proposal"]["scope"]["decision"],
                "unsupported",
            )
            self.assertIn(
                "45 mm × 30 mm",
                view["conversation"]["proposal"]["scope"]["reasons"][0],
            )
            self.assertIsNone(view["design"])

    def test_secrets_are_redacted_before_provider_and_storage(self) -> None:
        sentinel = "test-provider-secret-value-123456789"
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("COPPERWRIGHT_TEST_API_KEY")
            os.environ["COPPERWRIGHT_TEST_API_KEY"] = sentinel
            try:
                service = ApplicationService(temporary, provider_name="builtin")
                view = service.create_project(
                    "Secret check",
                    f"Create a 2-layer TMP102 I2C sensor; api_key={sentinel}",
                )
            finally:
                if previous is None:
                    os.environ.pop("COPPERWRIGHT_TEST_API_KEY", None)
                else:
                    os.environ["COPPERWRIGHT_TEST_API_KEY"] = previous
            self.assertEqual(view["project"]["status"], "awaiting_confirmation")
            combined = b""
            for path in Path(temporary).rglob("*"):
                if path.is_file():
                    combined += path.read_bytes()
            self.assertNotIn(sentinel.encode(), combined)
            self.assertIn(b"[REDACTED]", combined)

    def test_untrusted_provider_shape_is_rejected(self) -> None:
        valid = BuiltinIntentProvider().interpret(
            context=type(
                "Context",
                (),
                {
                    "request": "2-layer TMP102 I2C sensor",
                    "project_name": "Sensor",
                    "prior_decisions": {},
                },
            )(),
            project_dir=Path.cwd(),
            run_dir=Path.cwd(),
            timeout=1,
        )
        self.assertEqual(valid["layers"], 2)
        invalid = dict(valid)
        invalid["side_effect"] = "write KiCad"
        with self.assertRaisesRegex(ValidationError, "intent schema"):
            validate_interpretation(invalid)

    def test_codex_strict_schema_uses_supported_keywords(self) -> None:
        schema = interpretation_schema()
        serialized = json.dumps(schema, sort_keys=True)
        self.assertNotIn("uniqueItems", serialized)
        self.assertFalse(
            any(
                key not in {"type", "enum"}
                for key in schema["properties"]["missing_fields"]["items"]
            )
        )

    def test_builtin_provider_selects_all_profiles_and_rejects_unverified_usb(
        self,
    ) -> None:
        provider = BuiltinIntentProvider()
        cases = {
            "Build a 2-layer TMP102 I2C temperature board": "low_voltage_i2c_controller_v1",
            "Build a 2-layer BME280 SPI environmental board": "low_voltage_spi_environment_v1",
            "Build a 2-layer UART controller with 5V input and an AP2112 LDO": "low_voltage_uart_ldo_controller_v1",
            "Build a USB-C sensor board": "unsupported",
        }
        with tempfile.TemporaryDirectory() as temporary:
            for request, expected in cases.items():
                result = provider.interpret(
                    ProviderContext(request, "Profile selection", {}),
                    project_dir=Path(temporary),
                    run_dir=Path(temporary),
                    timeout=1,
                )
                self.assertEqual(result["proposed_profile"], expected, request)
                if expected == "unsupported":
                    self.assertTrue(result["unsupported_reasons"])

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
                    "proposed_profile": "low_voltage_uart_ldo_controller_v1",
                    "design_name": "UART controller",
                    "layers": 2,
                    "board": {"width_mm": None, "height_mm": None},
                    "assumptions": ["Externally regulated 5 V input"],
                    "missing_fields": [],
                    "unsupported_reasons": [],
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
        key_name = "COPPERWRIGHT_TEST_PROVIDER_KEY"
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
                self.assertEqual(
                    result["proposed_profile"],
                    "low_voltage_uart_ldo_controller_v1",
                )
                self.assertFalse(any(Path(temporary).rglob("*")))
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

    def test_queued_job_can_be_cancelled_and_retried_without_side_effects(
        self,
    ) -> None:
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
                    {"text": "Create a 2-layer TMP102 I2C sensor"},
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
                    "awaiting_confirmation",
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
            headers["X-CopperWright-CSRF"] = csrf
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
        self.assertEqual(bootstrap["schema"], "copperwright-browser-bootstrap")
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
            if job["status"] not in {"queued", "running", "cancel_requested"}:
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(view["project"]["status"], "needs_clarification")

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
        web = files("pcb_agent").joinpath("web")
        html = web.joinpath("index.html").read_text(encoding="utf-8")
        script = web.joinpath("app.js").read_text(encoding="utf-8")
        self.assertIn('id="setup-dialog"', html)
        self.assertIn("OPENAI_API_KEY=&lt;secret&gt;", html)
        self.assertNotIn('id="provider-api-key"', html)
        self.assertIn("Actionable findings & honest external gates", script)
        self.assertIn("validation_report", script)
        self.assertIn('node("strong", "", validation.candidate_ready', script)


if __name__ == "__main__":
    unittest.main()
