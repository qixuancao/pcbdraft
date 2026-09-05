"""Probe native conversation cancellation against a blocked loopback model."""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.conversations import ConversationOrchestrator
from pcbdraft.agent.turns import TurnStatus
from tests.agent.test_conversations import ConversationService


class BlockingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode: str) -> None:
        super().__init__(("127.0.0.1", 0), BlockingHandler)
        self.mode = mode
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.peer_closed = threading.Event()
        self.handler_done = threading.Event()
        self.requests: list[dict[str, Any]] = []


class BlockingHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, BlockingServer)
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        server.requests.append(body)
        if not body.get("tools"):
            response = {
                "id": "chatcmpl-auxiliary",
                "object": "chat.completion",
                "created": 1,
                "model": "native-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "local capability check",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if server.mode == "mid_sse":
            chunk = {
                "id": "chatcmpl-blocked",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "native-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "处理中"},
                        "finish_reason": None,
                    }
                ],
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        server.blocked.set()
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
            server.handler_done.set()


def _run(mode: str) -> None:
    server = BlockingServer(mode)
    server_worker = threading.Thread(
        target=server.serve_forever,
        name=f"blocked-model-{mode}",
        daemon=False,
    )
    server_worker.start()
    original_connect = socket.socket.connect

    def local_connect(sock: socket.socket, address: Any) -> Any:
        if not isinstance(address, tuple) or address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            raise OSError("non-loopback network disabled by cancellation probe")
        return original_connect(sock, address)

    run_error: list[Exception] = []
    run_result: list[dict[str, Any]] = []
    cancel = threading.Event()
    run_worker: threading.Thread | None = None
    runner: ConversationOrchestrator | None = None
    turn_id = ""
    cancel_elapsed = float("inf")
    cancelled_without_release = False
    service: ConversationService | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"pcbdraft-cancel-{mode}-"
        ) as temporary:
            root = Path(temporary)
            runtime_home = root / "runtime"
            runtime_home.mkdir()
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            (runtime_home / "config.yaml").write_text(
                "\n".join(
                    [
                        "model:",
                        "  default: native-test",
                        "  provider: custom",
                        f"  base_url: {base_url}",
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
                    },
                ),
                patch.object(socket.socket, "connect", local_connect),
            ):
                service = ConversationService(root)
                runner = ConversationOrchestrator(service)
                turn = runner.start_turn("board-a", "只查看当前设计，不要修改")
                turn_id = turn.turn_id

                def run_turn() -> None:
                    try:
                        run_result.append(
                            runner.run_turn(
                                "board-a",
                                turn_id,
                                timeout=15,
                                cancellation_requested=cancel.is_set,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - report any worker failure
                        run_error.append(exc)

                run_worker = threading.Thread(
                    target=run_turn,
                    name=f"conversation-{mode}",
                    daemon=False,
                )
                run_worker.start()
                assert server.blocked.wait(timeout=8), (
                    f"{mode}: model request did not reach the blocked transport"
                )
                cancel_started = time.monotonic()
                cancel.set()
                run_worker.join(timeout=4)
                cancel_elapsed = time.monotonic() - cancel_started
                cancelled_without_release = not run_worker.is_alive()
                peer_closed_without_release = server.peer_closed.wait(timeout=1)
                handler_finished_without_release = server.handler_done.wait(timeout=1)
                if run_worker.is_alive():
                    server.release.set()
                    run_worker.join(timeout=4)

                assert cancelled_without_release, (
                    f"{mode}: cancellation did not stop the native conversation "
                    "before the model server was released"
                )
                assert not run_worker.is_alive(), f"{mode}: conversation worker leaked"
                assert peer_closed_without_release, (
                    f"{mode}: model connection stayed open after cancellation"
                )
                assert handler_finished_without_release, (
                    f"{mode}: blocked model handler stayed alive after cancellation"
                )
                assert not run_error, f"{mode}: cancellation raised {run_error!r}"
                assert len(run_result) == 1, f"{mode}: missing run result"
                record = runner.store("board-a").load(turn_id)
                assert record.status is TurnStatus.CANCELLED, (
                    f"{mode}: turn ended as {record.status.value}"
                )
                assert not record.tool_runs, f"{mode}: tool run was journaled"
                assert not any(call[0] == "execute" for call in service.calls), (
                    f"{mode}: PCB tool executed after cancellation"
                )
                model_requests = [
                    request for request in server.requests if request.get("tools")
                ]
                assert len(model_requests) == 1, (
                    f"{mode}: cancellation caused model retry: {len(model_requests)}"
                )
                if mode == "mid_sse":
                    assert model_requests[0].get("stream") is True, (
                        "mid_sse: native model request did not enable streaming"
                    )
                print(
                    f"NATIVE_TRANSPORT_CANCEL_OK:{mode}:"
                    f"elapsed={cancel_elapsed:.3f}:status={record.status.value}"
                )
    finally:
        server.release.set()
        if run_worker is not None and run_worker.is_alive():
            run_worker.join(timeout=2)
        server.shutdown()
        server.server_close()
        server_worker.join(timeout=2)
        assert not server_worker.is_alive(), f"{mode}: server worker leaked"


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"no_headers", "mid_sse"}:
        raise SystemExit("usage: native_conversation_cancel_fixture MODE")
    _run(sys.argv[1])
