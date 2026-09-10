from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json, atomic_write_text
from pcbdraft.core.process import CommandResult, run_command
from pcbdraft.verification.boardbench import (
    AUTOMATIC_METRICS,
    BOARD_CATEGORIES,
    BoardBenchCorpus,
    load_campaign,
    load_run,
    store_run,
)
from pcbdraft.verification.boardbench_runner import (
    DEFAULT_WALL_TIMEOUT_SECONDS,
    FIXTURE_LABEL,
    RunnerEnvironment,
    _canonical_hash,
    _effective_tool_call_budget,
    _secret_free_configuration,
    _trace_summary,
    capture_environment,
    create_campaign,
    initialize_run_receipts,
    plan_campaign,
    run_campaign,
)
from pcbdraft.verification.boardbench_v2 import RUN_V2_SCHEMA, load_run_v2

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-08-21T09:00:00Z"


def _case(index: int, category: str) -> dict[str, object]:
    return {
        "id": f"case-{index:02d}",
        "category": category,
        "prompt": f"Design unseen board {index} from this natural-language request.",
        "applicable_metrics": list(AUTOMATIC_METRICS),
        "review_rubric": ["Check the core function."],
        "component_slots": [
            {
                "id": "device",
                "alternatives": [
                    {
                        "part_id": f"part-{index:02d}",
                        "symbol": "Device:R",
                        "footprints": ["Resistor_SMD:R_0603_1608Metric"],
                    }
                ],
            }
        ],
        "net_rules": [],
        "support_requirements": [],
        "forbidden_conditions": [],
        "rating_bounds": [],
        "manufacturing_constraints": [],
        "assembly_constraints": ["Suitable for hand assembly."],
    }


def _corpus() -> BoardBenchCorpus:
    cases = [
        _case(category_index * 4 + offset, category)
        for category_index, category in enumerate(BOARD_CATEGORIES)
        for offset in range(4)
    ]
    return BoardBenchCorpus.from_dict(
        {
            "schema": "pcbdraft-boardbench-corpus",
            "version": 1,
            "corpus_id": "private-boardbench-v1",
            "corpus_version": 1,
            "license": "CC0-1.0",
            "methodology": "Sealed holdout fixture for runner tests.",
            "cohort": "sealed_holdout_baseline",
            "cases": cases,
        }
    )


def _environment() -> RunnerEnvironment:
    return RunnerEnvironment(
        pcbdraft_commit="abcdef1234567890",
        dirty_state_sha256=HASH_A,
        provider="default-provider",
        model="default-model",
        configuration_sha256=HASH_B,
        kicad_version="10.0.5",
        python_version="3.13.6",
        platform="Linux-x86_64-test",
        tool_registry_sha256=HASH_C,
        tool_call_budget=500,
    )


def _argument(argv: Sequence[str], name: str) -> Path:
    index = argv.index(name)
    return Path(argv[index + 1])


