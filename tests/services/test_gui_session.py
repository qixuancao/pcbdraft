from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pcbdraft.agent.turns import TurnStatus
from pcbdraft.core.errors import ValidationError
from pcbdraft.services.gui_session import GuiSessionManager


class _Service:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.project = root / "projects" / "board-one"
        self.project.mkdir(mode=0o700, parents=True)
        self.revision = 7

    def project_root(self, project_id: str) -> Path:
        if project_id != "board-one":
            raise ValidationError("project does not exist")
        return self.project

    def open_project(self, project_id: str):
        self.project_root(project_id)
        return {
            "state": {"revision": self.revision, "design_revision": 3},
            "design": {"content_hash": "a" * 64},
        }


class _Store:
    def __init__(self) -> None:
        self.turns: list[SimpleNamespace] = []

    def list(self, *, limit: int):
        return list(reversed(self.turns))[:limit]

    def load(self, turn_id: str):
        return next(turn for turn in self.turns if turn.turn_id == turn_id)


class _Agent:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def store(self, project_id: str) -> _Store:
        if project_id != "board-one":
            raise ValidationError("project does not exist")
        return self._store

    @staticmethod
    def approval_payload(_turn: object):
        return None


class _Jobs:
    def __init__(self) -> None:
        self.store = _Store()
        self.agent = _Agent(self.store)
        self.records: list[dict[str, object]] = []
        self.shutdown_calls = 0

    def submit(self, project_id: str, action: str, args: dict[str, object]):
        turn_id = f"turn-{len(self.records) + 1}"
        turn = SimpleNamespace(
            turn_id=turn_id,
            user_message=args["text"],
            assistant_texts=(),
            status=TurnStatus.QUEUED,
            created_at="2026-09-04T00:00:00Z",
            updated_at="2026-09-04T00:00:00Z",
        )
        self.store.turns.append(turn)
        job = {
            "id": f"job-{len(self.records) + 1}",
            "project_id": project_id,
            "action": action,
            "args": {"turn_id": turn_id, "timeout": args["timeout"]},
            "status": "queued",
            "attempt": 1,
            "created_at": "2026-09-04T00:00:00Z",
            "started_at": None,
            "completed_at": None,
            "result": None,
        }
        self.records.insert(0, job)
        return job

    def list(self, project_id: str):
        if project_id != "board-one":
            raise ValidationError("project does not exist")
        return self.records

    def cancel(self, project_id: str, job_id: str):
        job = next(item for item in self.list(project_id) if item["id"] == job_id)
        job["status"] = "cancelled"
        turn = self.store.load(str(job["args"]["turn_id"]))  # type: ignore[index]
        turn.status = TurnStatus.CANCELLED
        return job

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class GuiSessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.service = _Service(self.root / "repository")
        self.jobs = _Jobs()
        self.manager = GuiSessionManager(self.service, jobs=self.jobs)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_submits_only_to_canonical_job_runner(self) -> None:
        result = self.manager.start("board-one", "Place U1 with api_key=secretvalue")

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertNotIn("secretvalue", self.jobs.store.turns[0].user_message)
        self.assertFalse(any(self.root.rglob("session.json")))
        with self.assertRaisesRegex(ValidationError, "non-empty"):
            self.manager.start("board-one", " ")

    def test_session_is_a_live_projection_of_canonical_turn_and_job(self) -> None:
        self.manager.start("board-one", "Route the LED net")
        turn = self.jobs.store.turns[0]
        turn.assistant_texts = ("Finished",)
        turn.status = TurnStatus.COMPLETED
        turn.updated_at = "2026-09-04T00:00:01Z"
        self.jobs.records[0]["status"] = "completed"
        self.jobs.records[0]["completed_at"] = "2026-09-04T00:00:01Z"
        self.jobs.records[0]["result"] = {
            "project_revision": 7,
            "design_content_hash": "a" * 64,
        }

        reconnected = GuiSessionManager(self.service, jobs=self.jobs).session(
            "board-one"
        )

        self.assertEqual(reconnected["version"], 2)
        self.assertEqual(reconnected["status"], "idle")
        self.assertEqual(
            [message["role"] for message in reconnected["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual(reconnected["canonical_revision"], 7)
        self.assertEqual(reconnected["content_hash"], "a" * 64)
        self.assertEqual(reconnected["jobs"][0]["project_revision"], 7)

    def test_stop_and_shutdown_use_canonical_job_lifecycle(self) -> None:
        started = self.manager.start("board-one", "Wait")

        stopped = self.manager.stop("board-one")

        self.assertEqual(stopped["job_id"], started["job_id"])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual(self.manager.stop("board-one")["status"], "idle")
        self.assertEqual(self.manager.events("board-one"), [])
        self.assertEqual(self.manager.shutdown(), [])
        self.assertEqual(self.jobs.shutdown_calls, 1)

    def test_legacy_subprocess_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "subprocess workers were removed"):
            GuiSessionManager(
                self.service,
                jobs=self.jobs,
                worker_command=("python", "worker.py"),
            )


if __name__ == "__main__":
    unittest.main()
