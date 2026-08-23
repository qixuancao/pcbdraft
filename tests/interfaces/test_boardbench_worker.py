from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.interfaces.boardbench_worker import (
    REQUEST_SCHEMA,
    REQUEST_VERSION,
    _WorkerRuntime,
    parse_request,
    run_worker,
)


class _Service:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def create_empty_project(self, name: str) -> dict[str, object]:
        self.events.append(("create", name))
        return {"project": {"id": f"project-{name}"}}


class BoardBenchWorkerTests(unittest.TestCase):
    """Deterministic worker checks; these never invoke a model or KiCad."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.request = self.root / "request.json"
        self.repository = self.root / "repository"
        self.repository_config = self.root / "repository-config.json"
        self.trace = self.root / "trace.jsonl"
        self.usage = self.root / "usage.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_request(self, **changes: object) -> None:
        value: dict[str, object] = {
            "schema": REQUEST_SCHEMA,
            "version": REQUEST_VERSION,
            "run_id": "case-00-run-1",
            "prompt": "Design a small sensor board from this request.",
        }
        value.update(changes)
        atomic_write_json(self.request, value)

    def _argv(self) -> list[str]:
        return [
            "--request",
            str(self.request),
            "--repository",
            str(self.repository),
            "--repository-config",
            str(self.repository_config),
            "--trace",
            str(self.trace),
            "--usage",
            str(self.usage),
        ]

    def test_closed_request_rejects_reference_or_circuit_plan_fields(self) -> None:
        for field in ("reference_path", "evaluator_path", "circuit_plan"):
            with self.subTest(field=field):
                self._write_request(**{field: {"prepared": True}})
                with self.assertRaisesRegex(ValidationError, "unexpected fields"):
                    parse_request(self._argv())

    def test_request_rejects_duplicate_fields(self) -> None:
        self.request.write_text(
            '{"schema":"pcbdraft-boardbench-worker-request","version":1,'
            '"run_id":"one","run_id":"two","prompt":"Build it."}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "duplicate fields"):
            parse_request(self._argv())

    def test_request_rejects_unsafe_id_and_invalid_unicode(self) -> None:
        self._write_request(run_id="../not-a-run")
        with self.assertRaisesRegex(ValidationError, "run id is invalid"):
            parse_request(self._argv())
        self.request.write_text(
            '{"schema":"pcbdraft-boardbench-worker-request","version":1,'
            '"run_id":"valid-run","prompt":"\\ud800"}',
            encoding="ascii",
        )
        with self.assertRaisesRegex(ValidationError, "valid natural-language"):
            parse_request(self._argv())

    def test_sets_local_environment_before_loading_runtime_and_sends_only_prompt(
        self,
    ) -> None:
        self._write_request()
        request = parse_request(self._argv())
        events: list[object] = []
        service = _Service(events)

        def configure(path: Path) -> None:
            events.append(("configure", path))

        def bind(project_id: str | None) -> None:
            events.append(("bind", project_id))

        def launch(argv: list[str], *, permission_mode: str) -> int:
            events.append(("launch", argv, permission_mode))
            return 0

        def load_runtime() -> _WorkerRuntime:
            events.append(
                (
                    "environment",
                    os.environ.get("PCBDRAFT_REPOSITORY_CONFIG"),
                    os.environ.get("PCBDRAFT_DEBUG_TRACE_PATH"),
                )
            )
            return _WorkerRuntime(configure, lambda: service, bind, launch)

        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(run_worker(request, runtime_loader=load_runtime), 0)
        self.assertEqual(
            events,
            [
                ("environment", str(self.repository_config), str(self.trace)),
                ("configure", self.repository),
                ("create", "case-00-run-1"),
                ("bind", "project-case-00-run-1"),
                (
                    "launch",
                    [
                        "--oneshot",
                        "Design a small sensor board from this request.",
                        "--usage-file",
                        str(self.usage),
                    ],
                    "workspace",
                ),
            ],
        )

    def test_request_is_self_contained_prompt_only_json(self) -> None:
        self._write_request()
        value = json.loads(self.request.read_text(encoding="utf-8"))
        self.assertEqual(set(value), {"schema", "version", "run_id", "prompt"})
        parsed = parse_request(self._argv())
        self.assertEqual(parsed.prompt, value["prompt"])
        self.assertEqual(parsed.run_id, value["run_id"])


if __name__ == "__main__":
    unittest.main()
