from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.agent.turns import AgentTurnStore, TurnRecord
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.model.api import (
    ModelTransportError,
    OpenAICompatibleSettings,
    OpenAIResponsesClient,
    ResponsesFunctionCall,
)
from pcbdraft.model.tool_calls import (
    ConfiguredPCBCallProducer,
    provider_agent_protocol,
)


def _view(*, status: str = "draft", revision: int = 0) -> dict[str, Any]:
    return {
        "project": {
            "id": "board",
            "name": "Board",
            "status": status,
            "design_revision": 0,
        },
        "state": {"revision": revision},
        "conversation": {"messages": [], "proposal": None},
        "design": None,
        "artifacts": {"validation": None},
        "attempts": [],
        "active_change": None,
    }


def _function_call(
    *,
    call_id: str = "provider-call-1",
    name: str = "pcb_plan_request",
    arguments: str = '{"message":"Build a sensor board"}',
) -> dict[str, Any]:
    return {
        "id": "response-item-id-must-not-be-used",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
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


def _responses_handler(
    output: list[dict[str, Any]],
    requests: list[bool],
    *,
    envelope_overrides: dict[str, Any] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            requests.append(True)
            self.rfile.read(int(self.headers["Content-Length"]))
            envelope = {"id": "response", "status": "completed", "output": output}
            if envelope_overrides is not None:
                envelope.update(envelope_overrides)
            payload = json.dumps(envelope).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class _ProducerService:
    def __init__(self, root: Path, provider: object) -> None:
        self.root = root / "board"
        self.root.mkdir()
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        self.provider = provider

    def project_root(self, project_id: str) -> Path:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return self.root


def _turn(
    service: _ProducerService,
    *,
    message: str = "Build a sensor board",
    turn_id: str = "turn-router-test",
) -> TurnRecord:
    return AgentTurnStore(service.root, service.locks_root).begin(
        project_id="board",
        thread_id="main",
        turn_id=turn_id,
        user_message=message,
        baseline_revision=0,
    )


def _settings(
    *,
    base_url: str = "https://api.openai.com/v1",
    provider_id: str = "openai",
) -> OpenAICompatibleSettings:
    return OpenAICompatibleSettings(
        base_url,
        "gpt-test",
        api_key="test-secret",
        provider_id=provider_id,
        provider_name="Test provider",
    )


class OpenAIResponsesClientTests(unittest.TestCase):
    def test_single_post_uses_flat_strict_tools_and_provider_call_id(self) -> None:
        requests: list[tuple[str, dict[str, Any]]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                requests.append((self.path, json.loads(self.rfile.read(length))))
                payload = json.dumps(
                    {
                        "id": "response-1",
                        "status": "completed",
                        "output": [_function_call()],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with _Server(Handler) as server:
            client = OpenAIResponsesClient(
                _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
            )
            tool = DEFAULT_PCB_TOOL_REGISTRY.resolve(
                "pcb_plan_request"
            ).to_openai_responses_tool()
            call, receipt = client.request_tool_call(
                instructions="Select one PCB tool.",
                input_items=[{"role": "user", "content": "quoted request"}],
                tools=[tool],
                tool_choice="required",
                timeout=5,
            )

        self.assertEqual(len(requests), 1)
        path, body = requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertFalse(body["parallel_tool_calls"])
        self.assertFalse(body["store"])
        self.assertEqual(body["tool_choice"], "required")
        wire_tool = body["tools"][0]
        self.assertEqual(
            set(wire_tool),
            {"type", "name", "description", "parameters", "strict"},
        )
        self.assertEqual(wire_tool["type"], "function")
        self.assertNotIn("function", wire_tool)
        self.assertTrue(wire_tool["strict"])
        self.assertFalse(wire_tool["parameters"]["additionalProperties"])
        self.assertEqual(
            set(wire_tool["parameters"]["required"]),
            set(wire_tool["parameters"]["properties"]),
        )
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call.call_id, "provider-call-1")
        self.assertNotEqual(call.call_id, "response-item-id-must-not-be-used")
        self.assertEqual(call.name, "pcb_plan_request")
        self.assertEqual(call.arguments, {"message": "Build a sensor board"})
        self.assertEqual(receipt["response_id"], "response-1")
        self.assertEqual(receipt["attempts"], 1)

    def test_request_conversation_returns_prose_and_optional_tool_call(self) -> None:
        requests: list[tuple[str, dict[str, Any]]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                requests.append((self.path, json.loads(self.rfile.read(length))))
                payload = json.dumps(
                    {
                        "id": "response-chat",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": "I can do that. "},
                                    {"type": "output_text", "text": "Let me plan it."},
                                ],
                            },
                            _function_call(call_id="provider-call-chat"),
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with _Server(Handler) as server:
            client = OpenAIResponsesClient(
                _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
            )
            tool = DEFAULT_PCB_TOOL_REGISTRY.resolve(
                "pcb_plan_request"
            ).to_openai_responses_tool()
            text, call, receipt = client.request_conversation(
                instructions="Answer conversationally or select a PCB tool.",
                input_items=[{"role": "user", "content": "Build me a board"}],
                tools=[tool],
                timeout=5,
            )

        self.assertEqual(len(requests), 1)
        _path, body = requests[0]
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["max_output_tokens"], 4096)
        self.assertFalse(body["parallel_tool_calls"])
        self.assertEqual(text, "I can do that. Let me plan it.")
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call.call_id, "provider-call-chat")
        self.assertEqual(call.arguments, {"message": "Build a sensor board"})
        self.assertTrue(receipt["has_reply_text"])

    def test_request_conversation_may_answer_in_prose_without_a_tool(self) -> None:
        output = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "This is a chat reply."}],
            }
        ]
        requests: list[bool] = []
        with _Server(_responses_handler(output, requests)) as server:
            client = OpenAIResponsesClient(
                _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
            )
            text, call, receipt = client.request_conversation(
                instructions="Answer conversationally or select a PCB tool.",
                input_items=[{"role": "user", "content": "hello"}],
                tools=[],
                timeout=5,
            )
        self.assertEqual(text, "This is a chat reply.")
        self.assertIsNone(call)
        self.assertTrue(receipt["has_reply_text"])
        self.assertEqual(len(requests), 1)

    def test_request_conversation_rejects_empty_and_refused_output(self) -> None:
        for label, output, error_type, message in (
            (
                "empty",
                [],
                ValidationError,
                "empty conversational response",
            ),
            (
                "refusal",
                [{"type": "refusal", "refusal": "declined"}],
                PCBDraftError,
                "declined the conversational request",
            ),
        ):
            requests: list[bool] = []
            with (
                self.subTest(case=label),
                _Server(_responses_handler(output, requests)) as server,
            ):
                client = OpenAIResponsesClient(
                    _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
                )
                with self.assertRaisesRegex(error_type, message):
                    client.request_conversation(
                        instructions="Answer conversationally or select a PCB tool.",
                        input_items=[{"role": "user", "content": "hello"}],
                        tools=[],
                        timeout=5,
                    )
            self.assertEqual(len(requests), 1)

    def test_responses_router_post_is_never_retried_by_transport(self) -> None:
        request_count = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                nonlocal request_count
                request_count += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = b'{"error":"temporary"}'
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with _Server(Handler) as server:
            client = OpenAIResponsesClient(
                _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
            )
            with self.assertRaises(ModelTransportError) as caught:
                client.request_tool_call(
                    instructions="Select one PCB tool.",
                    input_items=[{"role": "user", "content": "request"}],
                    tools=[],
                    tool_choice="required",
                    timeout=5,
                )

        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(request_count, 1)

    def test_multiple_calls_and_invalid_arguments_are_rejected(self) -> None:
        cases = (
            (
                "multiple calls",
                [_function_call(), _function_call(call_id="provider-call-2")],
                "more than one",
            ),
            (
                "array arguments",
                [_function_call(arguments="[]")],
                "must be an object",
            ),
            (
                "duplicate argument key",
                [_function_call(arguments=('{"message":"first","message":"second"}'))],
                "invalid JSON",
            ),
        )
        for label, output, error in cases:
            requests: list[bool] = []
            with (
                self.subTest(case=label),
                _Server(_responses_handler(output, requests)) as server,
            ):
                client = OpenAIResponsesClient(
                    _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
                )
                with self.assertRaisesRegex(ValidationError, error):
                    client.request_tool_call(
                        instructions="Select one PCB tool.",
                        input_items=[{"role": "user", "content": "request"}],
                        tools=[],
                        tool_choice="required",
                        timeout=5,
                    )
            self.assertEqual(len(requests), 1)

    def test_incomplete_or_unidentified_response_is_never_recorded_complete(
        self,
    ) -> None:
        cases = (
            ({"status": None}, ValidationError, "status is malformed"),
            ({"status": "incomplete"}, PCBDraftError, "did not complete"),
            ({"id": None}, ValidationError, "response id is invalid"),
        )
        for overrides, error_type, message in cases:
            requests: list[bool] = []
            with (
                self.subTest(overrides=overrides),
                _Server(
                    _responses_handler(
                        [_function_call()],
                        requests,
                        envelope_overrides=overrides,
                    )
                ) as server,
            ):
                client = OpenAIResponsesClient(
                    _settings(base_url=f"http://127.0.0.1:{server.server_port}/v1")
                )
                with self.assertRaisesRegex(error_type, message):
                    client.request_tool_call(
                        instructions="Select one PCB tool.",
                        input_items=[{"role": "user", "content": "request"}],
                        tools=[],
                        tool_choice="required",
                        timeout=5,
                    )
            self.assertEqual(len(requests), 1)


