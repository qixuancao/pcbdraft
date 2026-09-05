from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from pcbdraft.agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from pcbdraft.agent.conversations import create_conversation_agent
from pcbdraft.agent.turn_finalizer import finalize_turn
from pcbdraft.services.session_db import SessionDB


def _can_bind_loopback() -> bool:
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError:
        return False
    return True


def _chat_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="chatcmpl-summary",
        model="native-test",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content=content,
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


@contextmanager
def _native_agent():
    with ExitStack() as cleanup:
        temporary = tempfile.TemporaryDirectory(prefix="pcbdraft-summary-unit-")
        cleanup.callback(temporary.cleanup)
        root = Path(temporary.name)
        runtime_home = root / "runtime"
        runtime_home.mkdir()
        (runtime_home / "logs").mkdir()
        (runtime_home / "config.yaml").write_text(
            """model:
  default: native-test
  provider: custom
  base_url: http://127.0.0.1:1/v1
  api_key: local-test-only
display:
  streaming: false
compression:
  enabled: false
"""
        )
        cleanup.enter_context(
            patch.dict(
                os.environ,
                {
                    "PCBDRAFT_RUNTIME_HOME": str(runtime_home),
                    "PCBDRAFT_DEBUG_TRACE": "0",
                },
            )
        )
        db = SessionDB(root / "conversations.sqlite3")
        cleanup.callback(db.close)
        agent = create_conversation_agent(session_id="summary-unit", session_db=db)
        cleanup.callback(agent.close)
        yield agent


