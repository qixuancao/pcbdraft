"""Exercise the production model loop and durable tool dispatch on loopback only."""

import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.conversations import ConversationOrchestrator
from tests.agent.test_conversations import ConversationService

requests = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        requests.append(body)
        messages = body["messages"]
        if body.get("tools"):
            system = "\n".join(
                str(message.get("content", ""))
                for message in messages
                if message.get("role") == "system"
            )
            assert "PCBDraft" in system
            assert "You are Hermes Agent" not in system
            assert "You run on Hermes Agent" not in system
        results = [m for m in messages if m.get("role") == "tool"]
        if results:
            receipt = json.loads(results[-1]["content"])
            assert receipt["success"], receipt
            assert receipt["project_id"] == "board-a", receipt
            message = {"role": "assistant", "content": "已查看当前设计，未修改。"}
            reason = "stop"
        elif not body.get("tools"):
            message = {"role": "assistant", "content": "local capability check"}
            reason = "stop"
        else:
            names = {t["function"]["name"] for t in body.get("tools", [])}
            assert "pcb_inspect_project" in names, names
            assert all(n.startswith("pcb_") for n in names), names
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-inspect",
                        "type": "function",
                        "function": {"name": "pcb_inspect_project", "arguments": "{}"},
                    }
                ],
            }
            reason = "tool_calls"
        response = {
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "created": 1,
            "model": "native-test",
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        if body.get("stream"):
            delta = dict(message)
            for i, call in enumerate(delta.get("tool_calls", [])):
                call["index"] = i
            response["object"] = "chat.completion.chunk"
            response["choices"] = [
                {"index": 0, "delta": delta, "finish_reason": reason}
            ]
            encoded = (
                "data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n"
            ).encode()
        else:
            encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/event-stream" if body.get("stream") else "application/json",
        )
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
connect = socket.socket.connect


def local_connect(sock, address):
    if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
        raise OSError("non-loopback network disabled by the native integration check")
    return connect(sock, address)


try:
    with tempfile.TemporaryDirectory(prefix="pcbdraft-native-loop-") as temporary:
        root = Path(temporary)
        home = root / "runtime"
        home.mkdir()
        url = f"http://127.0.0.1:{server.server_port}/v1"
        (home / "config.yaml").write_text(f"""model:
  default: native-test
  provider: custom
  base_url: {url}
  api_key: local-test-only
display:
  streaming: false
compression:
  enabled: false
""")
        with (
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(home), "PCBDRAFT_DEBUG_TRACE": "0"},
            ),
            patch.object(socket.socket, "connect", local_connect),
        ):
            service = ConversationService(root)
            runner = ConversationOrchestrator(service)
            turn = runner.start_turn("board-a", "只查看当前设计，不要修改")
            runner.run_turn(
                "board-a",
                turn.turn_id,
                timeout=15,
                cancellation_requested=lambda: False,
            )
            record = runner.store("board-a").load(turn.turn_id)
            assert record.status.value == "completed", record.status
            assert [t.tool_name for t in record.tool_runs] == ["pcb_inspect_project"], (
                record.tool_runs
            )
            assert record.assistant_texts == ("已查看当前设计，未修改。",), (
                record.assistant_texts
            )
            assert sum(bool(r.get("tools")) for r in requests) == 2, len(requests)
            followup = runner.start_turn("board-a", "刚才查看了什么？")
            runner.run_turn(
                "board-a",
                followup.turn_id,
                timeout=15,
                cancellation_requested=lambda: False,
            )
            followup_record = runner.store("board-a").load(followup.turn_id)
            assert followup_record.status.value == "completed"
            assert not followup_record.tool_runs, followup_record.tool_runs
            assert any(
                m.get("role") == "user"
                and m.get("content") == "只查看当前设计，不要修改"
                for m in requests[-1]["messages"]
            ), "prior user message missing"
            print(
                "NATIVE_ROUNDTRIP_OK: tool schema, SSE, durable dispatch, reply and second-turn history"
            )
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
