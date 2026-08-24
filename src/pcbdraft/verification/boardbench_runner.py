"""Plan and execute isolated, immutable BoardBench model runs.

The runner sees the frozen campaign corpus only long enough to select each case's
natural-language prompt.  Every model invocation is a fresh worker process;
reference answers and evaluator data never cross that boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.core import process
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import (
    atomic_write_bytes,
    atomic_write_json,
    make_directory,
    privatize_tree,
    read_bytes_limited,
    read_text_limited,
)
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.verification.boardbench import (
    BoardBenchCampaign,
    BoardBenchCase,
    BoardBenchCorpus,
    BoardBenchRun,
    CampaignRunPlan,
    allocate_campaign_directory,
    artifact_sha256,
    build_inventory,
    load_campaign,
    load_run,
    write_artifact,
)
from pcbdraft.verification.boardbench_v2 import (
    load_run_v2,
    planned_run_v2,
    start_run_v2,
    store_run_v2,
    terminal_run_v2,
    validate_campaign_denominator,
)

DEFAULT_WALL_TIMEOUT_SECONDS = 3600.0
DEFAULT_TOOL_CALL_BUDGET = 500
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
TRACE_MEMBER_LIMIT = 32 * 1024 * 1024
GIT_OUTPUT_LIMIT = 32 * 1024 * 1024
UNTRACKED_FILE_LIMIT = 32 * 1024 * 1024
UNTRACKED_TOTAL_LIMIT = 128 * 1024 * 1024
UNTRACKED_FILE_COUNT_LIMIT = 100_000
MAX_FINAL_RESPONSE_BYTES = 64 * 1024
RUNS_DIRECTORY = "runs"
RUN_RECEIPT_NAME = "run.json"
CAMPAIGN_NAME = "campaign.json"
ARTIFACTS_DIRECTORY = "artifacts"
FIXTURE_LABEL = "non_baseline_fixture"

_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "key",
        "key_cmd",
        "password",
        "secret",
        "token",
    }
)
_SENSITIVE_CONFIG_SUFFIXES = (
    "api_key",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "credential",
    "authorization",
    "cookie",
)
_SECRET_CONTAINER_KEYS = frozenset(
    {"default_headers", "extra_headers", "headers", "secrets"}
)
_TRACE_MEMBER = re.compile(r"agent-trace\.jsonl(?:\.([1-9][0-9]*))?")


@dataclass(frozen=True)
class RunnerEnvironment:
    """Secret-free fingerprint bound into one BoardBench campaign."""

    pcbdraft_commit: str
    dirty_state_sha256: str
    provider: str
    model: str
    configuration_sha256: str
    kicad_version: str
    python_version: str
    platform: str
    tool_registry_sha256: str
    tool_call_budget: int

    def matches(self, campaign: BoardBenchCampaign) -> bool:
        return self == RunnerEnvironment.from_campaign(campaign)

    @classmethod
    def from_campaign(cls, campaign: BoardBenchCampaign) -> RunnerEnvironment:
        return cls(
            pcbdraft_commit=campaign.pcbdraft_commit,
            dirty_state_sha256=campaign.dirty_state_sha256,
            provider=campaign.provider,
            model=campaign.model,
            configuration_sha256=campaign.configuration_sha256,
            kicad_version=campaign.kicad_version,
            python_version=campaign.python_version,
            platform=campaign.platform,
            tool_registry_sha256=campaign.tool_registry_sha256,
            tool_call_budget=campaign.tool_call_budget,
        )


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        timeout: float,
        max_output_bytes: int,
        stdin_data: bytes | None = None,
    ) -> process.CommandResult: ...


class _DigestWriter(Protocol):
    def update(self, data: bytes) -> None: ...


def _reject_symlink_components(path: Path, label: str) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValidationError(f"BoardBench {label} path contains a symbolic link")


def _canonical_hash(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("BoardBench configuration is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _normalized_config_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")


def _sensitive_config_key(key: str) -> bool:
    normalized = _normalized_config_key(key)
    if normalized in _SENSITIVE_CONFIG_KEYS or normalized.endswith("_token"):
        return True
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SENSITIVE_CONFIG_SUFFIXES
    )


def _secret_free_configuration(
    value: object, *, key: str = "", secret_container: bool = False
) -> object:
    normalized = _normalized_config_key(key)
    if secret_container or _sensitive_config_key(key):
        return "<redacted>"
    if normalized in _SECRET_CONTAINER_KEYS and not isinstance(value, Mapping):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _secret_free_configuration(
                item,
                key=str(item_key),
                secret_container=normalized in _SECRET_CONTAINER_KEYS,
            )
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_secret_free_configuration(item, key=key) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_user_text(str(value))


def _effective_tool_call_budget(configuration: Mapping[str, object]) -> int:
    """Return the fixed PCB-tool budget, never Hermes' model-turn limit."""

    del configuration
    return DEFAULT_TOOL_CALL_BUDGET


