"""Exercise iteration-summary cancellation through the native conversation loop."""

from __future__ import annotations

import json
import os
import select
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from pcbdraft.agent.conversations import (
    ConversationOrchestrator,
    create_conversation_agent,
)
from pcbdraft.agent.turns import ToolRunStatus, TurnStatus
from pcbdraft.services.session_db import SessionDB
from tests.agent.test_conversations import ConversationService


def _response(message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "created": 1,
        "model": "native-test",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


class SummaryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), SummaryHandler)
        self.summary_blocked = threading.Event()
        self.release = threading.Event()
        self.peer_closed = threading.Event()
        self.summary_handler_done = threading.Event()
        self.requests: list[dict[str, Any]] = []


class SummaryHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _write_response(self, body: dict[str, Any], *, stream: bool) -> None:
        if stream:
            message = body["choices"][0]["message"]
            delta = dict(message)
            for index, call in enumerate(delta.get("tool_calls", [])):
                call["index"] = index
            body["object"] = "chat.completion.chunk"
            body["choices"] = [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": body["choices"][0]["finish_reason"],
                }
            ]
            encoded = ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode()
            content_type = "text/event-stream"
        else:
            encoded = json.dumps(body).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, SummaryServer)
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        server.requests.append(body)
        messages = body.get("messages", [])
        is_summary = any(
            message.get("role") == "user"
            and message.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST
            for message in messages
            if isinstance(message, dict)
        )
        if is_summary:
            server.summary_blocked.set()
            try:
                while not server.release.is_set():
                    readable, _, _ = select.select([self.connection], [], [], 0.05)
                    if not readable:
                        continue
                    try:
                        data = self.connection.recv(1)
                    except OSError:
                        server.peer_closed.set()
                        return
                    if not data:
                        server.peer_closed.set()
                        return
            finally:
                server.summary_handler_done.set()
            return
        if body.get("tools"):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-inspect",
                        "type": "function",
                        "function": {
                            "name": "pcb_inspect_project",
                            "arguments": "{}",
                        },
                    }
                ],
            }
            self._write_response(
                _response(message, "tool_calls"), stream=body.get("stream") is True
            )
            return
        self._write_response(
            _response(
                {"role": "assistant", "content": "local capability check"},
                "stop",
            ),
            stream=False,
        )


