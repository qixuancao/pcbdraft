from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.interfaces.gui_worker import (
    EVENT_SCHEMA,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    WORKER_VERSION,
    _WorkerRuntime,
    parse_request,
    run_worker,
)


class _Service:
    def __init__(self, repository: Path, calls: list[object]) -> None:
        self.repository = repository
        self.calls = calls

    def project_root(self, project_id: str) -> Path:
        self.calls.append(("open", project_id))
        return self.repository / "projects" / project_id


class GuiWorkerTests(unittest.TestCase):
    """Closed worker contract tests; no provider or model is invoked."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir(mode=0o700)
        self.turn_dir = self.root / "turn"
        self.turn_dir.mkdir(mode=0o700)
        self.request_path = self.turn_dir / "request.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_request(self, **changes: object) -> None:
        value: dict[str, object] = {
            "schema": REQUEST_SCHEMA,
            "version": WORKER_VERSION,
            "project_id": "board-one",
            "turn_id": "turn-001",
            "prompt": "Place the connector near the board edge.",
            "repository": str(self.repository),
        }
        value.update(changes)
        atomic_write_json(self.request_path, value, mode=0o600)

    def test_closed_private_request_rejects_extra_fields_and_broad_mode(self) -> None:
        self._write_request(tool_args={"path": "/etc/passwd"})
        with self.assertRaisesRegex(ValidationError, "unexpected fields"):
            parse_request(["--request", str(self.request_path)])

        self._write_request()
        self.request_path.chmod(0o644)
        with self.assertRaisesRegex(ValidationError, "permissions are too broad"):
            parse_request(["--request", str(self.request_path)])

    def test_request_rejects_duplicate_fields_and_oversize_before_json(self) -> None:
        self.request_path.write_text(
            '{"schema":"pcbdraft-gui-worker-request","version":1,'
            '"project_id":"board-one","turn_id":"one","turn_id":"two",'
            f'"prompt":"Build it","repository":{json.dumps(str(self.repository))}}}',
            encoding="utf-8",
        )
        self.request_path.chmod(0o600)
        with self.assertRaisesRegex(ValidationError, "duplicate fields"):
            parse_request(["--request", str(self.request_path)])

        self.request_path.write_bytes(b"{" + b"x" * (25 * 1024))
        self.request_path.chmod(0o600)
        with self.assertRaisesRegex(Exception, "exceeds"):
            parse_request(["--request", str(self.request_path)])

    def test_run_binds_existing_project_and_writes_only_safe_result_events(
        self,
    ) -> None:
        self._write_request(prompt="Use api_key=topsecretvalue to place U1.")
        request = parse_request(["--request", str(self.request_path)])
        calls: list[object] = []
        service = _Service(self.repository, calls)

        def install_observer(writer: object) -> None:
            writer.emit("model", "started")
            writer.emit("tool", "started", tool="pcb_validate")
            writer.emit("tool", "completed", tool="terminal", duration_ms=5)

        runtime = _WorkerRuntime(
            create_service=lambda repository: (
                calls.append(("service", repository)) or service
            ),
            pin_service=lambda value: calls.append(("pin", value is service)),
            bind_project=lambda value: calls.append(("bind", value)),
            activate=lambda: calls.append("activate"),
            provider_is_usable=lambda: True,
            run_agent=lambda prompt: (
                calls.append(("prompt", prompt)) or "Done. password=anothersecretvalue"
            ),
            install_observer=install_observer,
        )
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(run_worker(request, runtime_loader=lambda: runtime), 0)
            self.assertEqual(os.environ["PCBDRAFT_DEBUG_TRACE"], "0")
            self.assertEqual(os.environ["PCBDRAFT_PCB_TOOL_CALL_LIMIT"], "500")

        result = load_json_limited(request.result_path, 96 * 1024)
        self.assertEqual(result["schema"], RESULT_SCHEMA)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("anothersecretvalue", result["final_response"])
        self.assertNotIn("topsecretvalue", json.dumps(result))
        events = sorted(request.event_dir.glob("*.json"))
        self.assertEqual(len(events), 2)
        values = [load_json_limited(path, 8192) for path in events]
        self.assertEqual(values[0]["schema"], EVENT_SCHEMA)
        self.assertEqual(values[1]["tool"], "pcb_validate")
        self.assertNotIn("args", json.dumps(values))
        self.assertIn(("open", "board-one"), calls)
        self.assertIn(("bind", None), calls)


if __name__ == "__main__":
    unittest.main()