def _update_digest_frame(digest: _DigestWriter, label: bytes, payload: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _untracked_content_digest(root: Path) -> bytes:
    listing = _checked_command(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--"],
        cwd=root,
    )
    raw_paths = listing.split(b"\0")
    if raw_paths and raw_paths[-1] == b"":
        raw_paths.pop()
    if len(raw_paths) > UNTRACKED_FILE_COUNT_LIMIT:
        raise PCBDraftError("BoardBench working tree has too many untracked files")

    digest = hashlib.sha256()
    total = 0
    for raw_path in raw_paths:
        if not raw_path:
            raise PCBDraftError("BoardBench untracked path listing is malformed")
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise PCBDraftError("BoardBench untracked path is unsafe")
        candidate = root / relative
        try:
            before = candidate.lstat()
        except OSError as exc:
            raise PCBDraftError(
                "BoardBench untracked working-tree member is unavailable"
            ) from exc

        if stat.S_ISREG(before.st_mode):
            if before.st_size > UNTRACKED_FILE_LIMIT:
                raise PCBDraftError(
                    "BoardBench untracked file exceeds the fingerprint size limit"
                )
            total += before.st_size
            if total > UNTRACKED_TOTAL_LIMIT:
                raise PCBDraftError(
                    "BoardBench untracked files exceed the fingerprint size limit"
                )
            content = read_bytes_limited(candidate, UNTRACKED_FILE_LIMIT)
            try:
                after = candidate.lstat()
            except OSError as exc:
                raise PCBDraftError(
                    "BoardBench untracked working-tree member changed while hashing"
                ) from exc
            if not stat.S_ISREG(after.st_mode) or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise PCBDraftError(
                    "BoardBench untracked working-tree member changed while hashing"
                )
            kind_label = b"regular"
            payload = content
        elif stat.S_ISLNK(before.st_mode):
            try:
                target = os.fsencode(os.readlink(candidate))
                after = candidate.lstat()
            except OSError as exc:
                raise PCBDraftError(
                    "BoardBench untracked symbolic link changed while hashing"
                ) from exc
            if len(target) > UNTRACKED_FILE_LIMIT:
                raise PCBDraftError(
                    "BoardBench untracked symbolic link exceeds the size limit"
                )
            total += len(target)
            if total > UNTRACKED_TOTAL_LIMIT:
                raise PCBDraftError(
                    "BoardBench untracked files exceed the fingerprint size limit"
                )
            if not stat.S_ISLNK(after.st_mode) or (
                before.st_dev,
                before.st_ino,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                raise PCBDraftError(
                    "BoardBench untracked symbolic link changed while hashing"
                )
            kind_label = b"symlink"
            payload = target
        else:
            raise PCBDraftError(
                "BoardBench cannot fingerprint an untracked special file"
            )
        _update_digest_frame(digest, b"path", raw_path)
        _update_digest_frame(digest, b"kind", kind_label)
        _update_digest_frame(digest, b"content", payload)
    return digest.digest()


def _dirty_state_sha256(root: Path) -> str:
    status = _checked_command(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
    )
    tracked_diff = _checked_command(
        ["git", "diff", "--no-ext-diff", "--binary", "HEAD"], cwd=root
    )
    digest = hashlib.sha256()
    _update_digest_frame(digest, b"status", status)
    _update_digest_frame(digest, b"tracked-diff", tracked_diff)
    _update_digest_frame(digest, b"untracked-content", _untracked_content_digest(root))
    return digest.hexdigest()


def _checked_command(
    argv: Sequence[str], *, cwd: Path, output_limit: int = GIT_OUTPUT_LIMIT
) -> bytes:
    result = process.run_command(
        argv,
        cwd=cwd,
        timeout=30.0,
        max_output_bytes=output_limit,
    )
    if result.returncode != 0 or result.timed_out or result.output_limited:
        raise PCBDraftError(
            f"BoardBench environment probe failed: {Path(argv[0]).name}"
        )
    return result.stdout


def capture_environment(source_root: str | Path | None = None) -> RunnerEnvironment:
    """Capture the default model and executable environment without secrets."""

    raw_root = (
        Path(source_root).expanduser()
        if source_root is not None
        else Path(__file__).resolve().parents[3]
    )
    _reject_symlink_components(raw_root, "source")
    root = raw_root.resolve()
    from pcbdraft.model.hermes_config import write_hermes_config
    from pcbdraft.services.provider_connection import (
        activate_provider_runtime,
        connection_status,
    )

    activate_provider_runtime()
    write_hermes_config()
    status = connection_status(verify=False)
    if not status.configured or not status.provider or not status.model:
        raise ValidationError("BoardBench requires one configured default model")
    from hermes_cli.config import load_config_readonly

    raw_configuration = load_config_readonly()
    configuration = _secret_free_configuration(raw_configuration)
    tool_call_budget = _effective_tool_call_budget(raw_configuration)
    commit = (
        _checked_command(["git", "rev-parse", "HEAD"], cwd=root)
        .decode("ascii", "strict")
        .strip()
    )
    if re.fullmatch(r"[0-9a-f]{7,64}", commit) is None:
        raise PCBDraftError("BoardBench could not identify the PCBDraft commit")
    kicad_version = (
        _checked_command(["kicad-cli", "--version"], cwd=root, output_limit=64 * 1024)
        .decode("utf-8", "replace")
        .splitlines()
    )
    if not kicad_version or not kicad_version[0].strip():
        raise PCBDraftError("BoardBench could not identify the KiCad version")
    return RunnerEnvironment(
        pcbdraft_commit=commit,
        dirty_state_sha256=_dirty_state_sha256(root),
        provider=status.provider,
        model=status.model,
        configuration_sha256=_canonical_hash(configuration),
        kicad_version=kicad_version[0].strip()[:512],
        python_version=platform_module.python_version(),
        platform=(
            f"{platform_module.system()}-{platform_module.machine()}-"
            f"{platform_module.release()}"
        )[:512],
        tool_registry_sha256=DEFAULT_PCB_TOOL_REGISTRY.schema_fingerprint(),
        tool_call_budget=tool_call_budget,
    )


def _run_id(case_id: str, repetition: int) -> str:
    candidate = f"{case_id}-run-{repetition}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:24]
    return f"run-{digest}-{repetition}"


def plan_campaign(
    corpus: BoardBenchCorpus,
    *,
    campaign_id: str,
    environment: RunnerEnvironment,
    evaluator_version: str,
    created_at: str | None = None,
    wall_timeout_seconds: float = DEFAULT_WALL_TIMEOUT_SECONDS,
) -> BoardBenchCampaign:
    """Freeze a deterministic 20-by-3 campaign against one default model."""

    if environment.tool_call_budget != DEFAULT_TOOL_CALL_BUDGET:
        raise ValidationError("BoardBench PCB tool-call budget must be fixed at 500")

    plans = tuple(
        CampaignRunPlan(
            case_id=case.id,
            repetition=repetition,
            run_id=_run_id(case.id, repetition),
        )
        for case in corpus.cases
        for repetition in range(1, 4)
    )
    return BoardBenchCampaign(
        campaign_id=campaign_id,
        cohort=corpus.cohort,
        corpus_id=corpus.corpus_id,
        corpus_sha256=artifact_sha256(corpus),
        created_at=created_at or utc_timestamp(),
        pcbdraft_commit=environment.pcbdraft_commit,
        dirty_state_sha256=environment.dirty_state_sha256,
        provider=environment.provider,
        model=environment.model,
        configuration_sha256=environment.configuration_sha256,
        kicad_version=environment.kicad_version,
        python_version=environment.python_version,
        platform=environment.platform,
        tool_registry_sha256=environment.tool_registry_sha256,
        wall_timeout_seconds=wall_timeout_seconds,
        tool_call_budget=environment.tool_call_budget,
        repetitions=3,
        evaluator_version=evaluator_version,
        runs=plans,
    )


def create_campaign(
    parent: str | Path,
    corpus: BoardBenchCorpus,
    *,
    campaign_id: str,
    evaluator_version: str,
    environment: RunnerEnvironment | None = None,
    source_root: str | Path | None = None,
    created_at: str | None = None,
    wall_timeout_seconds: float = DEFAULT_WALL_TIMEOUT_SECONDS,
) -> Path:
    """Allocate a private campaign and persist its immutable manifest."""

    captured = environment or capture_environment(source_root)
    campaign = plan_campaign(
        corpus,
        campaign_id=campaign_id,
        environment=captured,
        evaluator_version=evaluator_version,
        created_at=created_at,
        wall_timeout_seconds=wall_timeout_seconds,
    )
    root = allocate_campaign_directory(parent, campaign_id)
    write_artifact(root / CAMPAIGN_NAME, campaign)
    make_directory(root / RUNS_DIRECTORY)
    return root


def _validated_cases(
    campaign: BoardBenchCampaign, corpus: BoardBenchCorpus
) -> dict[str, BoardBenchCase]:
    if (
        campaign.corpus_id != corpus.corpus_id
        or campaign.cohort != corpus.cohort
        or campaign.corpus_sha256 != artifact_sha256(corpus)
    ):
        raise ValidationError("BoardBench campaign does not match the frozen corpus")
    cases = {case.id: case for case in corpus.cases}
    if {plan.case_id for plan in campaign.runs} != set(cases):
        raise ValidationError("BoardBench campaign run ids do not match the corpus")
    return cases


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _private_run_root(campaign_root: Path, run_id: str) -> Path:
    target = campaign_root / RUNS_DIRECTORY / run_id
    _reject_symlink_components(target, "run directory")
    if target.exists() and not target.is_dir():
        raise ValidationError("BoardBench run path must be a directory")
    return make_directory(target)


def _planned_run(
    campaign: BoardBenchCampaign,
    plan: CampaignRunPlan,
    case: BoardBenchCase,
) -> BoardBenchRun:
    return BoardBenchRun(
        campaign_id=campaign.campaign_id,
        run_id=plan.run_id,
        case_id=plan.case_id,
        repetition=plan.repetition,
        prompt_sha256=_prompt_hash(case.prompt),
        status="planned",
        started_at=None,
        completed_at=None,
        termination_reason=None,
        final_response=None,
        inventory=(),
    )


def initialize_run_receipts(
    campaign_root: str | Path,
    campaign: BoardBenchCampaign,
    corpus: BoardBenchCorpus,
) -> tuple[BoardBenchRun, ...]:
    """Materialize all 60 planned receipts without changing existing runs."""

    raw_root = Path(campaign_root).expanduser()
    _reject_symlink_components(raw_root, "campaign")
    root = raw_root.resolve()
    cases = _validated_cases(campaign, corpus)
    receipts: list[BoardBenchRun] = []
    for plan in campaign.runs:
        run_root = _private_run_root(root, plan.run_id)
        receipt_path = run_root / RUN_RECEIPT_NAME
        expected = _planned_run(campaign, plan, cases[plan.case_id])
        if receipt_path.exists() or receipt_path.is_symlink():
            current = load_run(receipt_path)
            if (
                current.campaign_id,
                current.run_id,
                current.case_id,
                current.repetition,
                current.prompt_sha256,
            ) != (
                expected.campaign_id,
                expected.run_id,
                expected.case_id,
                expected.repetition,
                expected.prompt_sha256,
            ):
                raise ValidationError("BoardBench run receipt does not match its plan")
            receipts.append(current)
            continue
        if any(run_root.iterdir()):
            raise ValidationError("unrecorded BoardBench run evidence already exists")
        store_run_v2(receipt_path, planned_run_v2(expected, campaign))
        receipts.append(expected)
    return tuple(receipts)


def _sanitize_output(data: bytes, *, replacements: Mapping[str, str]) -> bytes:
    text = data.decode("utf-8", "replace")
    for raw, marker in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if raw:
            text = text.replace(raw, marker)
    return sanitize_user_text(text).encode("utf-8")


def _trace_sort_key(path: Path) -> tuple[int, str]:
    match = _TRACE_MEMBER.fullmatch(path.name)
    if match is None:
        return (0, path.name)
    suffix = int(match.group(1) or 0)
    return (-suffix, path.name)


@dataclass(frozen=True)
class _TraceSummary:
    members: tuple[str, ...]
    gap_detected: bool
    final_response: str | None


def _trace_summary(trace_root: Path) -> _TraceSummary:
    members = tuple(
        sorted(
            (
                path
                for path in trace_root.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and _TRACE_MEMBER.fullmatch(path.name) is not None
            ),
            key=_trace_sort_key,
        )
    )
    sequences: list[int] = []
    final_response: str | None = None
    malformed = False
    for member in members:
        try:
            lines = read_text_limited(member, TRACE_MEMBER_LIMIT).splitlines()
        except PCBDraftError:
            malformed = True
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                malformed = True
                continue
            if (
                not isinstance(event, dict)
                or isinstance(event.get("seq"), bool)
                or not isinstance(event.get("seq"), int)
            ):
                malformed = True
                continue
            sequences.append(int(event["seq"]))
            if event.get("event") == "turn_complete":
                data = event.get("data")
                response = (
                    data.get("assistant_response") if isinstance(data, dict) else None
                )
                if isinstance(response, str) and response.strip():
                    final_response = response
    gap = malformed or not sequences
    if sequences:
        gap = gap or sequences != list(range(sequences[0], sequences[-1] + 1))
        gap = gap or sequences[0] != 1
    return _TraceSummary(
        members=tuple(path.name for path in members),
        gap_detected=gap,
        final_response=final_response,
    )


def _bounded_response(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = sanitize_user_text(value).strip()
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= MAX_FINAL_RESPONSE_BYTES:
        return cleaned or None
    truncated = encoded[:MAX_FINAL_RESPONSE_BYTES].decode("utf-8", "ignore")
    return truncated.rstrip() or None


def _failure_artifact(
    artifacts: Path,
    *,
    status: str,
    reason: str,
    result: process.CommandResult | None,
) -> None:
    target = artifacts / "failure.json"
    if target.exists() or target.is_symlink():
        return
    atomic_write_json(
        target,
        {
            "schema": "pcbdraft-boardbench-worker-failure",
            "version": 1,
            "status": status,
            "reason": reason,
            "returncode": result.returncode if result is not None else None,
            "timed_out": result.timed_out if result is not None else False,
            "output_limited": result.output_limited if result is not None else False,
        },
        mode=0o600,
    )


def _retained_project_count(repository: Path) -> int:
    projects = repository / "projects"
    if projects.is_symlink() or not projects.is_dir():
        return 0
    count = 0
    for candidate in projects.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        project_record = candidate / "project.json"
        if project_record.is_file() and not project_record.is_symlink():
            count += 1
    return count


def _terminal_receipt(
    campaign: BoardBenchCampaign,
    receipt: BoardBenchRun,
    artifacts: Path,
    *,
    status: str,
    reason: str,
    final_response: str | None,
    result: process.CommandResult | None = None,
) -> BoardBenchRun:
    privatize_tree(artifacts)
    terminal = terminal_run_v2(
        receipt,
        campaign,
        artifacts,
        completed_at=utc_timestamp(),
        fallback_status=status,
        fallback_reason=reason,
        final_response=final_response,
        duration_seconds=result.duration_seconds if result is not None else None,
        worker_exit_code=result.returncode if result is not None else None,
        inventory=build_inventory(artifacts),
    )
    store_run_v2(artifacts.parent / RUN_RECEIPT_NAME, terminal)
    return terminal.to_legacy()


def _recover_running(
    campaign: BoardBenchCampaign, receipt: BoardBenchRun, run_root: Path
) -> BoardBenchRun:
    # Historical v1 receipts are compatibility-read-only.  Reject before
    # creating recovery evidence anywhere in their run directory.
    load_run_v2(run_root / RUN_RECEIPT_NAME)
    artifacts = make_directory(run_root / ARTIFACTS_DIRECTORY)
    _failure_artifact(
        artifacts,
        status="interrupted",
        reason="prior_worker_interrupted",
        result=None,
    )
    return _terminal_receipt(
        campaign,
        receipt,
        artifacts,
        status="interrupted",
        reason="prior_worker_interrupted",
        final_response=None,
    )


def _verify_terminal_artifacts(receipt: BoardBenchRun, run_root: Path) -> None:
    artifacts = run_root / ARTIFACTS_DIRECTORY
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise ValidationError("terminal BoardBench run artifacts are unavailable")
    if build_inventory(artifacts) != receipt.inventory:
        raise ValidationError(
            "terminal BoardBench run artifacts do not match their immutable receipt"
        )


def _execute_planned(
    campaign: BoardBenchCampaign,
    case: BoardBenchCase,
    receipt: BoardBenchRun,
    run_root: Path,
    *,
    python_executable: str,
    source_root: Path,
    environment_probe: Callable[[], RunnerEnvironment],
    command_runner: CommandRunner,
    max_output_bytes: int,
    fixture_label: str | None,
) -> BoardBenchRun:
    running = BoardBenchRun(
        campaign_id=receipt.campaign_id,
        run_id=receipt.run_id,
        case_id=receipt.case_id,
        repetition=receipt.repetition,
        prompt_sha256=receipt.prompt_sha256,
        status="running",
        started_at=utc_timestamp(),
        completed_at=None,
        termination_reason=None,
        final_response=None,
        inventory=(),
    )
    current_v2 = load_run_v2(run_root / RUN_RECEIPT_NAME)
    store_run_v2(
        run_root / RUN_RECEIPT_NAME,
        start_run_v2(current_v2, running.started_at or utc_timestamp()),
    )
    artifacts = make_directory(run_root / ARTIFACTS_DIRECTORY)
    request_path = artifacts / "request.json"
    atomic_write_json(
        request_path,
        {
            "schema": "pcbdraft-boardbench-worker-request",
            "version": 1,
            "run_id": receipt.run_id,
            "prompt": case.prompt,
        },
        mode=0o600,
    )
    trace_root = make_directory(artifacts / "trace")
    trace_path = trace_root / "agent-trace.jsonl"
    usage_path = artifacts / "usage.json"
    repository = artifacts / "repository"
    repository_config = artifacts / "repository-config.json"
    expected = RunnerEnvironment.from_campaign(campaign)
    try:
        current = environment_probe()
    except PCBDraftError:
        _failure_artifact(
            artifacts,
            status="failed",
            reason="environment_probe_failed",
            result=None,
        )
        return _terminal_receipt(
            campaign,
            running,
            artifacts,
            status="failed",
            reason="environment_probe_failed",
            final_response=None,
        )
    if current != expected:
        _failure_artifact(
            artifacts,
            status="configuration_drift",
            reason="configuration_drift",
            result=None,
        )
        return _terminal_receipt(
            campaign,
            running,
            artifacts,
            status="configuration_drift",
            reason="configuration_drift",
            final_response=None,
        )

    argv = [
        python_executable,
        "-m",
        "pcbdraft.interfaces.boardbench_worker",
        "--request",
        str(request_path),
        "--repository",
        str(repository),
        "--repository-config",
        str(repository_config),
        "--trace",
        str(trace_path),
        "--usage",
        str(usage_path),
        "--pcb-tool-call-limit",
        str(campaign.tool_call_budget),
    ]
    result: process.CommandResult | None = None
    interrupted = False
    command_error = False
    try:
        result = command_runner(
            argv,
            cwd=source_root,
            timeout=campaign.wall_timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
    except KeyboardInterrupt:
        interrupted = True
    except PCBDraftError:
        command_error = True

    replacements = {
        str(run_root): "<run>",
        str(source_root): "<source>",
        str(Path.home()): "<home>",
    }
    atomic_write_bytes(
        artifacts / "stdout.txt",
        _sanitize_output(
            result.stdout if result is not None else b"", replacements=replacements
        ),
        mode=0o600,
    )
    atomic_write_bytes(
        artifacts / "stderr.txt",
        _sanitize_output(
            result.stderr if result is not None else b"", replacements=replacements
        ),
        mode=0o600,
    )
    trace = _trace_summary(trace_root)
    atomic_write_json(
        artifacts / "trace-inventory.json",
        {
            "schema": "pcbdraft-boardbench-trace-inventory",
            "version": 1,
            "members": list(trace.members),
            "gap_detected": trace.gap_detected,
        },
        mode=0o600,
    )
    atomic_write_json(
        artifacts / "execution.json",
        {
            "schema": "pcbdraft-boardbench-worker-execution",
            "version": 1,
            "argv": process.redact_argv(
                argv,
                {
                    str(run_root): "<run>",
                    str(source_root): "<source>",
                    str(Path.home()): "<home>",
                },
            ),
            "returncode": result.returncode if result is not None else None,
            "duration_seconds": result.duration_seconds if result is not None else None,
            "timed_out": result.timed_out if result is not None else False,
            "output_limited": result.output_limited if result is not None else False,
            "retained_project_count": _retained_project_count(repository),
            "fixture_label": fixture_label,
        },
        mode=0o600,
    )

    status = "failed"
    reason = "worker_failed"
    final_response = _bounded_response(trace.final_response)
    project_count = _retained_project_count(repository)
    if interrupted:
        status, reason, final_response = "interrupted", "parent_interrupted", None
    elif command_error:
        status, reason, final_response = "failed", "worker_start_failed", None
    elif result is not None and result.timed_out:
        status, reason, final_response = "timed_out", "wall_timeout", None
    elif result is not None and result.output_limited:
        status, reason, final_response = "failed", "worker_output_limit", None
    elif (
        result is not None
        and result.returncode == 0
        and final_response is not None
        and project_count == 1
    ):
        status, reason = "completed", "agent_returned"
    elif result is not None and result.returncode == 0 and project_count != 1:
        status, reason, final_response = (
            "failed",
            "invalid_project_artifact_count",
            None,
        )
    elif result is not None and result.returncode == 0:
        status, reason = "failed", "missing_final_response"
    elif result is not None:
        status, reason = "failed", f"worker_exit_{result.returncode}"

    try:
        after = environment_probe()
    except PCBDraftError:
        if status == "completed":
            status, reason, final_response = "failed", "environment_probe_failed", None
    else:
        if after != expected:
            status, reason, final_response = (
                "configuration_drift",
                "configuration_drift",
                None,
            )

    if status != "completed":
        _failure_artifact(artifacts, status=status, reason=reason, result=result)
    terminal = _terminal_receipt(
        campaign,
        running,
        artifacts,
        status=status,
        reason=reason,
        final_response=final_response,
        result=result,
    )
    if interrupted:
        raise KeyboardInterrupt
    return terminal


def run_campaign(
    campaign_root: str | Path,
    corpus: BoardBenchCorpus,
    *,
    python_executable: str = sys.executable,
    source_root: str | Path | None = None,
    environment_probe: Callable[[], RunnerEnvironment] | None = None,
    command_runner: CommandRunner = process.run_command,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    fixture_label: str | None = None,
    run_ids: Sequence[str] | None = None,
) -> tuple[BoardBenchRun, ...]:
    """Execute selected still-planned runs once and preserve the 60-run plan.

    With no selector, every planned run is considered in frozen campaign order.
    An explicit selector executes only those ids, while all 60 receipts are still
    initialized and returned so callers cannot accidentally shrink denominators.
    """

    root = Path(campaign_root).expanduser()
    _reject_symlink_components(root, "campaign")
    if fixture_label not in {None, FIXTURE_LABEL}:
        raise ValidationError("unsupported BoardBench fixture label")
    if not python_executable or "\x00" in python_executable:
        raise ValidationError("BoardBench Python executable is invalid")
    if max_output_bytes <= 0:
        raise ValidationError("BoardBench output limit must be positive")
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("BoardBench campaign directory is unavailable")
    root = root.resolve()
    campaign = load_campaign(root / CAMPAIGN_NAME)
    cases = _validated_cases(campaign, corpus)
    planned_ids = tuple(plan.run_id for plan in campaign.runs)
    if run_ids is None:
        selected_ids = frozenset(planned_ids)
    else:
        if isinstance(run_ids, (str, bytes)):
            raise ValidationError("BoardBench run selector must be a sequence of ids")
        requested_ids = tuple(run_ids)
        if not requested_ids:
            raise ValidationError("BoardBench run selector cannot be empty")
        if any(not isinstance(run_id, str) for run_id in requested_ids):
            raise ValidationError("BoardBench run selector contains an invalid id")
        if len(requested_ids) != len(set(requested_ids)):
            raise ValidationError("BoardBench run selector contains duplicate ids")
        unknown_ids = set(requested_ids) - set(planned_ids)
        if unknown_ids:
            raise ValidationError(
                "BoardBench run selector is outside the immutable campaign plan"
            )
        selected_ids = frozenset(requested_ids)
    initialized = initialize_run_receipts(root, campaign, corpus)
    # A bounded run still operates on one immutable 60-run evidence set.  Check
    # every existing terminal artifact before starting any newly selected work,
    # including terminal runs that are not part of this invocation's selector.
    # Otherwise a selector could accidentally bypass corruption in an earlier
    # denominator entry and spend another model call before the drift is found.
    initialized_by_id = {receipt.run_id: receipt for receipt in initialized}
    verified_terminal_ids: set[str] = set()
    for receipt in initialized:
        if receipt.terminal:
            _verify_terminal_artifacts(
                receipt,
                root / RUNS_DIRECTORY / receipt.run_id,
            )
            verified_terminal_ids.add(receipt.run_id)
    raw_checkout = (
        Path(source_root).expanduser()
        if source_root is not None
        else Path(__file__).resolve().parents[3]
    )
    _reject_symlink_components(raw_checkout, "source")
    checkout = raw_checkout.resolve()
    probe = environment_probe or (lambda: capture_environment(checkout))
    results: list[BoardBenchRun] = []
    for plan in campaign.runs:
        run_root = _private_run_root(root, plan.run_id)
        receipt = load_run(run_root / RUN_RECEIPT_NAME)
        if receipt.terminal and (
            plan.run_id not in verified_terminal_ids
            or receipt != initialized_by_id[plan.run_id]
        ):
            _verify_terminal_artifacts(receipt, run_root)
        if plan.run_id not in selected_ids:
            results.append(receipt)
            continue
        if receipt.terminal:
            results.append(receipt)
            continue
        if receipt.status == "running":
            results.append(_recover_running(campaign, receipt, run_root))
            continue
        results.append(
            _execute_planned(
                campaign,
                cases[plan.case_id],
                receipt,
                run_root,
                python_executable=python_executable,
                source_root=checkout,
                environment_probe=probe,
                command_runner=command_runner,
                max_output_bytes=max_output_bytes,
                fixture_label=fixture_label,
            )
        )
    validate_campaign_denominator(campaign, results)
    return tuple(results)
