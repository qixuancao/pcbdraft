from __future__ import annotations

import unittest

from pcbdraft.services.gui_session_contract import (
    action_response,
    active_turn,
    session_message,
    session_response,
    visible_job,
)


class GuiSessionContractTests(unittest.TestCase):
    def test_action_response_preserves_start_and_idle_stop_shapes(self) -> None:
        self.assertEqual(
            action_response(
                project_id="board-one",
                job_id="job-1",
                turn_id="turn-1",
                status="queued",
            ),
            {
                "project_id": "board-one",
                "job_id": "job-1",
                "turn_id": "turn-1",
                "status": "queued",
            },
        )
        self.assertEqual(
            action_response(
                project_id="board-one",
                job_id=None,
                status="idle",
                include_turn_id=False,
            ),
            {"project_id": "board-one", "job_id": None, "status": "idle"},
        )

    def test_session_response_serializes_the_exact_public_allowlist(self) -> None:
        job = {
            "id": "job-1",
            "args": {"turn_id": "turn-1", "private": "omit"},
            "status": "running",
            "attempt": 1,
            "created_at": "2026-09-16T00:00:00Z",
            "started_at": "2026-09-16T00:00:01Z",
            "completed_at": None,
            "result": {
                "project_revision": 7,
                "design_content_hash": "a" * 64,
                "private": "omit",
            },
            "private": "omit",
        }
        message = session_message(
            message_id="turn-1-user",
            turn_id="turn-1",
            role="user",
            text="Route LED",
            status="running",
            created_at="2026-09-16T00:00:00Z",
        )
        response = session_response(
            project_id="board-one",
            status="running",
            active=active_turn(job, turn_id="turn-1"),
            pending_approval=None,
            messages=[message],
            legacy_session_id="legacy-session",
            jobs=[visible_job(job)],
            canonical_revision=7,
            design_revision=3,
            content_hash="a" * 64,
            product_status={
                "conversation": "running",
                "candidate_gate": {"outcome": "incomplete", "passed": False},
                "task_coverage": {"outcome": "incomplete", "complete": False},
            },
        )

        self.assertEqual(
            set(response),
            {
                "schema",
                "version",
                "project_id",
                "status",
                "active_turn",
                "pending_approval",
                "messages",
                "legacy_session_id",
                "product_status",
                "jobs",
                "canonical_revision",
                "design_revision",
                "content_hash",
            },
        )
        self.assertEqual(response["schema"], "pcbdraft-gui-session")
        self.assertEqual(response["version"], 3)
        self.assertEqual(response["legacy_session_id"], "legacy-session")
        self.assertEqual(response["product_status"]["conversation"], "running")
        self.assertEqual(response["active_turn"]["job_id"], "job-1")
        self.assertEqual(response["jobs"][0]["project_revision"], 7)
        self.assertNotIn("private", response["jobs"][0])


if __name__ == "__main__":
    unittest.main()