class _FakeWorkerProcess:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls: list[tuple[str, ...]] = []
        self.requests: list[dict[str, object]] = []
        self.running_statuses: list[str] = []
        self.timeouts: list[float] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        timeout: float,
        max_output_bytes: int,
        stdin_data: bytes | None = None,
    ) -> CommandResult:
        del cwd, max_output_bytes, stdin_data
        call = tuple(argv)
        self.calls.append(call)
        self.timeouts.append(timeout)
        request_path = _argument(call, "--request")
        repository = _argument(call, "--repository")
        repository_config = _argument(call, "--repository-config")
        trace = _argument(call, "--trace")
        usage = _argument(call, "--usage")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.requests.append(request)
        run_root = request_path.parent.parent
        self.running_statuses.append(load_run(run_root / "run.json").status)
        self.assert_request_boundary(request, call)
        if self.fail_first and len(self.calls) == 1:
            return CommandResult(
                argv=call,
                returncode=7,
                stdout=b"",
                stderr=b"api_key=super-secret-test-key",
                duration_seconds=0.1,
            )
        project = repository / "projects" / str(request["run_id"])
        project.mkdir(parents=True, mode=0o700)
        atomic_write_json(project / "project.json", {"id": request["run_id"]})
        atomic_write_json(repository_config, {"repository": str(repository)})
        trace.parent.mkdir(parents=True, exist_ok=True)
        session_id = f"session-{request['run_id']}"
        model_request = {
            "seq": 1,
            "event": "model_request",
            "data": {
                "session_id": session_id,
                "request": {
                    "messages": [{"role": "user", "content": request["prompt"]}]
                },
            },
        }
        complete = {
            "seq": 2,
            "event": "turn_complete",
            "data": {
                "session_id": session_id,
                "assistant_response": f"Completed {request['run_id']}.",
            },
        }
        atomic_write_json(
            usage,
            {
                "estimated_cost_usd": 0.125,
                "cost_status": "estimated",
                "cost_source": "pcbdraft_pricing_catalog",
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 1500,
                "api_calls": 2,
                "model": "default-model",
                "provider": "default-provider",
                "session_id": session_id,
                "completed": True,
                "failed": False,
                "service_tier": None,
            },
        )
        if len(self.calls) == 1:
            atomic_write_text(
                trace.with_name(f"{trace.name}.1"), json.dumps(model_request) + "\n"
            )
            atomic_write_text(trace, json.dumps(complete) + "\n")
        else:
            atomic_write_text(
                trace,
                json.dumps(model_request) + "\n" + json.dumps(complete) + "\n",
            )
        return CommandResult(
            argv=call,
            returncode=0,
            stdout=(f"{run_root} api_key=super-secret-test-key").encode(),
            stderr=b"",
            duration_seconds=0.2,
        )

    @staticmethod
    def assert_request_boundary(
        request: dict[str, object], argv: tuple[str, ...]
    ) -> None:
        if set(request) != {"schema", "version", "run_id", "prompt"}:
            raise AssertionError("worker request was not prompt-only")
        rendered = " ".join(argv).casefold()
        for forbidden in ("circuitplan", "circuit_plan", "evaluator", "reference"):
            if forbidden in rendered:
                raise AssertionError(f"forbidden worker input: {forbidden}")