def main() -> None:
    server = SummaryServer()
    server_worker = threading.Thread(
        target=server.serve_forever,
        name="iteration-summary-model",
        daemon=False,
    )
    server_worker.start()
    original_connect = socket.socket.connect

    def local_connect(sock: socket.socket, address: Any) -> Any:
        if not isinstance(address, tuple) or address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            raise OSError("non-loopback network disabled by summary probe")
        return original_connect(sock, address)

    run_worker: threading.Thread | None = None
    cancel = threading.Event()
    errors: list[Exception] = []
    results: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(
            prefix="pcbdraft-summary-cancel-"
        ) as temporary:
            root = Path(temporary)
            runtime_home = root / "runtime"
            runtime_home.mkdir()
            (runtime_home / "logs").mkdir()
            (runtime_home / "config.yaml").write_text(
                "\n".join(
                    [
                        "model:",
                        "  default: native-test",
                        "  provider: custom",
                        f"  base_url: http://127.0.0.1:{server.server_port}/v1",
                        "  api_key: local-test-only",
                        "display:",
                        "  streaming: false",
                        "compression:",
                        "  enabled: false",
                        "",
                    ]
                )
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PCBDRAFT_RUNTIME_HOME": str(runtime_home),
                        "PCBDRAFT_DEBUG_TRACE": "0",
                        "PCBDRAFT_RUNTIME_KANBAN_TASK": "summary-cancel-probe",
                    },
                ),
                patch.object(socket.socket, "connect", local_connect),
                patch(
                    "pcbdraft.agent.turn_finalizer._record_kanban_budget_exhausted"
                ) as budget_failure,
            ):
                service = ConversationService(root)
                cleanup_calls: list[str] = []
                persist_calls: list[int] = []
                request_close_calls: list[tuple[str, int]] = []
                agent_results: list[dict[str, Any]] = []
                session_ids: list[str] = []
                run_thread_ids: list[int] = []
                agent_closed = threading.Event()

                def factory(*, session_id: str, session_db: Any) -> Any:
                    session_ids.append(session_id)
                    agent = create_conversation_agent(
                        session_id=session_id, session_db=session_db
                    )
                    agent.max_iterations = 1
                    original_cleanup = agent._cleanup_task_resources
                    original_persist = agent._persist_session
                    original_request_close = agent._close_request_openai_client
                    original_run = agent.run_conversation
                    original_close = agent.close

                    def cleanup(task_id: str) -> None:
                        cleanup_calls.append(task_id)
                        original_cleanup(task_id)

                    def persist(messages: list[Any], history: list[Any]) -> None:
                        persist_calls.append(len(messages))
                        original_persist(messages, history)

                    def run_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
                        result = original_run(*args, **kwargs)
                        agent_results.append(result)
                        return result

                    def close_request(client: Any, *, reason: str) -> None:
                        request_close_calls.append((reason, threading.get_ident()))
                        original_request_close(client, reason=reason)

                    def close() -> None:
                        try:
                            original_close()
                        finally:
                            agent_closed.set()

                    agent._cleanup_task_resources = cleanup
                    agent._persist_session = persist
                    agent._close_request_openai_client = close_request
                    agent.run_conversation = run_conversation
                    agent.close = close
                    return agent

                runner = ConversationOrchestrator(service, agent_factory=factory)
                turn = runner.start_turn("board-a", "只查看当前设计，不要修改")

                def run_turn() -> None:
                    run_thread_ids.append(threading.get_ident())
                    try:
                        results.append(
                            runner.run_turn(
                                "board-a",
                                turn.turn_id,
                                timeout=15,
                                cancellation_requested=cancel.is_set,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - report any worker failure
                        errors.append(exc)

                run_worker = threading.Thread(
                    target=run_turn,
                    name="iteration-summary-conversation",
                    daemon=False,
                )
                run_worker.start()
                assert server.summary_blocked.wait(timeout=10), (
                    "iteration summary did not reach blocked loopback transport"
                )
                cancel_started = time.monotonic()
                cancel.set()
                run_worker.join(timeout=4)
                cancel_elapsed = time.monotonic() - cancel_started
                exited_before_release = not run_worker.is_alive()
                peer_closed_before_release = server.peer_closed.wait(timeout=1)
                handler_done_before_release = server.summary_handler_done.wait(
                    timeout=1
                )
                if run_worker.is_alive():
                    server.release.set()
                    run_worker.join(timeout=4)

                assert exited_before_release, (
                    "summary cancellation waited for server release"
                )
                assert not run_worker.is_alive(), "native conversation worker leaked"
                assert peer_closed_before_release, (
                    "summary model connection stayed open"
                )
                assert handler_done_before_release, (
                    "summary server handler stayed blocked"
                )
                assert not errors, f"native conversation raised {errors!r}"
                assert len(results) == 1, "native conversation returned no result"
                record = runner.store("board-a").load(turn.turn_id)
                assert record.status is TurnStatus.CANCELLED, record.status
                assert len(record.tool_runs) == 1, record.tool_runs
                assert record.tool_runs[0].status is ToolRunStatus.COMPLETED
                executions = [call for call in service.calls if call[0] == "execute"]
                assert [call[2] for call in executions] == ["inspect_project"], (
                    executions
                )
                summary_requests = [
                    request
                    for request in server.requests
                    if any(
                        message.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST
                        for message in request.get("messages", [])
                        if isinstance(message, dict)
                    )
                ]
                assert len(summary_requests) == 1, summary_requests
                assert cleanup_calls, "turn finalizer skipped resource cleanup"
                assert persist_calls, "turn finalizer skipped session persistence"
                assert len(agent_results) == 1, agent_results
                assert agent_results[0]["interrupted"] is True, agent_results[0]
                assert agent_results[0]["completed"] is False, agent_results[0]
                assert agent_results[0]["failed"] is False, agent_results[0]
                assert agent_results[0]["final_response"] is None, agent_results[0]
                assert (
                    agent_results[0]["turn_exit_reason"]
                    == "interrupted_during_iteration_summary"
                ), agent_results[0]
                budget_failure.assert_not_called()
                assert len(run_thread_ids) == 1, run_thread_ids
                assert any(
                    reason == "request_error_cleanup" and thread_id != run_thread_ids[0]
                    for reason, thread_id in request_close_calls
                ), request_close_calls
                assert agent_closed.wait(timeout=1), "conversation skipped agent.close"
                assert len(session_ids) == 1, session_ids
                persisted_db = SessionDB(
                    runner.store("board-a").turns_root / "conversations.sqlite3"
                )
                try:
                    persisted = persisted_db.get_messages_as_conversation(
                        session_ids[0], repair_alternation=True
                    )
                finally:
                    persisted_db.close()
                assert any(
                    message.get("role") == "user"
                    and message.get("content") == "只查看当前设计，不要修改"
                    for message in persisted
                ), persisted
                assert any(message.get("role") == "tool" for message in persisted), (
                    persisted
                )
                assert not any(
                    message.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST
                    for message in persisted
                ), persisted
                assert persisted[-1].get("role") == "assistant", persisted
                print(
                    "NATIVE_ITERATION_SUMMARY_CANCEL_OK:"
                    f"elapsed={cancel_elapsed:.3f}:status={record.status.value}"
                )
    finally:
        server.release.set()
        if run_worker is not None and run_worker.is_alive():
            run_worker.join(timeout=2)
        server.shutdown()
        server.server_close()
        server_worker.join(timeout=2)
        assert not server_worker.is_alive(), "summary server worker leaked"


if __name__ == "__main__":
    main()