class ConfiguredPCBCallProducerTests(unittest.TestCase):
    def test_capability_gate_requires_exact_openai_provider_and_host(self) -> None:
        cases = (
            (_settings(), "native-responses"),
            (
                _settings(base_url="https://mirror.example/v1"),
                "local-policy",
            ),
            (
                _settings(provider_id="custom-openai"),
                "local-policy",
            ),
        )
        for settings, expected in cases:
            with self.subTest(provider=settings.provider_id, url=settings.base_url):
                self.assertEqual(
                    provider_agent_protocol(SimpleNamespace(settings=settings)),
                    expected,
                )
        self.assertEqual(provider_agent_protocol(SimpleNamespace()), "local-policy")

    def test_non_native_provider_never_constructs_responses_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary),
                SimpleNamespace(
                    settings=_settings(base_url="https://mirror.example/v1")
                ),
            )
            record = _turn(service)
            producer = ConfiguredPCBCallProducer(service)  # type: ignore[arg-type]

            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient",
                side_effect=AssertionError("capability gate was bypassed"),
            ):
                self.assertIsNone(producer.conversation_step(record, _view(), timeout=5))
                proposal = producer.next_call(record, _view(), timeout=5)

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.name, "plan_request")
        self.assertEqual(proposal.source, "runtime_policy")
        self.assertEqual(proposal.arguments, {"message": record.user_message})

    def test_native_decision_marks_model_source_and_replays_completed_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary), SimpleNamespace(settings=_settings())
            )
            record = _turn(service)
            client_calls = 0

            class FakeResponsesClient:
                def __init__(self, settings: OpenAICompatibleSettings) -> None:
                    self.settings = settings

                def request_conversation(
                    self, **kwargs: Any
                ) -> tuple[Any, Any, dict[str, Any]]:
                    nonlocal client_calls
                    client_calls += 1
                    tools = kwargs["tools"]
                    plan_tool = next(
                        tool for tool in tools if tool["name"] == "pcb_plan_request"
                    )
                    self.assert_plan_binding(plan_tool, record.user_message)
                    return (
                        "Planning that board now.",
                        ResponsesFunctionCall(
                            call_id="provider-call-model",
                            name="pcb_plan_request",
                            arguments={"message": record.user_message},
                        ),
                        {
                            "completed": True,
                            "provider_protocol": "openai-responses",
                            "response_id": "response-model",
                        },
                    )

                @staticmethod
                def assert_plan_binding(tool: dict[str, Any], message: str) -> None:
                    if tool["parameters"]["properties"]["message"]["const"] != message:
                        raise AssertionError("plan request was not bound to the turn")

            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient",
                FakeResponsesClient,
            ):
                first = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                ).conversation_step(record, _view(), timeout=5)
                # A fresh producer instance simulates recovery after the completed
                # decision was journaled but before the proposal reached the turn.
                replayed = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                ).conversation_step(record, _view(), timeout=5)

            journal = json.loads(
                (
                    service.root
                    / "agent-turns"
                    / "model-decisions"
                    / f"{record.turn_id}-router.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(client_calls, 1)
        self.assertIsNotNone(first)
        self.assertEqual(first, replayed)
        assert first is not None
        self.assertEqual(first.reply, "Planning that board now.")
        self.assertIsNotNone(first.proposal)
        assert first.proposal is not None
        self.assertEqual(first.proposal.source, "model")
        self.assertEqual(first.proposal.tool_call_id, "provider-call-model")
        self.assertEqual(first.proposal.arguments, {"message": record.user_message})
        self.assertEqual(journal["status"], "completed")
        self.assertEqual(journal["reply"], "Planning that board now.")
        self.assertEqual(journal["call"]["call_id"], "provider-call-model")

    def test_conversational_reply_only_is_durable_and_uses_no_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary), SimpleNamespace(settings=_settings())
            )
            record = _turn(service, message="What does this tool do?")
            client_calls = 0

            class ChatClient:
                def __init__(self, _settings: OpenAICompatibleSettings) -> None:
                    pass

                def request_conversation(
                    self, **_kwargs: Any
                ) -> tuple[Any, Any, dict[str, Any]]:
                    nonlocal client_calls
                    client_calls += 1
                    return (
                        "It turns board descriptions into reviewable KiCad projects.",
                        None,
                        {"completed": True, "response_id": "response-chat"},
                    )

            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient", ChatClient
            ):
                first = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                ).conversation_step(record, _view(), timeout=5)
                replayed = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                ).conversation_step(record, _view(), timeout=5)

            journal = json.loads(
                (
                    service.root
                    / "agent-turns"
                    / "model-decisions"
                    / f"{record.turn_id}-router.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(client_calls, 1)
        self.assertEqual(first, replayed)
        assert first is not None
        self.assertIsNotNone(first.reply)
        self.assertIn("KiCad projects", first.reply)
        self.assertIsNone(first.proposal)
        self.assertEqual(journal["status"], "completed")
        self.assertIsNone(journal["call"])
        self.assertIsNotNone(journal["reply"])

    def test_completed_journal_cannot_inject_a_model_hidden_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary), SimpleNamespace(settings=_settings())
            )
            record = _turn(service)

            class ValidateClient:
                def __init__(self, _settings: OpenAICompatibleSettings) -> None:
                    pass

                def request_conversation(
                    self, **_kwargs: Any
                ) -> tuple[Any, Any, dict[str, Any]]:
                    return (
                        "Running validation now.",
                        ResponsesFunctionCall(
                            call_id="provider-call-validate",
                            name="pcb_validate",
                            arguments={},
                        ),
                        {"completed": True, "response_id": "response-validate"},
                    )

            view = _view(status="generated")
            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient", ValidateClient
            ):
                ConfiguredPCBCallProducer(service).conversation_step(  # type: ignore[arg-type]
                    record, view, timeout=5
                )

            journal_path = (
                service.root
                / "agent-turns"
                / "model-decisions"
                / f"{record.turn_id}-router.json"
            )
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["call"] = {
                "call_id": "provider-call-repair",
                "name": "pcb_repair_candidate",
                "arguments": {
                    "feedback": {
                        "schema": "pcbdraft-agent-repair-feedback",
                        "version": 1,
                        "phase": "validation",
                        "attempt": 1,
                        "summary": "retained validation finding",
                        "findings": ["one deterministic finding"],
                    }
                },
            }
            journal_path.write_text(json.dumps(journal), encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "outside its whitelist"):
                ConfiguredPCBCallProducer(service).conversation_step(  # type: ignore[arg-type]
                    record, view, timeout=5
                )

    def test_model_decision_root_cannot_escape_through_agent_turn_symlink(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = _ProducerService(root, SimpleNamespace(settings=_settings()))
            record = _turn(service)
            turns_root = service.root / "agent-turns"
            retained_turns = service.root / "retained-agent-turns"
            turns_root.rename(retained_turns)
            escaped = root / "escaped-agent-turns"
            escaped.mkdir()
            turns_root.symlink_to(escaped, target_is_directory=True)

            with self.assertRaisesRegex(ValidationError, "agent turn root"):
                ConfiguredPCBCallProducer(service).conversation_step(  # type: ignore[arg-type]
                    record, _view(), timeout=5
                )

            self.assertFalse((escaped / "model-decisions").exists())

    def test_dispatched_journal_fails_closed_to_deterministic_fallback(self) -> None:
        class SimulatedProcessCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary), SimpleNamespace(settings=_settings())
            )
            record = _turn(service)
            client_calls = 0

            class CrashingClient:
                def __init__(self, _settings: OpenAICompatibleSettings) -> None:
                    pass

                def request_conversation(self, **_kwargs: Any) -> Any:
                    nonlocal client_calls
                    client_calls += 1
                    raise SimulatedProcessCrash

            with (
                patch(
                    "pcbdraft.model.tool_calls.OpenAIResponsesClient",
                    CrashingClient,
                ),
                self.assertRaises(SimulatedProcessCrash),
            ):
                ConfiguredPCBCallProducer(service).conversation_step(  # type: ignore[arg-type]
                    record, _view(), timeout=5
                )

            journal_path = (
                service.root
                / "agent-turns"
                / "model-decisions"
                / f"{record.turn_id}-router.json"
            )
            self.assertEqual(
                json.loads(journal_path.read_text(encoding="utf-8"))["status"],
                "dispatched",
            )
            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient",
                side_effect=AssertionError("ambiguous model request was replayed"),
            ):
                step = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                ).conversation_step(record, _view(), timeout=5)

        self.assertEqual(client_calls, 1)
        self.assertIsNotNone(step)
        assert step is not None
        self.assertIsNone(step.reply)
        self.assertIsNotNone(step.proposal)
        assert step.proposal is not None
        self.assertEqual(step.proposal.source, "runtime_policy")
        self.assertEqual(step.proposal.arguments, {"message": record.user_message})

    def test_failed_journal_uses_deterministic_fallback_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary), SimpleNamespace(settings=_settings())
            )
            record = _turn(service)
            client_calls = 0

            class FailingClient:
                def __init__(self, _settings: OpenAICompatibleSettings) -> None:
                    pass

                def request_conversation(self, **_kwargs: Any) -> Any:
                    nonlocal client_calls
                    client_calls += 1
                    raise PCBDraftError("provider rejected routing")

            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient", FailingClient
            ):
                producer = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                )
                first = producer.conversation_step(record, _view(), timeout=5)
                replayed = producer.conversation_step(record, _view(), timeout=5)

            journal = json.loads(
                (
                    service.root
                    / "agent-turns"
                    / "model-decisions"
                    / f"{record.turn_id}-router.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(client_calls, 1)
        self.assertEqual(first, replayed)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertIsNone(first.reply)
        self.assertIsNotNone(first.proposal)
        assert first.proposal is not None
        self.assertEqual(first.proposal.source, "runtime_policy")
        self.assertEqual(journal["status"], "failed")
        self.assertEqual(journal["error"], "PCBDraftError")

    def test_model_cannot_rewrite_plan_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _ProducerService(
                Path(temporary), SimpleNamespace(settings=_settings())
            )
            record = _turn(service, message="Keep this exact PCB request")

            class RewritingClient:
                def __init__(self, _settings: OpenAICompatibleSettings) -> None:
                    pass

                def request_conversation(
                    self, **_kwargs: Any
                ) -> tuple[Any, Any, dict[str, Any]]:
                    return (
                        None,
                        ResponsesFunctionCall(
                            call_id="provider-call-rewrite",
                            name="pcb_plan_request",
                            arguments={"message": "Replace the user's request"},
                        ),
                        {"completed": True},
                    )

            with patch(
                "pcbdraft.model.tool_calls.OpenAIResponsesClient", RewritingClient
            ):
                step = ConfiguredPCBCallProducer(  # type: ignore[arg-type]
                    service
                ).conversation_step(record, _view(), timeout=5)

            journal = json.loads(
                (
                    service.root
                    / "agent-turns"
                    / "model-decisions"
                    / f"{record.turn_id}-router.json"
                ).read_text(encoding="utf-8")
            )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertIsNotNone(step.proposal)
        assert step.proposal is not None
        self.assertEqual(step.proposal.source, "runtime_policy")
        self.assertEqual(step.proposal.name, "plan_request")
        self.assertEqual(step.proposal.arguments, {"message": record.user_message})
        self.assertEqual(journal["status"], "failed")
        self.assertIsNone(journal["call"])
        self.assertEqual(journal["error"], "ValidationError")


if __name__ == "__main__":
    unittest.main()
