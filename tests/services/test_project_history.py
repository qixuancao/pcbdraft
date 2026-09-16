from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pcbdraft.services.project_history import (
    find_project_session_id,
    project_ids_from_history,
)


def _pcb_result(project_id: str) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_name": "pcb_inspect_project",
        "content": json.dumps(
            {"tool": "pcb_inspect_project", "project_id": project_id}
        ),
    }


class ProjectHistoryTests(unittest.TestCase):
    def test_project_evidence_requires_structured_pcb_tool_results(self) -> None:
        history = [
            {"role": "assistant", "content": "project_id=board-a"},
            {
                "role": "tool",
                "tool_name": "terminal",
                "content": '{"project_id":"board-b"}',
            },
            _pcb_result("board-c"),
            {
                "role": "tool",
                "content": {
                    "tool": "pcb_open_project",
                    "project_id": "board-c",
                },
            },
        ]

        self.assertEqual(project_ids_from_history(history), {"board-c"})

    def test_session_selection_rejects_mixed_and_unsafe_candidates(self) -> None:
        histories = {
            "mixed": [_pcb_result("board-a"), _pcb_result("board-b")],
            "unsafe": [_pcb_result("board-a")],
            "exact": [_pcb_result("board-a")],
        }

        class FakeSessionDB:
            def list_sessions_rich(self, **_kwargs):
                return [{"id": name} for name in histories]

            @staticmethod
            def get_session(session_id: str):
                return {"id": session_id, "source": "cli"}

            @staticmethod
            def resolve_resume_session_id(session_id: str):
                return session_id

            @staticmethod
            def assert_resume_safe(session_id: str):
                if session_id == "unsafe":
                    raise RuntimeError("unsafe session")

            @staticmethod
            def get_resume_conversations(session_id: str):
                return (
                    [{"role": "user", "content": "continue"}],
                    histories[session_id],
                )

        with tempfile.TemporaryDirectory() as temporary:
            service = SimpleNamespace(project_root=lambda _project_id: Path(temporary))
            selected = find_project_session_id(FakeSessionDB(), service, "board-a")

        self.assertEqual(selected, "exact")


if __name__ == "__main__":
    unittest.main()
