"""Export bounded semantic KiCad evidence for Codex review.

Run through workflows.run_review; requires kicad-cli.
Produces deterministic netlist, board-statistics, and PCB-connectivity artifacts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .blocks import BlockRegistry
from .errors import PcbAgentError, ValidationError
from .io import make_directory, read_bytes_limited
from .managed import MANAGED_MANIFEST, open_managed_project
from .parts import PartGraph
from .process import redact_argv, remaining_timeout, run_command
from .project import ProjectFiles, sha256_file
from .requirements import load_requirements
from .semantic_rules import evaluate_semantic_rules

PROCESS_OUTPUT_LIMIT = 1024 * 1024
NETLIST_LIMIT = 8 * 1024 * 1024
STATS_LIMIT = 2 * 1024 * 1024
IPCD356_LIMIT = 4 * 1024 * 1024
COMPONENT_LIMIT = 500
FIELD_LIMIT = 16
NET_LIMIT = 1_000
NODE_LIMIT = 5_000
IPCD356_RECORD_LIMIT = 2_000
STRING_LIMIT = 512
PROMPT_CONTEXT_LIMIT = 1024 * 1024
MANAGED_CONTEXT_LIMIT = 512 * 1024


def _bounded(value: str | None, limit: int = STRING_LIMIT) -> str:
    return (value or "")[:limit]


def _export(
    *,
    argv: list[str],
    cwd: Path,
    output: Path,
    limit: int,
    deadline: float,
    redactions: Mapping[str, str],
) -> tuple[dict[str, Any], bytes | None]:
    status: dict[str, Any] = {
        "available": False,
        "artifact": output.name,
        "argv": redact_argv(argv, redactions),
        "exit_code": None,
        "duration_seconds": 0.0,
        "failure_kind": None,
    }
    try:
        timeout = remaining_timeout(deadline)
    except PcbAgentError:
        status["failure_kind"] = "timeout"
        return status, None
    try:
        result = run_command(
            argv,
            cwd=cwd,
            timeout=timeout,
            max_output_bytes=PROCESS_OUTPUT_LIMIT,
        )
    except PcbAgentError:
        status["failure_kind"] = "launch_failed"
        return status, None
    status.update(
        exit_code=result.returncode,
        duration_seconds=result.duration_seconds,
    )
    if result.timed_out:
        status["failure_kind"] = "timeout"
        return status, None
    if result.output_limited:
        status["failure_kind"] = "output_limit"
        return status, None
    if result.returncode != 0:
        status["failure_kind"] = "exit_code"
        return status, None
    try:
        output.chmod(0o600)
        data = read_bytes_limited(output, limit)
    except (OSError, PcbAgentError):
        status["failure_kind"] = "missing_or_oversized_output"
        return status, None
    status.update(
        available=True,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return status, data


def _parse_netlist(data: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PcbAgentError("KiCad schematic netlist export is invalid XML") from exc

    title_block = root.find("./design/sheet/title_block")
    design = {
        "title": _bounded(
            title_block.findtext("title") if title_block is not None else None
        ),
        "revision": _bounded(
            title_block.findtext("rev") if title_block is not None else None
        ),
    }

    raw_components = root.findall("./components/comp")
    components: list[dict[str, Any]] = []
    for component in raw_components[:COMPONENT_LIMIT]:
        fields: dict[str, str] = {}
        for field in component.findall("./fields/field")[:FIELD_LIMIT]:
            name = _bounded(field.get("name"), 128)
            if name:
                fields[name] = _bounded(field.text)
        library = component.find("./libsource")
        components.append(
            {
                "reference": _bounded(component.get("ref"), 128),
                "value": _bounded(component.findtext("value")),
                "footprint": _bounded(component.findtext("footprint")),
                "library": _bounded(
                    library.get("lib") if library is not None else None, 256
                ),
                "part": _bounded(
                    library.get("part") if library is not None else None, 256
                ),
                "description": _bounded(
                    library.get("description") if library is not None else None
                ),
                "fields": fields,
            }
        )

    raw_nets = root.findall("./nets/net")
    nets: list[dict[str, Any]] = []
    nodes_seen = 0
    nodes_kept = 0
    for net in raw_nets[:NET_LIMIT]:
        raw_nodes = net.findall("./node")
        nodes_seen += len(raw_nodes)
        remaining = max(0, NODE_LIMIT - nodes_kept)
        nodes = [
            {
                "reference": _bounded(node.get("ref"), 128),
                "pin": _bounded(node.get("pin"), 128),
                "pin_function": _bounded(node.get("pinfunction"), 256),
                "pin_type": _bounded(node.get("pintype"), 128),
            }
            for node in raw_nodes[:remaining]
        ]
        nodes_kept += len(nodes)
        nets.append(
            {
                "name": _bounded(net.get("name")),
                "code": _bounded(net.get("code"), 128),
                "nodes": nodes,
                "node_count": len(raw_nodes),
                "nodes_truncated": len(nodes) < len(raw_nodes),
            }
        )

    return {
        "design": design,
        "components": components,
        "component_count": len(raw_components),
        "components_truncated": len(components) < len(raw_components),
        "nets": nets,
        "net_count": len(raw_nets),
        "nets_truncated": len(nets) < len(raw_nets),
        "nodes_seen_in_kept_nets": nodes_seen,
        "nodes_kept": nodes_kept,
        "nodes_truncated": nodes_kept < nodes_seen,
    }


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= 12:
        return "<depth-limit>"
    if isinstance(value, dict):
        items = list(value.items())[:200]
        rendered = {
            _bounded(str(key), 128): _bounded_json(child, depth=depth + 1)
            for key, child in items
        }
        if len(value) > len(items):
            rendered["<truncated-keys>"] = len(value) - len(items)
        return rendered
    if isinstance(value, list):
        items = value[:500]
        rendered = [_bounded_json(child, depth=depth + 1) for child in items]
        if len(value) > len(items):
            rendered.append({"<truncated-items>": len(value) - len(items)})
        return rendered
    if isinstance(value, str):
        return _bounded(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded(str(value))


def _parse_stats(data: bytes) -> Any:
    try:
        return _bounded_json(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcbAgentError("KiCad board statistics export is invalid JSON") from exc


def _parse_ipcd356(data: bytes) -> dict[str, Any]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PcbAgentError("KiCad IPC-D-356 export is not UTF-8") from exc
    records = [line[:STRING_LIMIT] for line in lines if line.startswith("317")]
    kept = records[:IPCD356_RECORD_LIMIT]
    return {
        "records": kept,
        "record_count": len(records),
        "records_truncated": len(kept) < len(records),
    }


def _context_size(context: Mapping[str, Any]) -> int:
    return len(json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _fit_prompt_context(context: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return _context_size(context)

    for key, list_key in (
        ("board_connectivity", "records"),
        ("schematic", "nets"),
        ("schematic", "components"),
    ):
        section = context.get(key)
        if not isinstance(section, dict):
            continue
        data = section.get("data")
        container = data if isinstance(data, dict) else section
        values = container.get(list_key)
        while (
            size() > PROMPT_CONTEXT_LIMIT
            and isinstance(values, list)
            and len(values) > 1
        ):
            del values[(len(values) + 1) // 2 :]
            container[f"{list_key}_truncated_for_prompt"] = True
        if size() > PROMPT_CONTEXT_LIMIT and isinstance(values, list) and values:
            values.clear()
            container[f"{list_key}_truncated_for_prompt"] = True
    for section_name in ("board_statistics", "board_connectivity", "schematic"):
        if size() <= PROMPT_CONTEXT_LIMIT:
            break
        context[section_name] = {
            "available": False,
            "failure_kind": "prompt_context_limit",
        }
    if size() > PROMPT_CONTEXT_LIMIT:
        raise PcbAgentError("semantic KiCad context exceeds the prompt limit")
    return context


def _managed_semantic_context(project_root: Path) -> dict[str, Any]:
    """Return trusted, bounded design intent for an intact managed project.

    Merely placing JSON files next to a KiCad project does not enable this path:
    ``open_managed_project`` strictly parses the manifest, requirements, and IR,
    validates every managed member, and checks the pinned KiCad compatibility.
    Byte and semantic synchronization are reported separately so reviewers do not
    mistake stale intent for native-board evidence.
    """
    manifest_path = project_root / MANAGED_MANIFEST
    if not manifest_path.exists():
        return {
            "available": False,
            "reason": "not_a_managed_project",
        }
    try:
        project = open_managed_project(project_root)
        requirements = load_requirements(project.requirements_path)
        graph = PartGraph.bundled()
        registry = BlockRegistry.bundled(graph)
        drift = project.drift()
        part_issues = graph.validate_design(project.design, check_libraries=True)
        semantic_findings = evaluate_semantic_rules(
            project.design,
            graph,
            routing=project.manifest["generation"]["pcb"]["routing"],
            approximate_geometry=False,
        )
        used_part_ids = sorted(
            {component.part_id for component in project.design.components}
        )
        used_block_ids = sorted(block.id for block in project.design.blocks)
        block_definitions = {
            definition.id: definition
            for definition in registry.definitions()
            if definition.id in used_block_ids
        }
        generation = project.manifest["generation"]
        context = {
            "available": True,
            "schema": "pcb-agent-managed-review-context",
            "version": 1,
            "authority": project.manifest["sync"].get("authority"),
            "synchronization": {
                "state": "synchronized" if not drift else "drifted",
                "drift": list(drift),
                "manifest_state": project.manifest["sync"].get("state"),
                "kicad_compatibility": project.manifest["sync"].get(
                    "kicad_compatibility"
                ),
            },
            "identity": {
                "design_content_hash": project.design.content_hash(),
                "requirements_sha256": sha256_file(project.requirements_path),
                "ir_sha256": sha256_file(project.ir_path),
                "schematic_sha256": sha256_file(project.schematic_path),
                "board_sha256": sha256_file(project.board_path),
            },
            "requirements": requirements.to_dict(),
            "design_ir": project.design.to_dict(),
            "trusted_parts": {
                "license": graph.license_id,
                "records": [graph.get(part_id).to_dict() for part_id in used_part_ids],
                "contract_issues": [issue.to_dict() for issue in part_issues],
            },
            "verified_blocks": [
                {
                    "id": definition.id,
                    "version": definition.version,
                    "kind": definition.kind,
                    "verification_state": definition.verification_state,
                    "description": definition.description,
                    "constraints": list(definition.constraints),
                    "evidence": list(definition.evidence),
                    "verification_tests": list(definition.verification_tests),
                }
                for definition in (
                    block_definitions[block_id] for block_id in used_block_ids
                )
            ],
            "generation_evidence": {
                "schematic": generation["schematic"],
                "placement": generation["pcb"]["placement"],
                "routing": generation["pcb"]["routing"],
                "constraint_metrics": generation["pcb"].get("constraint_metrics", {}),
                "native_board_snapshot": project.manifest["native_snapshots"]["board"],
            },
            "semantic_rule_evaluation": {
                "method": "deterministic_typed_ir_rules_without_approximate_geometry",
                "outcome": "pass" if not semantic_findings else "fail",
                "findings": [finding.to_dict() for finding in semantic_findings],
            },
            "interpretation": {
                "native_evidence_authoritative": not drift,
                "intent_evidence_authoritative": not drift,
                "physical_or_human_signoff": False,
            },
        }
        if _context_size(context) > MANAGED_CONTEXT_LIMIT:
            raise PcbAgentError(
                f"managed semantic context exceeds {MANAGED_CONTEXT_LIMIT} bytes"
            )
        return context
    except (PcbAgentError, ValidationError, KeyError, TypeError) as exc:
        return {
            "available": False,
            "reason": "invalid_or_unsafe_managed_project",
            "failure": str(exc)[:STRING_LIMIT],
        }


def collect_semantic_context(
    *,
    files: ProjectFiles,
    project_root: Path | None = None,
    output_dir: Path,
    deadline: float,
    redactions: Mapping[str, str],
    executable: str | None = None,
) -> dict[str, Any]:
    """Export and normalize semantic evidence without asking Codex to inspect raw KiCad files."""
    make_directory(output_dir)
    resolved = executable or shutil.which("kicad-cli")
    if not resolved:
        raise PcbAgentError("required executable not found: kicad-cli")

    specs = {
        "schematic_netlist": (
            [
                resolved,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(output_dir / "schematic.netlist.xml"),
                str(files.schematic),
            ],
            files.schematic.parent,
            output_dir / "schematic.netlist.xml",
            NETLIST_LIMIT,
        ),
        "board_statistics": (
            [
                resolved,
                "pcb",
                "export",
                "stats",
                "--format",
                "json",
                "--units",
                "mm",
                "--output",
                str(output_dir / "board-stats.json"),
                str(files.board),
            ],
            files.board.parent,
            output_dir / "board-stats.json",
            STATS_LIMIT,
        ),
        "board_connectivity": (
            [
                resolved,
                "pcb",
                "export",
                "ipcd356",
                "--output",
                str(output_dir / "board-netlist.d356"),
                str(files.board),
            ],
            files.board.parent,
            output_dir / "board-netlist.d356",
            IPCD356_LIMIT,
        ),
    }

    exports: dict[str, Any] = {}
    payloads: dict[str, bytes] = {}
    for name, (argv, cwd, output, limit) in specs.items():
        status, data = _export(
            argv=argv,
            cwd=cwd,
            output=output,
            limit=limit,
            deadline=deadline,
            redactions=redactions,
        )
        exports[name] = status
        if data is not None:
            payloads[name] = data

    context: dict[str, Any] = {
        "context_version": 2,
        "exports": exports,
        "schematic": {"available": False},
        "board_statistics": {"available": False},
        "board_connectivity": {"available": False},
        "managed_project": _managed_semantic_context(project_root)
        if project_root is not None
        else {"available": False, "reason": "project_root_not_supplied"},
    }
    parsers = {
        "schematic_netlist": ("schematic", _parse_netlist),
        "board_statistics": ("board_statistics", _parse_stats),
        "board_connectivity": ("board_connectivity", _parse_ipcd356),
    }
    for export_name, (context_name, parser) in parsers.items():
        data = payloads.get(export_name)
        if data is None:
            context[context_name] = {
                "available": False,
                "failure_kind": exports[export_name]["failure_kind"],
            }
            continue
        try:
            context[context_name] = {"available": True, "data": parser(data)}
        except PcbAgentError as exc:
            exports[export_name]["available"] = False
            exports[export_name]["failure_kind"] = "parse_failed"
            context[context_name] = {"available": False, "failure_kind": str(exc)}

    return _fit_prompt_context(context)
