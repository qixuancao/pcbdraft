"""Versioned newline-delimited JSON-RPC API for local agents and automation."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import (
    COMPATIBILITY_CLIS,
    DISTRIBUTION_NAME,
    PRIMARY_CLI,
    PRODUCT_NAME,
    __version__,
)
from .benchmark import run_benchmark
from .blocks import BlockRegistry
from .errors import PcbAgentError, ValidationError
from .external_evidence import record_external_evidence
from .managed import generate_managed_project, open_managed_project
from .parts import PartGraph
from .release import build_manufacturing_release, verify_manufacturing_release
from .requirements import (
    GENERATION_PROFILE_DOMAINS,
    GENERATION_PROFILE_ID,
    SUPPORTED_FUNCTIONS,
    RequirementsSpec,
    compile_requirements,
)
from .scope import REJECTED_DOMAINS, SUPPORTED_DOMAINS
from .sync import apply_kicad_import, preview_kicad_import
from .validation import EVIDENCE_STATES, validate_managed_project

API_VERSION = "1.0"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 10_000


def capabilities() -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "runtime_version": __version__,
        "product": {
            "name": PRODUCT_NAME,
            "distribution": DISTRIBUTION_NAME,
            "primary_cli": PRIMARY_CLI,
            "compatibility_clis": list(COMPATIBILITY_CLIS),
            "python_module": "pcb_agent",
        },
        "transport": "newline_delimited_json_rpc_2.0",
        "methods": [
            "runtime.capabilities",
            "parts.find",
            "requirements.compile",
            "project.generate",
            "project.inspect",
            "project.validate",
            "project.release",
            "release.verify",
            "sync.preview",
            "sync.apply",
            "evidence.record",
            "benchmark.run",
        ],
        "evidence_states": sorted(EVIDENCE_STATES),
        "accepted_scope": {
            "layers": [2, 4],
            "domains": sorted(GENERATION_PROFILE_DOMAINS),
            "high_risk_domains": "explicitly_rejected",
        },
        "generation_profiles": [
            {
                "id": GENERATION_PROFILE_ID,
                "domains": sorted(GENERATION_PROFILE_DOMAINS),
                "functions": sorted(SUPPORTED_FUNCTIONS),
                "layers": [2, 4],
            }
        ],
        "scope_policy": {
            "recognized_domains": sorted(SUPPORTED_DOMAINS),
            "explicitly_rejected_domains": sorted(REJECTED_DOMAINS),
            "recognized_without_bundled_generator": sorted(
                SUPPORTED_DOMAINS - GENERATION_PROFILE_DOMAINS
            ),
        },
    }


def handle_request(request: Any) -> dict[str, Any]:
    request_id: Any = None
    try:
        if not isinstance(request, Mapping):
            raise RpcError(-32600, "request must be an object")
        request_id = request.get("id")
        if set(request) - {"jsonrpc", "id", "method", "params"}:
            raise RpcError(-32600, "request contains unknown fields")
        if request.get("jsonrpc") != "2.0" or not isinstance(
            request.get("method"), str
        ):
            raise RpcError(-32600, "invalid JSON-RPC request")
        params = request.get("params", {})
        if not isinstance(params, Mapping):
            raise RpcError(-32602, "params must be an object")
        result = _dispatch(request["method"], dict(params))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": exc.code, "message": str(exc)},
        }
    except PcbAgentError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000 - exc.exit_code,
                "message": str(exc),
                "data": {"type": type(exc).__name__},
            },
        }
    except Exception as exc:  # noqa: BLE001 - API must preserve protocol framing
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": "internal runtime error",
                "data": {"type": type(exc).__name__},
            },
        }


def _dispatch(method: str, params: dict[str, Any]) -> Any:
    if method == "runtime.capabilities":
        _exact(params, set())
        return capabilities()
    if method == "parts.find":
        _exact(
            params,
            set(),
            {"kind", "function", "min_voltage_v", "active_only", "trusted_only"},
        )
        parts = PartGraph.bundled().find(**params)
        return {"parts": [part.to_dict() for part in parts]}
    if method == "requirements.compile":
        _exact(params, {"requirements"})
        spec = RequirementsSpec.from_dict(params["requirements"])
        graph = PartGraph.bundled()
        design = compile_requirements(
            spec, graph=graph, registry=BlockRegistry.bundled(graph)
        )
        return {"design": design.to_dict(), "content_hash": design.content_hash()}
    if method == "project.generate":
        _exact(params, {"requirements", "output"})
        generated = generate_managed_project(
            RequirementsSpec.from_dict(params["requirements"]),
            _path(params["output"], "output"),
        )
        return _project_result(generated.project)
    if method == "project.inspect":
        _exact(params, {"project"})
        return _project_result(
            open_managed_project(_path(params["project"], "project"))
        )
    if method == "project.validate":
        _exact(params, {"project", "output"}, {"timeout"})
        result = validate_managed_project(
            _path(params["project"], "project"),
            output=_path(params["output"], "output"),
            timeout=_timeout(params.get("timeout", 90.0)),
        )
        return {
            "report": str(result.report_path),
            "report_sha256": result.report_sha256,
            "candidate_ready": result.candidate_ready,
            "production_ready": result.production_ready,
            "levels": [level.to_dict() for level in result.levels],
        }
    if method == "project.release":
        _exact(params, {"project", "output"}, {"timeout"})
        result = build_manufacturing_release(
            _path(params["project"], "project"),
            _path(params["output"], "output"),
            timeout=_timeout(params.get("timeout", 180.0)),
        )
        return {
            "root": str(result.root),
            "manifest": str(result.manifest_path),
            "manifest_sha256": result.manifest_sha256,
            "archive": str(result.archive_path),
            "archive_sha256": result.archive_sha256,
            "candidate_ready": result.candidate_ready,
            "production_ready": result.production_ready,
        }
    if method == "release.verify":
        _exact(params, {"release"})
        return verify_manufacturing_release(
            _path(params["release"], "release")
        ).to_dict()
    if method == "sync.preview":
        _exact(params, {"project"})
        return preview_kicad_import(_path(params["project"], "project")).to_dict()
    if method == "sync.apply":
        _exact(params, {"project"}, {"timeout"})
        preview = preview_kicad_import(_path(params["project"], "project"))
        transaction = apply_kicad_import(
            preview, timeout=_timeout(params.get("timeout", 120.0))
        )
        return {"transaction": str(transaction), "preview": preview.to_dict()}
    if method == "evidence.record":
        _exact(
            params,
            {
                "project",
                "level",
                "outcome",
                "actor",
                "role",
                "performed_at",
                "statement",
                "artifacts",
                "metadata",
            },
        )
        artifacts = params["artifacts"]
        if not isinstance(artifacts, list):
            raise RpcError(-32602, "artifacts must be an array")
        path = record_external_evidence(
            _path(params["project"], "project"),
            level=params["level"],
            outcome=params["outcome"],
            actor=params["actor"],
            role=params["role"],
            performed_at=params["performed_at"],
            statement=params["statement"],
            artifacts=[_path(value, "artifact") for value in artifacts],
            metadata=params["metadata"],
        )
        return {"index": str(path)}
    if method == "benchmark.run":
        _exact(
            params,
            {"output"},
            {"repetitions", "corpus", "model_runs", "model_timeout"},
        )
        corpus = params.get("corpus")
        result = run_benchmark(
            _path(params["output"], "output"),
            repetitions=_integer(params.get("repetitions", 5), "repetitions"),
            corpus_path=_path(corpus, "corpus") if corpus is not None else None,
            model_runs=_integer(params.get("model_runs", 0), "model_runs"),
            model_timeout=_timeout(params.get("model_timeout", 420.0)),
        )
        return {
            "report": str(result.report_path),
            "metrics": result.result["metrics"],
            "model_consistency": result.result["model_consistency"],
        }
    raise RpcError(-32601, f"method not found: {method}")


def serve(
    input_stream: TextIO | None = None, output_stream: TextIO | None = None
) -> int:
    source = input_stream or sys.stdin
    destination = output_stream or sys.stdout
    for index, line in enumerate(source, start=1):
        if index > MAX_REQUESTS:
            raise ValidationError(
                f"API accepts at most {MAX_REQUESTS} requests per process"
            )
        if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "request exceeds byte limit"},
            }
        else:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            else:
                response = handle_request(request)
        destination.write(
            json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        destination.flush()
    return 0


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _exact(
    params: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    allowed = required | (optional or set())
    missing = required - set(params)
    extra = set(params) - allowed
    if missing or extra:
        raise RpcError(
            -32602,
            f"parameter fields mismatch (missing={sorted(missing)}, extra={sorted(extra)})",
        )


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RpcError(-32602, f"{name} must be a non-empty path string")
    return Path(value)


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RpcError(-32602, "timeout must be a number")
    result = float(value)
    if not 0 < result <= 3600:
        raise RpcError(-32602, "timeout must be in (0, 3600]")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(-32602, f"{name} must be an integer")
    return value


def _project_result(project: Any) -> dict[str, Any]:
    return {
        "root": str(project.root),
        "design_id": project.design.design_id,
        "design_content_hash": project.design.content_hash(),
        "manifest": str(project.manifest_path),
        "drift": list(project.drift()),
        "files": dict(sorted(project.manifest["files"].items())),
    }