class IterationSummaryTests(unittest.TestCase):
    @unittest.skipUnless(
        _can_bind_loopback(),
        "sandbox does not permit the loopback listener required by this probe",
    )
    def test_native_summary_cancel_closes_turn_and_runs_finalizers(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.agent.native_iteration_summary_cancel_fixture",
            ],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NATIVE_ITERATION_SUMMARY_CANCEL_OK", result.stdout)

    def test_preexisting_cancel_does_not_send_summary(self) -> None:
        with _native_agent() as agent:
            agent._interrupt_requested = True
            interruptible = Mock()
            messages = [{"role": "user", "content": "finish"}]
            with (
                patch.object(agent, "_interruptible_api_call", interruptible),
                self.assertRaises(InterruptedError),
            ):
                agent._handle_max_iterations(messages, 1)

            interruptible.assert_not_called()
            self.assertEqual(messages, [{"role": "user", "content": "finish"}])

    def test_interrupt_does_not_remove_same_text_user_message_by_value(self) -> None:
        with _native_agent() as agent:
            same_text_user = {
                "role": "user",
                "content": MAX_ITERATIONS_SUMMARY_REQUEST,
            }
            messages = [{"role": "user", "content": "finish"}]

            def interrupt(_request: dict[str, Any]) -> None:
                messages.append(same_text_user)
                raise InterruptedError("cancel")

            with (
                patch.object(agent, "_interruptible_api_call", side_effect=interrupt),
                self.assertRaises(InterruptedError),
            ):
                agent._handle_max_iterations(messages, 1)

            self.assertIs(messages[-1], same_text_user)
            self.assertEqual(
                sum(
                    message.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST
                    for message in messages
                ),
                1,
            )

    def test_retry_summary_interrupt_removes_only_synthetic_prompt(self) -> None:
        with _native_agent() as agent:
            requests: list[dict[str, Any]] = []

            def call(request: dict[str, Any]) -> SimpleNamespace:
                requests.append(request)
                if len(requests) == 1:
                    return _chat_response("")
                raise InterruptedError("cancel retry")

            messages = [{"role": "user", "content": "finish the task"}]
            with (
                patch.object(agent, "_interruptible_api_call", side_effect=call),
                patch(
                    "pcbdraft.agent.relay_llm.complete_logical_call"
                ) as complete_logical_call,
                self.assertRaises(InterruptedError),
            ):
                agent._handle_max_iterations(messages, 1)

            self.assertEqual(len(requests), 2)
            self.assertEqual(
                messages,
                [{"role": "user", "content": "finish the task"}],
            )
            self.assertEqual(
                complete_logical_call.call_args.kwargs["outcome"], "cancelled"
            )

    def test_summary_interrupt_runs_finalizers_without_budget_failure(self) -> None:
        with _native_agent() as agent:
            agent.max_iterations = 1
            messages = [{"role": "user", "content": "finish the task"}]
            cleanup = Mock()
            persist = Mock()
            with (
                patch.dict(
                    os.environ,
                    {"PCBDRAFT_RUNTIME_KANBAN_TASK": "summary-unit"},
                ),
                patch.object(
                    agent,
                    "_handle_max_iterations",
                    side_effect=InterruptedError("cancel summary"),
                ),
                patch.object(agent, "_save_trajectory"),
                patch.object(agent, "_cleanup_task_resources", cleanup),
                patch.object(agent, "_persist_session", persist),
                patch(
                    "pcbdraft.agent.turn_finalizer._record_kanban_budget_exhausted"
                ) as budget_failure,
            ):
                result = finalize_turn(
                    agent,
                    final_response=None,
                    api_call_count=1,
                    interrupted=False,
                    failed=False,
                    messages=messages,
                    conversation_history=[],
                    effective_task_id="task",
                    turn_id="turn",
                    user_message="finish the task",
                    original_user_message="finish the task",
                    _should_review_memory=False,
                    _turn_exit_reason="budget_exhausted",
                )

            self.assertTrue(result["interrupted"])
            self.assertFalse(result["failed"])
            self.assertFalse(result["completed"])
            self.assertIsNone(result["final_response"])
            self.assertEqual(
                result["turn_exit_reason"],
                "interrupted_during_iteration_summary",
            )
            cleanup.assert_called_once_with("task")
            persist.assert_called_once()
            budget_failure.assert_not_called()

    def test_chat_summary_success_and_empty_response_retry(self) -> None:
        with _native_agent() as agent:
            requests: list[dict[str, Any]] = []
            responses = iter([_chat_response(""), _chat_response("retry summary")])

            def call(request: dict[str, Any]) -> SimpleNamespace:
                requests.append(request)
                return next(responses)

            messages = [{"role": "user", "content": "finish the task"}]
            with patch.object(agent, "_interruptible_api_call", side_effect=call):
                result = agent._handle_max_iterations(messages, 1)

            self.assertEqual(result, "retry summary")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all("tools" not in request for request in requests))
            self.assertEqual(messages[-2]["content"], MAX_ITERATIONS_SUMMARY_REQUEST)
            self.assertEqual(messages[-1]["content"], "retry summary")

    def test_codex_summary_preserves_built_request_shape(self) -> None:
        with _native_agent() as agent:
            agent.api_mode = "codex_responses"
            request = {"model": "native-test", "input": ["exact"], "tools": ["drop"]}
            transport = Mock()
            transport.normalize_response.return_value = SimpleNamespace(
                content="codex summary"
            )
            interruptible = Mock(return_value=object())
            with (
                patch.object(agent, "_build_api_kwargs", return_value=request),
                patch.object(agent, "_get_transport", return_value=transport),
                patch.object(agent, "_interruptible_api_call", interruptible),
            ):
                result = agent._handle_max_iterations(
                    [{"role": "user", "content": "finish"}], 1
                )

            self.assertEqual(result, "codex summary")
            self.assertEqual(
                interruptible.call_args.args[0],
                {"model": "native-test", "input": ["exact"]},
            )

    def test_anthropic_summary_preserves_transport_request_shape(self) -> None:
        with _native_agent() as agent:
            agent.api_mode = "anthropic_messages"
            transport = Mock()
            transport.build_kwargs.return_value = {"anthropic": "exact-shape"}
            transport.normalize_response.return_value = SimpleNamespace(
                content="anthropic summary"
            )
            interruptible = Mock(return_value=object())
            with (
                patch.object(agent, "_get_transport", return_value=transport),
                patch.object(agent, "_interruptible_api_call", interruptible),
            ):
                result = agent._handle_max_iterations(
                    [{"role": "user", "content": "finish"}], 1
                )

            self.assertEqual(result, "anthropic summary")
            self.assertEqual(
                interruptible.call_args.args[0], {"anthropic": "exact-shape"}
            )
