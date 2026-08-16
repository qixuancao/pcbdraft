from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pcbdraft.interfaces.tui.controller import TuiController
from pcbdraft.interfaces.tui.review import review_sections
from pcbdraft.interfaces.tui.session import TuiSessionStore
from tests.interfaces.test_tui import FakeService, _view


class TuiSessionTests(unittest.TestCase):
    def test_last_project_pointer_resumes_without_storing_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TuiSessionStore(Path(temporary))
            store.save_project_id("existing-project")
            service = FakeService()

            controller = TuiController(service=service, session_store=store)

            self.assertEqual(controller.project_id, "existing-project")
            self.assertIn("Resumed", controller.notice)
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["project_id"], "existing-project")
            self.assertNotIn("messages", payload)
            self.assertNotIn("provider", payload)

    def test_stale_or_malformed_session_pointer_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TuiSessionStore(Path(temporary))
            store.save_project_id("missing-project")
            self.assertIsNone(store.load_project_id({"existing-project"}))
            store.path.write_text("{}\n", encoding="utf-8")
            self.assertIsNone(store.load_project_id({"existing-project"}))

    def test_review_summary_includes_staged_semantic_diff(self) -> None:
        view = _view("existing-project", status="change_ready")
        view["active_change"] = {
            "request": "Add a status LED",
            "status": "ready",
            "diff": {
                "summary": {
                    "objects_added": 2,
                    "objects_removed": 0,
                    "objects_modified": 1,
                },
                "collections": {
                    "components": {
                        "added": ["status-led"],
                        "removed": [],
                        "modified": [{"id": "series-resistor", "fields": {}}],
                    }
                },
                "board_fields": {},
            },
            "validation": {
                "candidate_ready": True,
                "production_evidence_complete": False,
                "production_ready": False,
            },
        }

        sections = review_sections(view)
        change = next(
            section for section in sections if section.title.startswith("Staged")
        )
        text = "\n".join(change.lines)
        self.assertIn("+2", text)
        self.assertIn("status-led", text)
        self.assertIn("series-resistor", text)
        self.assertIn("Candidate ready: yes", text)

    def test_review_summary_bounds_large_evidence_and_keeps_failed_checks(self) -> None:
        view = _view("existing-project", status="validation_failed")
        proposal = view["conversation"]["proposal"]
        proposal["brief"]["plan_review"]["findings"] = [
            {
                "id": f"finding-{index}",
                "outcome": "fail",
                "summary": "needs attention",
                "evidence": [f"evidence-{item}" for item in range(20)],
            }
            for index in range(100)
        ]
        view["artifacts"]["validation"] = {
            "candidate_ready": False,
            "production_evidence_complete": False,
            "assurance": "provisional",
            "levels": [
                {
                    "level": "L2",
                    "checks": [
                        {
                            "id": "kicad.erc",
                            "state": "completed",
                            "outcome": "fail",
                            "summary": "one required pin is unconnected",
                        }
                    ],
                }
            ],
        }

        sections = review_sections(view)
        self.assertLessEqual(sum(len(section.lines) for section in sections), 320)
        self.assertTrue(all(len(section.lines) <= 80 for section in sections))
        rendered = "\n".join(line for section in sections for line in section.lines)
        self.assertIn("more line(s) omitted", rendered)
        self.assertIn("kicad.erc", rendered)


if __name__ == "__main__":
    unittest.main()
