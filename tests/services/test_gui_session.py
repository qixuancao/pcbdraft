from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.interfaces.gui_worker import (
    EVENT_SCHEMA,
    RESULT_SCHEMA,
    WORKER_VERSION,
)
from pcbdraft.services.gui_session import GuiSessionManager


class _Service:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.project = root / "projects" / "board-one"
        self.project.mkdir(mode=0o700, parents=True)

    def project_root(self, project_id: str) -> Path:
        if project_id != "board-one":
            raise ValidationError("project does not exist")
        return self.project


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 999_999_999
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class GuiSessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir(mode=0o700)
        self.cache = self.root / "cache"
        self.service = _Service(self.repository)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_is_fail_fast_and_prompt_is_only_in_private_request(self) -> None:
        manager = GuiSessionManager(self.service, self.cache)
        process = _FakeProcess()
        captured: list[object] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            captured.extend((command, kwargs))
            return process

        user_message = "Place U1 with api_key=topsecretvalue"
        with mock.patch(
            "pcbdraft.services.gui_session.subprocess.Popen", side_effect=fake_popen
        ):
            summary = manager.start("board-one", user_message)
            with self.assertRaisesRegex(PCBDraftError, "active GUI turn"):
                manager.start("board-one", "A second turn")

        command = captured[0]
        self.assertNotIn(user_message, command)
        request_path = Path(command[-1])
        self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o600)
        self.assertIn("topsecretvalue", request_path.read_text(encoding="utf-8"))
        session = manager.session("board-one")
        self.assertEqual(session["schema"], "pcbdraft-gui-session")
        self.assertEqual(session["active_turn"]["turn_id"], summary["turn_id"])
        self.assertNotIn("topsecretvalue", session["messages"][0]["text"])
        self.assertFalse(
            {"pid", "request_path", "result_path", "event_dir"}
            & set(session["active_turn"])
        )

    def test_completion_and_safe_events_persist_across_reconnect(self) -> None:
        manager = GuiSessionManager(self.service, self.cache)
        process = _FakeProcess()
        with mock.patch.object(manager, "_spawn_worker", return_value=process):
            started = manager.start("board-one", "Route the LED net")
        state_dir = manager._session_dir("board-one")
        turn_dir = state_dir / "turns" / started["turn_id"]
        event_dir = turn_dir / "events"
        event_dir.mkdir(mode=0o700)
        atomic_write_json(
            event_dir / "00000001.json",
            {
                "schema": EVENT_SCHEMA,
                "version": WORKER_VERSION,
                "project_id": "board-one",
                "turn_id": started["turn_id"],
                "ordinal": 1,
                "kind": "tool",
                "state": "completed",
                "tool": "pcb_validate",
                "duration_ms": 12,
                "created_at": "2026-08-30T00:00:00Z",
            },
        )
        atomic_write_json(
            turn_dir / "result.json",
            {
                "schema": RESULT_SCHEMA,
                "version": WORKER_VERSION,
                "project_id": "board-one",
                "turn_id": started["turn_id"],
                "status": "completed",
                "final_response": "Finished. token=anothersecretvalue",
                "error_code": None,
                "completed_at": "2026-08-30T00:00:01Z",
            },
        )
        process.returncode = 0

        reconnected = GuiSessionManager(self.service, self.cache)
        session = reconnected.session("board-one")
        self.assertEqual(session["status"], "idle")
        self.assertEqual(session["messages"][-1]["role"], "assistant")
        self.assertNotIn("anothersecretvalue", session["messages"][-1]["text"])
        events = reconnected.events("board-one", after=1)
        self.assertEqual([event["sequence"] for event in events], [2, 3])
        self.assertEqual(events[0]["tool"], "pcb_validate")
        self.assertFalse((turn_dir / "request.json").exists())
        with self.assertRaisesRegex(ValidationError, "sequence"):
            reconnected.events("board-one", after=True)

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group assertion")
    def test_process_group_cancellation_and_cross_manager_busy_lock(self) -> None:
        command = (sys.executable, "-c", "import time; time.sleep(60)")
        manager = GuiSessionManager(
            self.service,
            self.cache,
            worker_command=command,
            stop_grace_seconds=0.05,
        )
        started = manager.start("board-one", "Wait for cancellation")
        second = GuiSessionManager(self.service, self.cache)
        with self.assertRaisesRegex(PCBDraftError, "active GUI turn"):
            second.start("board-one", "Do not start")
        stopped = manager.stop("board-one")
        self.assertEqual(stopped["turn_id"], started["turn_id"])
        self.assertEqual(stopped["status"], "cancelled")
        session = manager.session("board-one")
        self.assertEqual(session["status"], "idle")
        self.assertEqual(session["messages"][0]["status"], "cancelled")
        self.assertEqual(manager.shutdown(), [])


if __name__ == "__main__":
    unittest.main()
