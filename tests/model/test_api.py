from __future__ import annotations

import json
import threading
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.model.api import (
    ModelTransportError,
    OpenAICompatibleSettings,
    StructuredModelClient,
)
from pcbdraft.model.profiles import provider_wire_profile
from pcbdraft.model.retry import parse_retry_after_seconds

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> ThreadingHTTPServer:
        self.thread.start()
        return self.server

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class StructuredModelClientTests(unittest.TestCase):
    def test_known_endpoint_host_selects_profile_for_legacy_provider_id(self) -> None:
        profile = provider_wire_profile(
            "openai-compatible", "https://api.deepseek.com/v1"
        )
        self.assertEqual(profile.output_mode, "json_object")

    def test_provider_profiles_emit_compatible_wire_parameters(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                requests.append(json.loads(self.rfile.read(length)))
                response = json.dumps(
                    {"choices": [{"message": {"content": '{"answer":"ok"}'}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        provider_ids = (
            "deepseek",
            "minimax",
            "kimi",
            "openai",
            "openrouter",
            "ollama",
        )
        with _Server(Handler) as server:
            for provider_id in provider_ids:
                client = StructuredModelClient(
                    OpenAICompatibleSettings(
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "model",
                        api_key="secret",
                        provider_id=provider_id,
                    )
                )
                value, receipt = client.request(
                    prompt="test", schema_name="result", schema=_SCHEMA, timeout=5
                )
                self.assertEqual(value, {"answer": "ok"})
                self.assertEqual(receipt["provider"], provider_id)

        bodies = dict(zip(provider_ids, requests, strict=True))
        self.assertEqual(bodies["deepseek"]["response_format"], {"type": "json_object"})
        self.assertIn("JSON Schema", bodies["deepseek"]["messages"][0]["content"])
        self.assertNotIn("response_format", bodies["minimax"])
        self.assertNotIn("temperature", bodies["minimax"])
        self.assertEqual(bodies["minimax"]["max_completion_tokens"], 2048)
        self.assertTrue(bodies["minimax"]["reasoning_split"])
        for provider_id in ("kimi", "openai"):
            self.assertEqual(
                bodies[provider_id]["response_format"]["type"], "json_schema"
            )
            self.assertIn("max_completion_tokens", bodies[provider_id])
            self.assertNotIn("max_tokens", bodies[provider_id])
            self.assertNotIn("temperature", bodies[provider_id])
        self.assertEqual(bodies["openrouter"]["provider"], {"require_parameters": True})
        self.assertEqual(bodies["ollama"]["temperature"], 0.0)

    def test_transient_provider_failure_retries_within_the_same_deadline(self) -> None:
        request_count = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                nonlocal request_count
                request_count += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                if request_count == 1:
                    response = b'{"error":"temporarily unavailable"}'
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                else:
                    response = b'{"choices":[{"message":{"content":"{\\"answer\\":\\"ok\\"}"}}]}'
                    self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with _Server(Handler) as server:
            client = StructuredModelClient(
                OpenAICompatibleSettings(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "model",
                    api_key="secret",
                )
            )
            client._sleep = lambda _seconds: None
            value, receipt = client.request(
                prompt="test", schema_name="result", schema=_SCHEMA, timeout=5
            )
        self.assertEqual(value, {"answer": "ok"})
        self.assertEqual(receipt["attempts"], 2)
        self.assertEqual(request_count, 2)

    def test_authentication_failure_is_not_retried_or_leaked(self) -> None:
        request_count = 0
        private_error_detail = "provider-body-must-not-leak"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                nonlocal request_count
                request_count += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                response = json.dumps({"error": private_error_detail}).encode()
                self.send_response(401)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with _Server(Handler) as server:
            client = StructuredModelClient(
                OpenAICompatibleSettings(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "model",
                    api_key="secret",
                )
            )
            with self.assertRaises(ModelTransportError) as caught:
                client.request(
                    prompt="test", schema_name="result", schema=_SCHEMA, timeout=5
                )
        self.assertEqual(caught.exception.category, "authentication")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertNotIn(private_error_detail, str(caught.exception))
        self.assertEqual(request_count, 1)

    def test_retry_after_accepts_seconds_and_http_dates(self) -> None:
        now = datetime(2026, 8, 15, tzinfo=UTC)
        self.assertEqual(parse_retry_after_seconds({"retry-after": "2.5"}), 2.5)
        self.assertAlmostEqual(
            parse_retry_after_seconds(
                format_datetime(now + timedelta(seconds=7)), now=now
            ),
            7.0,
        )
        self.assertIsNone(parse_retry_after_seconds("nan"))

    def test_explicit_length_finish_reason_reports_truncation(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                response = json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {"content": "{"},
                            }
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with _Server(Handler) as server:
            client = StructuredModelClient(
                OpenAICompatibleSettings(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "model",
                    api_key="secret",
                )
            )
            with self.assertRaisesRegex(ValidationError, "truncated"):
                client.request(
                    prompt="test", schema_name="result", schema=_SCHEMA, timeout=5
                )

    def test_remote_plaintext_provider_is_rejected(self) -> None:
        for url in ("http://example.com/v1", "http://192.168.1.20:8000/v1"):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(ValidationError, "must use HTTPS"),
            ):
                OpenAICompatibleSettings(url, "model", api_key="secret").validated()

    def test_ambiguous_or_control_character_provider_url_is_rejected(self) -> None:
        for url in (
            "https://api.example.com/v1\\@elsewhere.example",
            "https://api.example.com/v1\nignored",
            "https://api.example.com/a b",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                OpenAICompatibleSettings(url, "model", api_key="secret").validated()

    def test_model_and_credential_cannot_inject_request_fields(self) -> None:
        for settings in (
            OpenAICompatibleSettings(
                "https://api.example.com/v1", "model\nother", api_key="secret"
            ),
            OpenAICompatibleSettings(
                "https://api.example.com/v1",
                "model",
                api_key="secret\r\nX-Injected: yes",
            ),
            OpenAICompatibleSettings(
                "https://api.example.com/v1", "model", api_key="密钥"
            ),
        ):
            with self.subTest(settings=settings), self.assertRaises(ValidationError):
                settings.validated()

    def test_literal_loopback_http_provider_is_allowed(self) -> None:
        for url in (
            "http://localhost:8080/v1",
            "http://127.0.0.2:8080/v1",
            "http://[::1]:8080/v1",
        ):
            with self.subTest(url=url):
                self.assertIs(
                    OpenAICompatibleSettings(url, "model", api_key="secret")
                    .validated()
                    .__class__,
                    OpenAICompatibleSettings,
                )

    def test_redirect_is_rejected_without_forwarding_authorization(self) -> None:
        received: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        with _Server(SinkHandler) as sink:

            class RedirectHandler(BaseHTTPRequestHandler):
                def log_message(self, _format: str, *_args: object) -> None:
                    return

                def do_POST(self) -> None:
                    self.send_response(307)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{sink.server_port}/v1/chat/completions",
                    )
                    self.end_headers()

            with _Server(RedirectHandler) as source:
                client = StructuredModelClient(
                    OpenAICompatibleSettings(
                        f"http://127.0.0.1:{source.server_port}/v1",
                        "model",
                        api_key="redirect-secret",
                    )
                )
                with self.assertRaises(PCBDraftError):
                    client.request(
                        prompt="test",
                        schema_name="result",
                        schema=_SCHEMA,
                        timeout=5,
                    )
        self.assertEqual(received, [])

    def test_output_is_validated_locally_against_requested_schema(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                response = json.dumps(
                    {"choices": [{"message": {"content": '{"answer": 42}'}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with _Server(Handler) as server:
            client = StructuredModelClient(
                OpenAICompatibleSettings(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "model",
                    api_key="secret",
                )
            )
            with self.assertRaisesRegex(ValidationError, "does not satisfy"):
                client.request(
                    prompt="test", schema_name="result", schema=_SCHEMA, timeout=5
                )

    def test_duplicate_keys_and_non_json_numbers_are_rejected(self) -> None:
        contents = ['{"answer":"first","answer":"second"}', '{"answer":NaN}']
        requests_seen = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                nonlocal requests_seen
                requests_seen += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                response = json.dumps(
                    {"choices": [{"message": {"content": contents[requests_seen % 2]}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with _Server(Handler) as server:
            client = StructuredModelClient(
                OpenAICompatibleSettings(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "model",
                    api_key="secret",
                )
            )
            for case in ("duplicate", "non-finite"):
                with (
                    self.subTest(case=case),
                    self.assertRaisesRegex(ValidationError, "invalid JSON content"),
                ):
                    client.request(
                        prompt="test",
                        schema_name="result",
                        schema=_SCHEMA,
                        timeout=5,
                    )
            self.assertGreater(requests_seen, 2)

    def test_receipt_reports_loopback_plaintext_transport_honestly(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                response = json.dumps(
                    {"choices": [{"message": {"content": '{"answer":"ok"}'}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with _Server(Handler) as server:
            client = StructuredModelClient(
                OpenAICompatibleSettings(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "model",
                    api_key="secret",
                )
            )
            value, receipt = client.request(
                prompt="test", schema_name="result", schema=_SCHEMA, timeout=5
            )
        self.assertEqual(value, {"answer": "ok"})
        self.assertEqual(receipt["prompt_transport"], "loopback-http-body")
        self.assertTrue(receipt["schema_valid"])


if __name__ == "__main__":
    unittest.main()
