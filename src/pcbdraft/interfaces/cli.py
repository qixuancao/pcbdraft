"""Command-line interface for PCBDraft."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pcbdraft import PRIMARY_CLI, PRODUCT_NAME, __version__
from pcbdraft.agent.compiler import compile_agent_plan
from pcbdraft.agent.part_resolver import LocalKiCadPartResolver
from pcbdraft.agent.plan import (
    AgentDesignRequest,
    CircuitPlan,
)
from pcbdraft.core.errors import PCBDraftError, TransactionRejected, ValidationError
from pcbdraft.core.io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
    make_directory,
    read_bytes_limited,
)
from pcbdraft.core.repository import configure_repository, current_repository
from pcbdraft.core.runs import new_run_id, utc_timestamp
from pcbdraft.domain.blocks import BlockRegistry
from pcbdraft.domain.ir import IR_FILE_LIMIT
from pcbdraft.domain.operations import MAX_CHANGE_BYTES, load_change_set_bytes
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.requirements import compile_requirements, load_requirements
from pcbdraft.interfaces.api import serve
from pcbdraft.interfaces.chat import run_chat_command
from pcbdraft.interfaces.tui.controller import run_tui_command
from pcbdraft.interfaces.web import run_app
from pcbdraft.kicad.sync import (
    apply_kicad_import,
    preview_kicad_import,
    recover_kicad_import,
    undo_kicad_import,
)
from pcbdraft.services.doctor import doctor_report
from pcbdraft.services.managed import (
    ManagedProject,
    generate_managed_project,
    materialize_managed_design,
    open_managed_project,
)
from pcbdraft.services.transactions import (
    apply_transaction,
    prepare_transaction,
    recover_transaction,
    undo_transaction,
)
from pcbdraft.services.workflows import run_apply, run_patch, run_review
from pcbdraft.verification.benchmark import run_benchmark
from pcbdraft.verification.evidence import record_external_evidence
from pcbdraft.verification.release import (
    build_manufacturing_release,
    verify_manufacturing_release,
)
from pcbdraft.verification.validation import validate_managed_project

DEFAULT_TIMEOUT = 600.0
_ROOT_OPTIONS_WITH_VALUES = frozenset(
    {"--workspace", "--provider", "--project", "--timeout", "--approval-mode"}
)


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0 or timeout > 86400:
        raise argparse.ArgumentTypeError("timeout must be > 0 and <= 86400 seconds")
    return timeout


def invoked_program(argv0: str | None = None) -> str:
    """Return the supported launcher name, defaulting module use to the primary CLI."""
    name = Path(argv0 if argv0 is not None else sys.argv[0]).name
    return name if name == PRIMARY_CLI else PRIMARY_CLI


def _root_command_index(tokens: Sequence[str]) -> int:
    """Locate the subcommand while respecting values of root-level options."""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _ROOT_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _ROOT_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return len(tokens)


def _option_was_supplied(tokens: Sequence[str], option: str) -> bool:
    return any(token == option or token.startswith(f"{option}=") for token in tokens)


def _mcp_scope_arguments(
    args: argparse.Namespace, tokens: Sequence[str]
) -> dict[str, Any]:
    """Merge root and MCP-local scope without silently ignoring either form.

    MCP-local values win when both placements are present. Otherwise an option
    explicitly supplied before ``mcp`` is inherited; untouched parser defaults
    retain MCP's safer review policy.
    """

    command_index = _root_command_index(tokens)
    before = tokens[:command_index]
    after = tokens[command_index + 1 :]

    def selected(option: str, root_name: str, mcp_name: str) -> Any:
        if _option_was_supplied(after, option):
            return getattr(args, mcp_name)
        if _option_was_supplied(before, option):
            return getattr(args, root_name)
        return getattr(args, mcp_name)

    return {
        "workspace": selected("--workspace", "workspace", "mcp_workspace"),
        "provider": selected("--provider", "provider", "mcp_provider"),
        "approval_mode": selected(
            "--approval-mode", "approval_mode", "mcp_approval_mode"
        ),
        "timeout": selected("--timeout", "timeout", "mcp_timeout"),
    }


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or invoked_program(),
        description=(
            f"{PRODUCT_NAME}: generate native KiCad projects from reviewable circuit plans."
        ),
        epilog=(
            "Run without a subcommand to launch the full-screen terminal interface."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--workspace", metavar="DIR", help="terminal interface workspace"
    )
    parser.add_argument(
        "--provider",
        choices=(
            "auto",
            "openai-compatible",
            "builtin",
        ),
        default="auto",
        help="terminal planning provider",
    )
    parser.add_argument(
        "--project", dest="project_id", metavar="ID", help="project to open"
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=420.0,
        metavar="SEC",
        help="terminal action timeout",
    )
    parser.add_argument(
        "--approval-mode",
        choices=("workspace", "review", "read_only"),
        default="workspace",
        help="terminal tool permission policy",
    )
    subcommands = parser.add_subparsers(dest="command")

    doctor = subcommands.add_parser(
        "doctor", help="check local KiCad, Git, and model configuration"
    )
    doctor.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )

    repository = subcommands.add_parser(
        "repository",
        help="show or set the persistent PCB project repository",
        description=(
            "PCBDraft stores every normal project below one persistent repository, "
            "not below the directory where the command was launched."
        ),
    )
    repository.add_argument(
        "DIRECTORY",
        nargs="?",
        help="new repository directory; omit to show the current location",
    )
    repository.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )

    review = subcommands.add_parser(
        "review", help="run available deterministic checks and an AI heuristic review"
    )
    review.add_argument("PROJECT", help="KiCad project directory")
    review.add_argument(
        "--output", metavar="DIR", help="parent directory for the new run"
    )
    review.add_argument(
        "--timeout", type=positive_timeout, default=DEFAULT_TIMEOUT, metavar="SEC"
    )

    patch = subcommands.add_parser(
        "patch", help="prepare a replace-only transaction in staging"
    )
    patch.add_argument("PROJECT", help="KiCad project directory")
    patch.add_argument(
        "--request", required=True, metavar="TEXT", help="requested change"
    )
    patch.add_argument(
        "--output", metavar="DIR", help="parent directory for the new transaction"
    )
    patch.add_argument(
        "--timeout", type=positive_timeout, default=DEFAULT_TIMEOUT, metavar="SEC"
    )

    apply_parser = subcommands.add_parser(
        "apply", help="verify and atomically apply a ready transaction"
    )
    apply_parser.add_argument("RUN_DIR", help="ready patch transaction directory")

    compile_parser = subcommands.add_parser(
        "compile",
        help="compile a legacy deterministic fixture requirements file into semantic IR",
    )
    compile_parser.add_argument("REQUIREMENTS", help="requirements JSON file")
    compile_parser.add_argument("--output", required=True, metavar="FILE")
    compile_parser.add_argument("--json", action="store_true", dest="as_json")

    generate = subcommands.add_parser(
        "generate",
        help="generate a legacy deterministic fixture managed KiCad project",
    )
    generate.add_argument("REQUIREMENTS", help="requirements JSON file")
    generate.add_argument("OUTPUT", help="new project directory")
    generate.add_argument("--json", action="store_true", dest="as_json")

    symbols = subcommands.add_parser(
        "symbols", help="search symbols available in the local KiCad installation"
    )
    symbols.add_argument("QUERY")
    symbols.add_argument("--limit", type=int, default=12)
    symbols.add_argument("--json", action="store_true", dest="as_json")

    agent_compile = subcommands.add_parser(
        "agent-compile",
        help="compile a reviewed generic agent request and circuit plan into semantic IR",
    )
    agent_compile.add_argument("REQUEST", help="agent design request JSON")
    agent_compile.add_argument("PLAN", help="reviewed circuit plan JSON")
    agent_compile.add_argument("--ir-output", required=True, metavar="FILE")
    agent_compile.add_argument("--parts-output", required=True, metavar="FILE")
    agent_compile.add_argument("--json", action="store_true", dest="as_json")

    agent_generate = subcommands.add_parser(
        "agent-generate",
        help="generate a native KiCad project from a reviewed stock-library plan",
    )
    agent_generate.add_argument("REQUEST", help="agent design request JSON")
    agent_generate.add_argument("PLAN", help="reviewed circuit plan JSON")
    agent_generate.add_argument("OUTPUT", help="new project directory")
    agent_generate.add_argument(
        "--retain-failed-attempt",
        metavar="DIR",
        help="attempt directory retained on failure; defaults beside OUTPUT",
    )
    agent_generate.add_argument("--json", action="store_true", dest="as_json")

    inspect_parser = subcommands.add_parser(
        "inspect", help="inspect a managed project and report synchronization drift"
    )
    inspect_parser.add_argument("PROJECT")
    inspect_parser.add_argument("--json", action="store_true", dest="as_json")

    validate_parser = subcommands.add_parser(
        "validate", help="run available KiCad and PCBDraft checks"
    )
    validate_parser.add_argument("PROJECT")
    validate_parser.add_argument("--output", metavar="DIR")
    validate_parser.add_argument(
        "--timeout", type=positive_timeout, default=90.0, metavar="SEC"
    )
    validate_parser.add_argument("--json", action="store_true", dest="as_json")

    release_parser = subcommands.add_parser(
        "release", help="build a validated manufacturing-candidate evidence bundle"
    )
    release_parser.add_argument("PROJECT")
    release_parser.add_argument("OUTPUT")
    release_parser.add_argument(
        "--timeout", type=positive_timeout, default=180.0, metavar="SEC"
    )
    release_parser.add_argument("--json", action="store_true", dest="as_json")

    release_verify = subcommands.add_parser(
        "release-verify", help="verify a manufacturing release and deterministic ZIP"
    )
    release_verify.add_argument("RELEASE")
    release_verify.add_argument("--json", action="store_true", dest="as_json")

    sync_parser = subcommands.add_parser(
        "sync", help="preview recognized KiCad edits and optionally import them"
    )
    sync_parser.add_argument("PROJECT")
    sync_parser.add_argument("--apply", action="store_true")
    sync_parser.add_argument("--preview-output", metavar="FILE")
    sync_parser.add_argument(
        "--timeout", type=positive_timeout, default=120.0, metavar="SEC"
    )
    sync_parser.add_argument("--json", action="store_true", dest="as_json")

    sync_undo = subcommands.add_parser("sync-undo", help="undo an applied KiCad import")
    sync_undo.add_argument("TRANSACTION")
    sync_recover = subcommands.add_parser(
        "sync-recover", help="recover an interrupted KiCad import directory swap"
    )
    sync_recover.add_argument("TRANSACTION")

    parts = subcommands.add_parser("parts", help="query the trusted component graph")
    parts.add_argument("--kind")
    parts.add_argument("--function")
    parts.add_argument("--min-voltage-v", type=float)
    parts.add_argument("--include-inactive", action="store_true")
    parts.add_argument("--include-untrusted", action="store_true")
    parts.add_argument("--json", action="store_true", dest="as_json")

    evidence = subcommands.add_parser(
        "evidence-record", help="record attributed external L4/L6/L7 evidence"
    )
    evidence.add_argument("PROJECT")
    evidence.add_argument("--level", required=True, choices=("L4", "L6", "L7"))
    evidence.add_argument("--outcome", required=True, choices=("pass", "fail"))
    evidence.add_argument("--actor", required=True)
    evidence.add_argument("--role", required=True)
    evidence.add_argument("--performed-at", required=True)
    evidence.add_argument("--statement", required=True)
    evidence.add_argument("--artifact", required=True, action="append")
    evidence.add_argument("--metadata", required=True, metavar="JSON_FILE")

    benchmark = subcommands.add_parser(
        "benchmark", help="run the independent error-injection and repair corpus"
    )
    benchmark.add_argument("OUTPUT", help="new benchmark result JSON file")
    benchmark.add_argument("--repetitions", type=int, choices=range(2, 21), default=5)
    benchmark.add_argument("--corpus", metavar="JSON_FILE")
    benchmark.add_argument("--model-runs", type=int, choices=(0, 2, 3, 4, 5), default=0)
    benchmark.add_argument(
        "--model-timeout", type=positive_timeout, default=420.0, metavar="SEC"
    )
    benchmark.add_argument("--json", action="store_true", dest="as_json")

    semantic_preview = subcommands.add_parser(
        "semantic-preview", help="prepare a previewable semantic IR transaction"
    )
    semantic_preview.add_argument("IR")
    semantic_preview.add_argument("CHANGE_SET")
    semantic_preview.add_argument("--output", metavar="DIR")
    semantic_apply = subcommands.add_parser(
        "semantic-apply", help="apply a ready semantic IR transaction"
    )
    semantic_apply.add_argument("TRANSACTION")
    semantic_undo = subcommands.add_parser(
        "semantic-undo", help="undo an applied semantic IR transaction"
    )
    semantic_undo.add_argument("TRANSACTION")
    semantic_recover = subcommands.add_parser(
        "semantic-recover", help="recover an interrupted semantic IR transaction"
    )
    semantic_recover.add_argument("TRANSACTION")

    chat = subcommands.add_parser(
        "chat", help="run explicit project actions, including JSON automation"
    )
    chat.add_argument("--workspace", metavar="DIR")
    chat.add_argument(
        "--provider",
        choices=(
            "auto",
            "openai-compatible",
            "builtin",
        ),
        default="auto",
    )
    chat.add_argument("--project", dest="project_id", metavar="ID")
    chat.add_argument("--new", dest="new_name", metavar="NAME")
    chat.add_argument("--message", metavar="TEXT")
    chat.add_argument(
        "--yes",
        action="store_true",
        help="confirm a ready generation or semantic change noninteractively",
    )
    chat.add_argument("--undo", action="store_true")
    chat.add_argument("--validate", action="store_true", dest="chat_validate")
    chat.add_argument("--release", action="store_true", dest="chat_release")
    chat.add_argument("--list", action="store_true", dest="list_only")
    chat.add_argument("--json", action="store_true", dest="as_json")
    chat.add_argument("--timeout", type=positive_timeout, default=420.0, metavar="SEC")

    app = subcommands.add_parser(
        "app", help="start the local PCBDraft browser application"
    )
    app.add_argument("--workspace", metavar="DIR")
    app.add_argument(
        "--provider",
        choices=(
            "auto",
            "openai-compatible",
            "builtin",
        ),
        default="auto",
    )
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=8765)
    app.add_argument(
        "--no-open", action="store_true", help="do not open the default browser"
    )

    subcommands.add_parser(
        "api", help="serve newline-delimited JSON-RPC 2.0 on stdin/stdout"
    )
    mcp = subcommands.add_parser(
        "mcp",
        help="serve project-bound PCB tools over MCP stdio (2025-11-25)",
    )
    mcp.add_argument("--project", required=True, dest="mcp_project_id", metavar="ID")
    mcp.add_argument(
        "--workspace",
        dest="mcp_workspace",
        metavar="ABS_DIR",
        help="absolute isolated workspace; omit to use the configured repository",
    )
    mcp.add_argument(
        "--provider",
        dest="mcp_provider",
        choices=("auto", "openai-compatible", "builtin"),
        default="auto",
    )
    mcp.add_argument(
        "--approval-mode",
        dest="mcp_approval_mode",
        choices=("workspace", "review", "read_only"),
        default="review",
    )
    mcp.add_argument(
        "--timeout",
        dest="mcp_timeout",
        type=positive_timeout,
        default=420.0,
        metavar="SEC",
    )
    return parser


def _print_doctor(report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        for name, result in report["tools"].items():
            status = "ok" if result["available"] else "missing/failed"
            version = result.get("version") or "unknown"
            path = result.get("path") or "not found"
            print(f"{name}: {status} — {version} ({path})")
    return 0 if report["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    tokens = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(tokens)
    result: Any
    try:
        if args.command is None:
            return run_tui_command(
                workspace=args.workspace,
                provider=args.provider,
                project_id=args.project_id,
                timeout=args.timeout,
                approval_mode=args.approval_mode,
            )
        if args.command == "doctor":
            return _print_doctor(doctor_report(), args.as_json)
        if args.command == "repository":
            repository = (
                configure_repository(args.DIRECTORY)
                if args.DIRECTORY
                else current_repository()
            )
            value = {
                "root": str(repository.root),
                "projects_root": str(repository.projects_root),
                "source": repository.source,
                "configured_now": repository.configured_now,
            }
            _emit(
                value,
                args.as_json,
                "PCB project repository: " + str(repository.root),
            )
            return 0
        if args.command == "review":
            run_dir = run_review(
                args.PROJECT,
                output_parent=args.output,
                timeout=args.timeout,
            )
            print(f"review complete: {run_dir}")
            return 0
        if args.command == "patch":
            run_dir = run_patch(
                args.PROJECT,
                request=args.request,
                output_parent=args.output,
                timeout=args.timeout,
            )
            print(f"transaction ready: {run_dir}")
            return 0
        if args.command == "apply":
            run_dir = run_apply(args.RUN_DIR)
            print(f"transaction applied: {run_dir}")
            return 0
        if args.command == "symbols":
            if not 1 <= args.limit <= 64:
                raise ValidationError("symbol candidate limit must be from 1 to 64")
            candidates = LocalKiCadPartResolver().find(args.QUERY, limit=args.limit)
            value = {
                "query": args.QUERY,
                "candidates": [candidate.to_dict() for candidate in candidates],
            }
            _emit(
                value,
                args.as_json,
                "\n".join(candidate.symbol for candidate in candidates),
            )
            return 0
        if args.command == "agent-compile":
            compilation = _load_agent_compilation(args.REQUEST, args.PLAN)
            ir_output = Path(args.ir_output).expanduser().resolve(strict=False)
            parts_output = Path(args.parts_output).expanduser().resolve(strict=False)
            if ir_output == parts_output:
                raise ValidationError(
                    "agent IR and part-catalog output paths must differ"
                )
            atomic_write_bytes(
                ir_output, compilation.design.canonical_bytes(), mode=0o644
            )
            atomic_write_json(parts_output, compilation.graph.to_dict(), mode=0o644)
            value = {
                "ir": str(ir_output),
                "part_catalog": str(parts_output),
                "content_hash": compilation.design.content_hash(),
                "assurance": compilation.design.metadata.get("assurance"),
                "component_qualification": compilation.review.qualification.to_dict(),
                "plan_review": compilation.review.to_dict(),
                "components": len(compilation.design.components),
                "nets": len(compilation.design.nets),
            }
            _emit(value, args.as_json, f"generic semantic IR compiled: {ir_output}")
            return 0
        if args.command == "agent-generate":
            compilation = _load_agent_compilation(args.REQUEST, args.PLAN)
            output = Path(args.OUTPUT).expanduser()
            attempt_root = (
                Path(args.retain_failed_attempt).expanduser()
                if args.retain_failed_attempt
                else output.parent / f"{output.name}.attempt-{new_run_id()}"
            )
            if (
                attempt_root.name in {"", ".", ".."}
                or attempt_root.is_symlink()
                or attempt_root.exists()
            ):
                raise ValidationError(
                    "generic failed-attempt directory is unsafe or already occupied"
                )
            make_directory(attempt_root.parent)
            make_directory(attempt_root, parents=False)
            attempt: dict[str, Any] = {
                "schema": "pcbdraft-generic-cli-attempt",
                "version": 1,
                "status": "running",
                "started_at": utc_timestamp(),
                "completed_at": None,
                "error": None,
                "files": {
                    "request": "request.json",
                    "plan": "circuit-plan.json",
                    "semantic_ir": "design.pcbir.json",
                    "part_catalog": "parts.pcbdraft.json",
                    "retained_native": None,
                },
            }
            atomic_write_json(
                attempt_root / "request.json", compilation.request.to_dict()
            )
            atomic_write_json(
                attempt_root / "circuit-plan.json", compilation.plan.to_dict()
            )
            atomic_write_json(
                attempt_root / "design.pcbir.json", compilation.design.to_dict()
            )
            atomic_write_json(
                attempt_root / "parts.pcbdraft.json", compilation.graph.to_dict()
            )
            atomic_write_json(attempt_root / "attempt.json", attempt)
            try:
                result = materialize_managed_design(
                    compilation.request,
                    compilation.design,
                    output,
                    graph=compilation.graph,
                    plan=compilation.plan,
                    retain_failed_attempt=attempt_root / "native",
                )
            except PCBDraftError as exc:
                attempt["status"] = "failed"
                attempt["completed_at"] = utc_timestamp()
                attempt["error"] = str(exc)[:2048]
                if (attempt_root / "native").is_dir():
                    attempt["files"]["retained_native"] = "native"
                atomic_write_json(attempt_root / "attempt.json", attempt)
                value = {
                    "error": str(exc),
                    "retained_attempt": str(attempt_root),
                }
                _emit(
                    value,
                    args.as_json,
                    f"generic generation failed: {exc}; retained attempt: {attempt_root}",
                )
                return exc.exit_code
            shutil.rmtree(attempt_root)
            value = {
                **_managed_value(result.project),
                "routing": {
                    "state": result.pcb.routing.state,
                    "unrouted": list(result.pcb.routing.unrouted),
                    "via_count": len(result.pcb.routing.vias),
                },
                "validation": "not_run",
            }
            _emit(
                value,
                args.as_json,
                "KiCad schematic and routed PCB generated: "
                f"{result.project.root} (ERC/DRC not run)",
            )
            return 0
        if args.command == "compile":
            graph = PartGraph.bundled()
            design = compile_requirements(
                load_requirements(args.REQUIREMENTS),
                graph=graph,
                registry=BlockRegistry.bundled(graph),
            )
            output = Path(args.output).expanduser().resolve(strict=False)
            atomic_write_bytes(output, design.canonical_bytes(), mode=0o644)
            value = {
                "output": str(output),
                "content_hash": design.content_hash(),
                "components": len(design.components),
                "nets": len(design.nets),
            }
            _emit(value, args.as_json, f"semantic IR compiled: {output}")
            return 0
        if args.command == "generate":
            result = generate_managed_project(args.REQUIREMENTS, args.OUTPUT)
            value = _managed_value(result.project)
            _emit(
                value, args.as_json, f"managed project generated: {result.project.root}"
            )
            return 0
        if args.command == "inspect":
            project = open_managed_project(args.PROJECT)
            value = _managed_value(project)
            _emit(
                value,
                args.as_json,
                f"managed project {'synchronized' if not project.drift() else 'drifted'}: {project.root}",
            )
            return 0 if not project.drift() else 3
        if args.command == "validate":
            result = validate_managed_project(
                args.PROJECT, output=args.output, timeout=args.timeout
            )
            value = {
                "report": str(result.report_path),
                "report_sha256": result.report_sha256,
                "candidate_ready": result.candidate_ready,
                "production_evidence_complete": (result.production_evidence_complete),
                "production_ready": result.production_ready,
                "levels": [level.to_dict() for level in result.levels],
            }
            _emit(value, args.as_json, f"validation complete: {result.report_path}")
            return 0 if result.candidate_ready else 3
        if args.command == "release":
            result = build_manufacturing_release(
                args.PROJECT, args.OUTPUT, timeout=args.timeout
            )
            value = {
                "root": str(result.root),
                "manifest": str(result.manifest_path),
                "manifest_sha256": result.manifest_sha256,
                "archive": str(result.archive_path),
                "archive_sha256": result.archive_sha256,
                "candidate_ready": result.candidate_ready,
                "production_evidence_complete": (result.production_evidence_complete),
                "production_ready": result.production_ready,
            }
            _emit(value, args.as_json, f"manufacturing candidate built: {result.root}")
            return 0
        if args.command == "release-verify":
            result = verify_manufacturing_release(args.RELEASE)
            _emit(
                result.to_dict(),
                args.as_json,
                f"manufacturing release verified: {result.root}",
            )
            return 0
        if args.command == "sync":
            preview = preview_kicad_import(args.PROJECT)
            value = preview.to_dict()
            if args.preview_output:
                atomic_write_json(Path(args.preview_output), value)
            if args.apply and preview.has_changes:
                transaction = apply_kicad_import(preview, timeout=args.timeout)
                value["transaction"] = str(transaction)
                value["applied"] = True
            else:
                value["applied"] = False
            _emit(
                value,
                args.as_json,
                (
                    f"KiCad import applied: {value['transaction']}"
                    if value["applied"]
                    else f"KiCad sync preview: {len(preview.native_changes)} recognized changes"
                ),
            )
            return 0
        if args.command == "sync-undo":
            print(f"KiCad import undone: {undo_kicad_import(args.TRANSACTION)}")
            return 0
        if args.command == "sync-recover":
            print(
                f"KiCad import recovery state: {recover_kicad_import(args.TRANSACTION)}"
            )
            return 0
        if args.command == "parts":
            records = PartGraph.bundled().find(
                kind=args.kind,
                function=args.function,
                min_voltage_v=args.min_voltage_v,
                active_only=not args.include_inactive,
                trusted_only=not args.include_untrusted,
            )
            value = {
                "count": len(records),
                "parts": [part.to_dict() for part in records],
            }
            _emit(value, args.as_json, "\n".join(part.id for part in records))
            return 0
        if args.command == "evidence-record":
            metadata = load_json_limited(Path(args.metadata), IR_FILE_LIMIT)
            path = record_external_evidence(
                args.PROJECT,
                level=args.level,
                outcome=args.outcome,
                actor=args.actor,
                role=args.role,
                performed_at=args.performed_at,
                statement=args.statement,
                artifacts=[Path(value) for value in args.artifact],
                metadata=metadata,
            )
            print(f"external evidence recorded: {path}")
            return 0
        if args.command == "benchmark":
            result = run_benchmark(
                args.OUTPUT,
                repetitions=args.repetitions,
                corpus_path=args.corpus,
                model_runs=args.model_runs,
                model_timeout=args.model_timeout,
            )
            metrics = result.result["metrics"]
            value = {
                "report": str(result.report_path),
                "metrics": metrics,
                "model_consistency": result.result["model_consistency"],
            }
            _emit(value, args.as_json, f"benchmark complete: {result.report_path}")
            passed = (
                metrics["confusion_matrix"]["false_negative"] == 0
                and metrics["confusion_matrix"]["false_positive"] == 0
                and metrics["repair"]["success_rate"] == 1.0
                and metrics["repair"]["introduced_regression_cases"] == 0
                and metrics["repeatability"]["rate"] == 1.0
            )
            return 0 if passed else 3
        if args.command == "semantic-preview":
            change_set = load_change_set_bytes(
                read_bytes_limited(Path(args.CHANGE_SET), MAX_CHANGE_BYTES)
            )
            run_dir = prepare_transaction(
                args.IR, change_set, output_parent=args.output
            )
            print(f"semantic transaction ready: {run_dir}")
            return 0
        if args.command == "semantic-apply":
            print(
                f"semantic transaction applied: {apply_transaction(args.TRANSACTION)}"
            )
            return 0
        if args.command == "semantic-undo":
            print(f"semantic transaction undone: {undo_transaction(args.TRANSACTION)}")
            return 0
        if args.command == "semantic-recover":
            print(
                f"semantic transaction recovered: {recover_transaction(args.TRANSACTION)}"
            )
            return 0
        if args.command == "chat":
            return run_chat_command(
                workspace=args.workspace,
                provider=args.provider,
                project_id=args.project_id,
                new_name=args.new_name,
                message=args.message,
                assume_yes=args.yes,
                undo=args.undo,
                validate=args.chat_validate,
                release=args.chat_release,
                list_only=args.list_only,
                as_json=args.as_json,
                timeout=args.timeout,
            )
        if args.command == "app":
            return run_app(
                host=args.host,
                port=args.port,
                workspace=args.workspace,
                provider=args.provider,
                open_browser=not args.no_open,
            )
        if args.command == "api":
            return serve()
        if args.command == "mcp":
            from pcbdraft.interfaces.mcp import run_mcp_stdio

            scope = _mcp_scope_arguments(args, tokens)
            return run_mcp_stdio(
                workspace=scope["workspace"],
                provider=scope["provider"],
                project_id=args.mcp_project_id,
                approval_mode=scope["approval_mode"],
                timeout=scope["timeout"],
            )
    except TransactionRejected as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return exc.exit_code
    except PCBDraftError as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return exc.exit_code
    parser.error("unknown command")
    return 2


def _emit(value: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(text)


def _load_agent_compilation(request_path: str, plan_path: str):
    request = AgentDesignRequest.from_dict(
        load_json_limited(Path(request_path), IR_FILE_LIMIT)
    )
    plan = CircuitPlan.from_dict(load_json_limited(Path(plan_path), IR_FILE_LIMIT))
    return compile_agent_plan(request, plan)


def _managed_value(project: ManagedProject) -> dict[str, object]:
    return {
        "root": str(project.root),
        "design_id": project.design.design_id,
        "design_content_hash": project.design.content_hash(),
        "manifest": str(project.manifest_path),
        "drift": list(project.drift()),
        "files": dict(sorted(project.manifest["files"].items())),
    }


if __name__ == "__main__":
    raise SystemExit(main())
