"""Versioned newline-delimited JSON-RPC API for local agents and automation."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import (
    DISTRIBUTION_NAME,
    PRIMARY_CLI,
    PRODUCT_NAME,
    __version__,
)
from .agent_design import (
    AgentDesignRequest,
    CircuitPlan,
    LocalKiCadPartResolver,
    circuit_plan_schema,
    compile_agent_plan,
    planner_symbol_context,
)
from .application import sanitize_user_text
from .benchmark import run_benchmark
from .blocks import BlockRegistry
from .errors import PCBDraftError, ValidationError
from .external_evidence import record_external_evidence
from .managed import (
    generate_managed_project,
    materialize_managed_design,
    open_managed_project,
)
from .parts import PartGraph
from .release import build_manufacturing_release, verify_manufacturing_release
from .requirements import RequirementsSpec, compile_requirements
from .runs import utc_timestamp
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
            "python_module": "pcbdraft",
        },
        "transport": "newline_delimited_json_rpc_2.0",
        "methods": [
            "runtime.capabilities",
            "symbols.find",
            "parts.find",
            "agent.request.prepare",
            "agent.plan.compile",
            "agent.project.generate",
            # Compatibility methods for the deterministic fixture compiler. They
            # are intentionally not the conversational product path.
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
        "generation": {
            "layers": "agent_selected_or_user_specified; checked by installed KiCad during generation",
            "component_libraries": "installed_stock_kicad_only",
            "domain_requests": "attempted_without_preemptive_rejection",
            "validation_claims": "limited_to_recorded_tool_evidence",
        },
        "agent_runtime": {
            "request_schema": "pcbdraft-agent-design-request",
            "plan_schema": "pcbdraft-circuit-plan",
            "part_resolution": "installed KiCad symbols are resolved into a project-local provisional part graph",
            "plan_checks": "topology findings do not block an attempt; completed deterministic failures block candidate readiness and can enter bounded repair",
            "project_files": "successful generic projects retain circuit-plan.json and component-qualification.json beside the native KiCad files",
            "model_authority": "structured topology only; no raw KiCad, coordinates, or executable code",
        },
        "legacy_fixture_methods": ["requirements.compile", "project.generate"],
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
    except PCBDraftError as exc:
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
    if method == "symbols.find":
        _exact(params, {"query"}, {"limit"})
        query = params["query"]
        if not isinstance(query, str):
            raise RpcError(-32602, "query must be a string")
        limit = _integer(params.get("limit", 12), "limit")
        if not 1 <= limit <= 64:
            raise RpcError(-32602, "limit must be from 1 to 64")
        candidates = LocalKiCadPartResolver().find(query, limit=limit)
        return {"candidates": [candidate.to_dict() for candidate in candidates]}
    if method == "parts.find":
        _exact(
            params,
            set(),
            {"kind", "function", "min_voltage_v", "active_only", "trusted_only"},
        )
        parts = PartGraph.bundled().find(**params)
        return {"parts": [part.to_dict() for part in parts]}
    if method == "agent.request.prepare":
        _exact(
            params,
            {
                "request_summary",
                "design_name",
                "layers",
                "requested_parts",
                "functions",
            },
            {"assumptions", "board", "domains", "power"},
        )
        request = _prepare_agent_request(params)
        return {
            "request": request.to_dict(),
            "symbol_context": planner_symbol_context(request),
            "plan_schema": circuit_plan_schema(),
        }
    if method == "agent.plan.compile":
        _exact(params, {"request", "plan"})
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(params["request"]),
            CircuitPlan.from_dict(params["plan"]),
        )
        return _agent_compilation_result(compilation)
    if method == "agent.project.generate":
        _exact(params, {"request", "plan", "output"}, {"retain_failed_attempt"})
        compilation = compile_agent_plan(
            AgentDesignRequest.from_dict(params["request"]),
            CircuitPlan.from_dict(params["plan"]),
        )
        generated = materialize_managed_design(
            compilation.request,
            compilation.design,
            _path(params["output"], "output"),
            graph=compilation.graph,
            plan=compilation.plan,
            retain_failed_attempt=(
                _path(params["retain_failed_attempt"], "retain_failed_attempt")
                if "retain_failed_attempt" in params
                else None
            ),
        )
        return {
            **_project_result(generated.project),
            "plan_review": compilation.review.to_dict(),
        }
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


def _prepare_agent_request(params: dict[str, Any]) -> AgentDesignRequest:
    """Build the model-facing request record without a conversational pre-question."""

    summary = _agent_text(params["request_summary"], "request_summary", 4096)
    name = _agent_text(params["design_name"], "design_name", 256)
    layers = _integer(params["layers"], "layers")
    if layers < 1:
        raise RpcError(-32602, "layers must be a positive integer")
    requested_parts = _agent_text_list(
        params["requested_parts"], "requested_parts", limit=64, item_limit=256
    )
    functions = _agent_text_list(
        params["functions"], "functions", limit=64, item_limit=256
    )
    assumptions = _agent_text_list(
        params.get("assumptions", []), "assumptions", limit=32, item_limit=512
    )
    domains = _agent_text_list(
        params.get("domains", ["simple_control"]),
        "domains",
        limit=32,
        item_limit=128,
    )
    if not domains:
        domains = ["simple_control"]

    board = params.get("board", {})
    if not isinstance(board, Mapping):
        raise RpcError(-32602, "board must be an object")
    if set(board) - {"width_mm", "height_mm"}:
        raise RpcError(-32602, "board contains unsupported fields")
    width = _agent_positive_number(board.get("width_mm", 80.0), "board.width_mm")
    height = _agent_positive_number(board.get("height_mm", 50.0), "board.height_mm")

    power = params.get("power", {})
    if not isinstance(power, Mapping):
        raise RpcError(-32602, "power must be an object")
    if set(power) - {
        "nominal_v",
        "max_voltage_v",
        "max_current_a",
        "max_power_w",
    }:
        raise RpcError(-32602, "power contains unsupported fields")
    nominal_v = _agent_positive_number(power.get("nominal_v", 3.3), "power.nominal_v")
    max_voltage_v = max(
        nominal_v,
        _agent_positive_number(
            power.get("max_voltage_v", nominal_v), "power.max_voltage_v"
        ),
    )
    max_current_a = _agent_positive_number(
        power.get("max_current_a", 0.5), "power.max_current_a"
    )
    max_power_w = _agent_positive_number(
        power.get("max_power_w", nominal_v * max_current_a), "power.max_power_w"
    )

    payload = {
        "summary": summary,
        "name": name,
        "layers": layers,
        "requested_parts": requested_parts,
        "functions": functions,
    }
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "board"
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return AgentDesignRequest.from_dict(
        {
            "schema": "pcbdraft-agent-design-request",
            "version": 1,
            "design_id": f"dsh-{slug[:96].rstrip('-')}-{digest}",
            "name": name,
            "revision": "A",
            "request_summary": summary,
            "scope": {
                "domains": sorted(set(domains)),
                "max_voltage_v": max_voltage_v,
                "max_current_a": max_current_a,
                "max_power_w": max_power_w,
                "layers": layers,
                "intended_use": "User-requested PCB design; no domain validation is implied.",
                "risk_class": "unspecified",
            },
            "board": {
                "width_mm": width,
                "height_mm": height,
                "layers": layers,
                "thickness_mm": 1.6,
                "edge_clearance_mm": 0.5,
                "min_track_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "finish": "enig",
            },
            "assumptions": sorted(set(assumptions)),
            "requested_parts": requested_parts,
            "functions": functions,
            "power": {
                "nominal_v": nominal_v,
                "max_voltage_v": max_voltage_v,
                "max_current_a": max_current_a,
                "max_power_w": max_power_w,
            },
            "source": {"locator": "dsh/pcbdraft", "date": utc_timestamp()[:10]},
        }
    )


def _agent_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise RpcError(-32602, f"{name} must be a string")
    result = sanitize_user_text(value).strip()
    if not result or len(result.encode("utf-8")) > limit:
        raise RpcError(-32602, f"{name} must be non-empty and at most {limit} bytes")
    return result


def _agent_text_list(
    value: Any, name: str, *, limit: int, item_limit: int
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise RpcError(-32602, f"{name} must contain at most {limit} strings")
    result = [_agent_text(item, f"{name} item", item_limit) for item in value]
    if len(set(result)) != len(result):
        raise RpcError(-32602, f"{name} contains duplicate values")
    return sorted(result, key=str.casefold)


def _agent_positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RpcError(-32602, f"{name} must be a number")
    result = float(value)
    if not result > 0:
        raise RpcError(-32602, f"{name} must be positive")
    return result


def _agent_compilation_result(compilation: Any) -> dict[str, Any]:
    """Serialize a generic plan without treating it as a validated release."""

    return {
        "request": compilation.request.to_dict(),
        "plan": compilation.plan.to_dict(),
        "design": compilation.design.to_dict(),
        "part_catalog": compilation.graph.to_dict(),
        "component_qualification": compilation.review.qualification.to_dict(),
        "plan_review": compilation.review.to_dict(),
        "content_hash": compilation.design.content_hash(),
        "assurance": compilation.design.metadata.get("assurance"),
    }


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
