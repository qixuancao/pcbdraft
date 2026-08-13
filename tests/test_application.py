from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pcb_agent.application import ApplicationService
from pcb_agent.errors import ValidationError
from pcb_agent.providers import (
    BuiltinIntentProvider,
    ProviderContext,
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


if __name__ == "__main__":
    unittest.main()