class BoardBenchRunnerTests(unittest.TestCase):
    """All executions are explicitly labeled non-baseline fixtures."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.corpus = _corpus()
        self.environment = _environment()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create(self, name: str = "campaign-v1") -> Path:
        return create_campaign(
            self.root / "campaigns",
            self.corpus,
            campaign_id=name,
            evaluator_version="boardbench-evaluator-v1",
            environment=self.environment,
            source_root=self.root,
            created_at=NOW,
            wall_timeout_seconds=30.0,
        )

    def test_trace_summary_rejects_boolean_sequence_numbers(self) -> None:
        trace_root = self.root / "trace"
        trace_root.mkdir()
        atomic_write_text(
            trace_root / "agent-trace.jsonl",
            '{"seq":true,"event":"turn_complete","data":{}}\n',
        )

        summary = _trace_summary(trace_root)

        self.assertTrue(summary.gap_detected)
        self.assertIsNone(summary.final_response)

    @staticmethod
    def _git(repository: Path, *arguments: str) -> bytes:
        result = run_command(
            ["git", *arguments],
            cwd=repository,
            timeout=10.0,
            max_output_bytes=1024 * 1024,
        )
        if result.returncode != 0 or result.timed_out or result.output_limited:
            raise AssertionError(f"git fixture command failed: {arguments[0]}")
        return result.stdout

    def test_capture_environment_hashes_tracked_and_untracked_contents(self) -> None:
        repository = self.root / "fingerprint-repository"
        repository.mkdir()
        self._git(repository, "init", "--quiet")
        self._git(repository, "config", "user.name", "BoardBench Test")
        self._git(repository, "config", "user.email", "boardbench@example.invalid")
        tracked = repository / "tracked.txt"
        tracked.write_text("committed\n", encoding="utf-8")
        self._git(repository, "add", "tracked.txt")
        self._git(repository, "commit", "--quiet", "-m", "fixture")

        untracked = repository / "untracked.txt"
        tracked.write_text("tracked-a\n", encoding="utf-8")
        untracked.write_text("untrack-a\n", encoding="utf-8")
        fake_bin = repository / "fake-bin"
        fake_bin.mkdir()
        fake_kicad = fake_bin / "kicad-cli"
        fake_kicad.write_text("#!/bin/sh\nprintf '10.0.5\\n'\n", encoding="utf-8")
        fake_kicad.chmod(0o700)

        def snapshot() -> RunnerEnvironment:
            status = self._git(
                repository,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
            with (
                mock.patch("pcbdraft.model.settings.write_runtime_config"),
                mock.patch(
                    "pcbdraft.services.provider_connection.activate_provider_runtime"
                ),
                mock.patch(
                    "pcbdraft.services.provider_connection.connection_status",
                    return_value=SimpleNamespace(
                        configured=True,
                        provider="default-provider",
                        model="default-model",
                    ),
                ),
                mock.patch(
                    "pcbdraft.model.configuration.load_config_readonly",
                    return_value={
                        "model": {"default": "default-model", "max_tokens": 8192},
                        "agent": {"max_turns": 73},
                    },
                ),
                mock.patch.dict(
                    os.environ,
                    {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
                ),
            ):
                captured = capture_environment(repository)
            self.assertEqual(
                self._git(
                    repository,
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ),
                status,
            )
            self.assertEqual(captured.tool_call_budget, 500)
            return captured

        first = snapshot()
        tracked_status = self._git(
            repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        tracked.write_text("tracked-b\n", encoding="utf-8")
        self.assertEqual(
            self._git(
                repository,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            tracked_status,
        )
        second = snapshot()
        self.assertNotEqual(first.dirty_state_sha256, second.dirty_state_sha256)

        untracked_status = self._git(
            repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        untracked.write_text("untrack-b\n", encoding="utf-8")
        self.assertEqual(
            self._git(
                repository,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            untracked_status,
        )
        third = snapshot()
        self.assertNotEqual(second.dirty_state_sha256, third.dirty_state_sha256)

        if os.name != "nt":
            odd_name = repository / "odd\nname.txt"
            odd_name.write_text("nul-safe-name\n", encoding="utf-8")
            link = repository / "untracked-link"
            link.symlink_to("untracked.txt")
            fourth = snapshot()
            link.unlink()
            link.symlink_to("tracked.txt")
            fifth = snapshot()
            self.assertNotEqual(fourth.dirty_state_sha256, fifth.dirty_state_sha256)

    def test_configuration_fingerprint_redacts_secrets_but_keeps_token_limits(
        self,
    ) -> None:
        raw: dict[str, Any] = {
            "model": {
                "api_key": "first-model-secret",
                "max_tokens": 8192,
                "tokenizer": "stable-tokenizer",
                "headers": {"X-Custom-Auth": "first-header-secret"},
            },
            "agent": {"max_turns": 73, "proactive_prune_tokens": 4096},
        }
        sanitized = _secret_free_configuration(raw)
        rendered = json.dumps(sanitized, sort_keys=True)
        self.assertNotIn("first-model-secret", rendered)
        self.assertNotIn("first-header-secret", rendered)
        self.assertIn('"max_tokens": 8192', rendered)
        self.assertIn('"tokenizer": "stable-tokenizer"', rendered)
        self.assertIn('"proactive_prune_tokens": 4096', rendered)
        self.assertEqual(_effective_tool_call_budget(raw), 500)

        rotated_secret = {
            **raw,
            "model": {
                **raw["model"],
                "api_key": "second-model-secret",
                "headers": {"X-Custom-Auth": "second-header-secret"},
            },
        }
        self.assertEqual(
            _canonical_hash(sanitized),
            _canonical_hash(_secret_free_configuration(rotated_secret)),
        )
        changed_limit = {
            **raw,
            "model": {**raw["model"], "max_tokens": 4096},
        }
        self.assertNotEqual(
            _canonical_hash(sanitized),
            _canonical_hash(_secret_free_configuration(changed_limit)),
        )
        self.assertEqual(
            _effective_tool_call_budget({"agent": {"max_turns": True}}), 500
        )

    def test_plan_freezes_exactly_sixty_ids_and_effective_pcbdraft_budget(self) -> None:
        self.assertEqual(DEFAULT_WALL_TIMEOUT_SECONDS, 3600.0)
        with self.assertRaisesRegex(ValidationError, "fixed at 500"):
            plan_campaign(
                self.corpus,
                campaign_id="invalid-budget",
                environment=replace(self.environment, tool_call_budget=73),
                evaluator_version="boardbench-evaluator-v1",
                created_at=NOW,
            )
        campaign = plan_campaign(
            self.corpus,
            campaign_id="campaign-v1",
            environment=self.environment,
            evaluator_version="boardbench-evaluator-v1",
            created_at=NOW,
        )
        self.assertEqual(len(campaign.runs), 60)
        self.assertEqual(len({run.run_id for run in campaign.runs}), 60)
        self.assertEqual(campaign.tool_call_budget, 500)
        self.assertEqual(campaign.wall_timeout_seconds, DEFAULT_WALL_TIMEOUT_SECONDS)
        self.assertEqual(
            {(run.case_id, run.repetition) for run in campaign.runs},
            {
                (case.id, repetition)
                for case in self.corpus.cases
                for repetition in range(1, 4)
            },
        )
        for invalid in (
            0.0,
            86_401.0,
            float("nan"),
            float("inf"),
            float("-inf"),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValidationError, "campaign wall timeout"),
            ):
                plan_campaign(
                    self.corpus,
                    campaign_id="invalid-timeout",
                    environment=self.environment,
                    evaluator_version="boardbench-evaluator-v1",
                    created_at=NOW,
                    wall_timeout_seconds=invalid,
                )

    def test_runs_fresh_prompt_only_processes_and_resume_skips_terminal(self) -> None:
        campaign_root = self._create()
        campaign = load_campaign(campaign_root / "campaign.json")
        planned = initialize_run_receipts(campaign_root, campaign, self.corpus)
        self.assertEqual({run.status for run in planned}, {"planned"})
        fake = _FakeWorkerProcess()
        results = run_campaign(
            campaign_root,
            self.corpus,
            source_root=self.root,
            environment_probe=lambda: self.environment,
            command_runner=fake,
            fixture_label=FIXTURE_LABEL,
        )
        self.assertEqual(len(fake.calls), 60)
        self.assertEqual(set(fake.timeouts), {30.0})
        self.assertEqual({run.status for run in results}, {"completed"})
        self.assertEqual(set(fake.running_statuses), {"running"})
        self.assertEqual(
            len({_argument(call, "--repository") for call in fake.calls}), 60
        )
        self.assertEqual(len({_argument(call, "--trace") for call in fake.calls}), 60)
        self.assertEqual(len({_argument(call, "--usage") for call in fake.calls}), 60)
        self.assertEqual(
            {_argument(call, "--pcb-tool-call-limit") for call in fake.calls},
            {Path("500")},
        )
        self.assertEqual(
            [request["prompt"] for request in fake.requests],
            [case.prompt for case in self.corpus.cases for _ in range(3)],
        )
        sessions: set[str] = set()
        for call, request in zip(fake.calls, fake.requests, strict=True):
            trace_path = _argument(call, "--trace")
            oldest = trace_path.with_name(f"{trace_path.name}.1")
            first_member = oldest if oldest.exists() else trace_path
            first_event = json.loads(
                first_member.read_text(encoding="utf-8").splitlines()[0]
            )
            first_request = first_event["data"]["request"]
            self.assertEqual(
                first_request["messages"],
                [{"role": "user", "content": request["prompt"]}],
            )
            sessions.add(first_event["data"]["session_id"])
        self.assertEqual(len(sessions), 60)
        first_root = campaign_root / "runs" / campaign.runs[0].run_id
        first_v2 = load_run_v2(first_root / "run.json")
        self.assertEqual(first_v2.to_dict()["schema"], RUN_V2_SCHEMA)
        self.assertEqual(first_v2.process_status.value, "exited")
        self.assertEqual(first_v2.task_outcome.value, "incomplete")
        self.assertEqual(first_v2.termination_reason, "agent_returned_before_gate")
        first_inventory = {item.path for item in results[0].inventory}
        self.assertIn("trace/agent-trace.jsonl", first_inventory)
        self.assertIn("trace/agent-trace.jsonl.1", first_inventory)
        self.assertIn("usage.json", first_inventory)
        execution = json.loads(
            (first_root / "artifacts" / "execution.json").read_text(encoding="utf-8")
        )
        self.assertEqual(execution["fixture_label"], FIXTURE_LABEL)
        trace_inventory = json.loads(
            (first_root / "artifacts" / "trace-inventory.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(trace_inventory["gap_detected"])
        stdout = (first_root / "artifacts" / "stdout.txt").read_text(encoding="utf-8")
        self.assertNotIn(str(first_root), stdout)
        self.assertNotIn("super-secret-test-key", stdout)
        run_campaign(
            campaign_root,
            self.corpus,
            source_root=self.root,
            environment_probe=lambda: self.environment,
            command_runner=fake,
            fixture_label=FIXTURE_LABEL,
        )
        self.assertEqual(len(fake.calls), 60)
        atomic_write_text(first_root / "artifacts" / "stdout.txt", "tampered\n")
        with self.assertRaisesRegex(ValidationError, "immutable receipt"):
            run_campaign(
                campaign_root,
                self.corpus,
                source_root=self.root,
                environment_probe=lambda: self.environment,
                command_runner=fake,
                fixture_label=FIXTURE_LABEL,
            )

    def test_selected_run_ids_execute_a_bounded_subset_without_changing_plan(
        self,
    ) -> None:
        campaign_root = self._create("campaign-selected")
        campaign = load_campaign(campaign_root / "campaign.json")
        # Deliberately request reverse order; execution remains frozen-plan order.
        selected = (campaign.runs[7].run_id, campaign.runs[2].run_id)
        fake = _FakeWorkerProcess()

        with mock.patch("pcbdraft.verification.boardbench.store_run") as legacy_store:
            results = run_campaign(
                campaign_root,
                self.corpus,
                source_root=self.root,
                environment_probe=lambda: self.environment,
                command_runner=fake,
                fixture_label=FIXTURE_LABEL,
                run_ids=selected,
            )
        legacy_store.assert_not_called()

        self.assertEqual(60, len(results))
        self.assertEqual(2, len(fake.calls))
        self.assertEqual(
            [self.corpus.cases[0].prompt, self.corpus.cases[2].prompt],
            [request["prompt"] for request in fake.requests],
        )
        self.assertEqual(60, len(load_campaign(campaign_root / "campaign.json").runs))
        statuses = {run.run_id: run.status for run in results}
        self.assertEqual(
            {run_id: "completed" for run_id in selected},
            {run_id: statuses[run_id] for run_id in selected},
        )
        self.assertEqual(
            {"planned"},
            {status for run_id, status in statuses.items() if run_id not in selected},
        )
        stored_statuses = {
            plan.run_id: load_run(
                campaign_root / "runs" / plan.run_id / "run.json"
            ).status
            for plan in campaign.runs
        }
        self.assertEqual(statuses, stored_statuses)

        run_campaign(
            campaign_root,
            self.corpus,
            source_root=self.root,
            environment_probe=lambda: self.environment,
            command_runner=fake,
            fixture_label=FIXTURE_LABEL,
            run_ids=selected,
        )
        self.assertEqual(2, len(fake.calls))

        # A selector must not make an unselected terminal denominator entry
        # invisible to immutable-evidence verification.  Detect the drift before
        # executing the newly selected planned run.
        tampered_run_id = selected[1]
        atomic_write_text(
            campaign_root / "runs" / tampered_run_id / "artifacts" / "stdout.txt",
            "tampered\n",
        )
        with self.assertRaisesRegex(ValidationError, "immutable receipt"):
            run_campaign(
                campaign_root,
                self.corpus,
                source_root=self.root,
                environment_probe=lambda: self.environment,
                command_runner=fake,
                fixture_label=FIXTURE_LABEL,
                run_ids=(campaign.runs[10].run_id,),
            )
        self.assertEqual(2, len(fake.calls))

    def test_selected_run_ids_reject_duplicates_and_unknown_ids_before_execution(
        self,
    ) -> None:
        campaign_root = self._create("campaign-invalid-selection")
        campaign = load_campaign(campaign_root / "campaign.json")
        fake = _FakeWorkerProcess()
        run_id = campaign.runs[0].run_id

        with self.assertRaisesRegex(ValidationError, "duplicate ids"):
            run_campaign(
                campaign_root,
                self.corpus,
                source_root=self.root,
                environment_probe=lambda: self.environment,
                command_runner=fake,
                fixture_label=FIXTURE_LABEL,
                run_ids=(run_id, run_id),
            )
        with self.assertRaisesRegex(ValidationError, "immutable campaign plan"):
            run_campaign(
                campaign_root,
                self.corpus,
                source_root=self.root,
                environment_probe=lambda: self.environment,
                command_runner=fake,
                fixture_label=FIXTURE_LABEL,
                run_ids=("not-a-planned-run",),
            )
        self.assertEqual([], fake.calls)
        self.assertEqual([], list((campaign_root / "runs").iterdir()))

    def test_worker_failure_is_terminal_and_retained(self) -> None:
        campaign_root = self._create("campaign-failure")
        fake = _FakeWorkerProcess(fail_first=True)
        results = run_campaign(
            campaign_root,
            self.corpus,
            source_root=self.root,
            environment_probe=lambda: self.environment,
            command_runner=fake,
            fixture_label=FIXTURE_LABEL,
        )
        failed = results[0]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.termination_reason, "crashed")
        self.assertEqual(
            load_run_v2(
                campaign_root / "runs" / failed.run_id / "run.json"
            ).process_status.value,
            "crashed",
        )
        paths = {item.path for item in failed.inventory}
        self.assertIn("failure.json", paths)
        stderr = (
            campaign_root / "runs" / failed.run_id / "artifacts" / "stderr.txt"
        ).read_text(encoding="utf-8")
        self.assertNotIn("super-secret-test-key", stderr)

    def test_resume_finalizes_running_receipt_without_restarting_it(self) -> None:
        campaign_root = self._create("campaign-interrupted")
        campaign = load_campaign(campaign_root / "campaign.json")
        planned = initialize_run_receipts(campaign_root, campaign, self.corpus)
        first = planned[0]
        with self.assertRaisesRegex(ValidationError, "immutable identity"):
            store_run(
                campaign_root / "runs" / first.run_id / "run.json",
                replace(
                    first,
                    campaign_id="different-campaign",
                    status="running",
                    started_at=NOW,
                ),
            )
        self.assertEqual(
            load_run_v2(campaign_root / "runs" / first.run_id / "run.json").run_state,
            "planned",
        )
        store_run(
            campaign_root / "runs" / first.run_id / "run.json",
            replace(first, status="running", started_at=NOW),
        )
        fake = _FakeWorkerProcess()
        results = run_campaign(
            campaign_root,
            self.corpus,
            source_root=self.root,
            environment_probe=lambda: self.environment,
            command_runner=fake,
            fixture_label=FIXTURE_LABEL,
        )
        self.assertEqual(results[0].status, "interrupted")
        self.assertEqual(results[0].termination_reason, "cancelled")
        self.assertEqual(len(fake.calls), 59)

    def test_configuration_drift_terminates_without_retry(self) -> None:
        campaign_root = self._create("campaign-drift")
        fake = _FakeWorkerProcess()
        drifted = replace(self.environment, model="changed-model")
        probes = 0

        def probe() -> RunnerEnvironment:
            nonlocal probes
            probes += 1
            return self.environment if probes == 1 else drifted

        results = run_campaign(
            campaign_root,
            self.corpus,
            source_root=self.root,
            environment_probe=probe,
            command_runner=fake,
            fixture_label=FIXTURE_LABEL,
        )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual({run.status for run in results}, {"configuration_drift"})
        self.assertTrue(
            all(run.termination_reason == "configuration_drift" for run in results)
        )


if __name__ == "__main__":
    unittest.main()
