"""Command-line interface for pcb-agent-runtime."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .api import serve
from .benchmark import run_benchmark
from .blocks import BlockRegistry
from .doctor import doctor_report
from .errors import PcbAgentError, TransactionRejected
from .external_evidence import record_external_evidence
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
    read_bytes_limited,
)
from .ir import IR_FILE_LIMIT
from .managed import generate_managed_project, open_managed_project
from .operations import MAX_CHANGE_BYTES, load_change_set_bytes
from .parts import PartGraph
from .release import build_manufacturing_release, verify_manufacturing_release
from .requirements import compile_requirements, load_requirements
from .sync import (
    apply_kicad_import,
    preview_kicad_import,
    recover_kicad_import,
    undo_kicad_import,
)
from .transactions import (
    apply_transaction,
    prepare_transaction,
    recover_transaction,
    undo_transaction,
)
from .validation import validate_managed_project
from .workflows import run_apply, run_patch, run_review

DEFAULT_TIMEOUT = 600.0


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0 or timeout > 86400:
        raise argparse.ArgumentTypeError("timeout must be > 0 and <= 86400 seconds")
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcb-agent",
        description="Evidence-driven semantic PCB design, validation, and release runtime.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser(
        "doctor", help="check local Codex, KiCad, and Git tools"
    )
    doctor.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )

    review = subcommands.add_parser(
        "review", help="run deterministic gates and an AI heuristic review"
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
        "compile", help="compile strict requirements into deterministic semantic IR"
    )
    compile_parser.add_argument("REQUIREMENTS", help="requirements JSON file")
    compile_parser.add_argument("--output", required=True, metavar="FILE")
    compile_parser.add_argument("--json", action="store_true", dest="as_json")

    generate = subcommands.add_parser(
        "generate", help="generate a new managed semantic and native KiCad project"
    )
    generate.add_argument("REQUIREMENTS", help="requirements JSON file")
    generate.add_argument("OUTPUT", help="new project directory")
    generate.add_argument("--json", action="store_true", dest="as_json")

    inspect_parser = subcommands.add_parser(
        "inspect", help="inspect a managed project and report synchronization drift"
    )
    inspect_parser.add_argument("PROJECT")
    inspect_parser.add_argument("--json", action="store_true", dest="as_json")

    validate_parser = subcommands.add_parser(
        "validate", help="run honest L0-L7 validation on a managed project"
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
        "evidence-record", help="record attributed external L6/L7 evidence"
    )
    evidence.add_argument("PROJECT")
    evidence.add_argument("--level", required=True, choices=("L6", "L7"))
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

    subcommands.add_parser(
        "api", help="serve newline-delimited JSON-RPC 2.0 on stdin/stdout"
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
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _print_doctor(doctor_report(), args.as_json)
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
        if args.command == "api":
            return serve()
    except TransactionRejected as exc:
        print(f"pcb-agent: {exc}", file=sys.stderr)
        return exc.exit_code
    except PcbAgentError as exc:
        print(f"pcb-agent: {exc}", file=sys.stderr)
        return exc.exit_code
    parser.error("unknown command")
    return 2


def _emit(value: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(text)


def _managed_value(project: object) -> dict[str, object]:
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
