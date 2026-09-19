#!/usr/bin/env python3
"""Score one private Blue Bridge Cup KiCad pilot attempt.

The input run and answer key are read-only.  A managed project is copied into
the fresh output directory before KiCad is invoked, so validation receipts and
netlist exports cannot alter the runner's raw evidence.

The private answer-key contract is intentionally small and machine readable::

    {
      "schema": "pcbdraft-lq-eda-answer-key",
      "version": 1,
      "task_id": "15th-province-p1-m1",
      "components": [{"ref": "U13", "value": "BCON",
                      "symbol": "LQEDA:BCON",
                      "footprint": "LQEDA:SOP_BCON"}],
      "networks": {"GND": [{"ref": "R18", "pin": "1"}, ...]},
      "pin_policy": {"swappable_references": ["R18", "C13"]},
      "rules": {"layers": 2, "minimum_track_width_mil": 10, ...}
    }

``project_manifest`` may be supplied when a run contains more than one
project.  It is relative to the run root and is never allowed to escape it.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import shutil
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

from defusedxml import ElementTree as ET

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.process import run_command
from pcbdraft.core.project import sha256_file
from pcbdraft.kicad.pcb import inspect_native_board
from pcbdraft.kicad.runtime import find_kicad_cli
from pcbdraft.services.managed import ManagedProject, open_managed_project
from pcbdraft.verification.validation import validate_managed_project

JSON_LIMIT = 32 * 1024 * 1024
XML_LIMIT = 32 * 1024 * 1024
COMMAND_OUTPUT_LIMIT = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 180.0
MIL_TO_MM = 0.0254
ANSWER_SCHEMA = "pcbdraft-lq-eda-answer-key"
SCORE_SCHEMA = "pcbdraft-lq-eda-score"
MANIFEST_NAME = "project.pcbdraft.json"


class _Endpoint(NamedTuple):
    reference: str
    pin: str
    function: str | None = None
    polarity: str | None = None


class _ObservedEndpoint(NamedTuple):
    reference: str
    pin: str
    function: str | None


def _load_json(path: Path, *, limit: int = JSON_LIMIT) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"JSON file is missing or unsafe: {path}")
    if path.stat().st_size > limit:
        raise ValidationError(f"JSON file exceeds the size limit: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return sha256_file(path, max_bytes=max(JSON_LIMIT, XML_LIMIT, 128 * 1024 * 1024))


def _safe_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValidationError(f"{label} directory is missing or unsafe: {path}")
    return path.resolve(strict=True)


def _safe_relative_path(root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or not value or ".." in candidate.parts:
        raise ValidationError(f"{label} must be a relative path inside the run")
    resolved = (root / candidate).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} escapes the run directory") from exc
    return resolved


def _manifest_project_root(manifest: Path) -> Path:
    if (
        manifest.name != MANIFEST_NAME
        or manifest.is_symlink()
        or not manifest.is_file()
    ):
        raise ValidationError(
            f"managed project manifest is missing or unsafe: {manifest}"
        )
    return manifest.parent


def _manifest_pointer(answer_key: Mapping[str, Any]) -> str | None:
    for key in ("project_manifest", "manifest_path", "manifest"):
        value = answer_key.get(key)
        if isinstance(value, str) and value.strip():
            return value
    project = answer_key.get("project")
    if isinstance(project, Mapping):
        for key in ("manifest", "manifest_path"):
            value = project.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _candidate_manifests(run_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    known_parents = (
        run_root,
        run_root / "output" / "repository" / "projects",
        run_root / "repository" / "projects",
        run_root / "artifacts" / "repository" / "projects",
        run_root / "output" / "repository",
        run_root / "artifacts" / "repository",
        run_root / "projects",
    )
    for parent in known_parents:
        if parent.is_symlink() or not parent.is_dir():
            continue
        if parent.name == "projects":
            children = sorted(parent.iterdir(), key=lambda item: item.name)
        else:
            children = [parent]
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            direct = child / MANIFEST_NAME
            design = child / "design" / MANIFEST_NAME
            for manifest in (direct, design):
                if manifest.is_file() and not manifest.is_symlink():
                    candidates.add(manifest.resolve(strict=True))
    return sorted(candidates)


def _locate_project(run_root: Path, answer_key: Mapping[str, Any]) -> Path:
    pointer = _manifest_pointer(answer_key)
    if pointer is not None:
        return _manifest_project_root(
            _safe_relative_path(run_root, pointer, "project manifest")
        )
    candidates = _candidate_manifests(run_root)
    if len(candidates) != 1:
        raise ValidationError(
            "run must identify exactly one managed project; "
            f"found {len(candidates)} candidates"
        )
    return _manifest_project_root(candidates[0])


def _run_receipt(run_root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    for name in ("run.json", "manifest.json"):
        candidate = run_root / name
        if candidate.is_file() and not candidate.is_symlink():
            try:
                return candidate, _load_json(candidate)
            except ValidationError:
                return candidate, None
    return None, None


def _artifact_inventory_check(
    run_root: Path, source_project: Path
) -> tuple[dict[str, Any], str | None, str | None]:
    """Verify the terminal run receipt and hashes for the selected artifact."""

    receipt_path, receipt = _run_receipt(run_root)
    if receipt_path is None or receipt is None:
        return (
            _check(
                "raw_run_receipt",
                None,
                reason="terminal run receipt is missing or malformed",
            ),
            None,
            None,
        )
    result_hash_mismatch: str | None = None
    result = receipt.get("result")
    if isinstance(result, Mapping):
        result_path_value = result.get("path", "result.json")
        result_hash = result.get("sha256")
        if isinstance(result_path_value, str) and isinstance(result_hash, str):
            result_relative = Path(result_path_value)
            result_path = run_root / result_relative
            safe_result_path = (
                not result_relative.is_absolute()
                and ".." not in result_relative.parts
                and result_path.resolve(strict=False).is_relative_to(run_root)
            )
            if (
                not safe_result_path
                or not result_path.is_file()
                or _sha256(result_path) != result_hash
            ):
                result_hash_mismatch = "terminal result receipt hash mismatch"
    status = receipt.get("status")
    if isinstance(result, Mapping):
        # The private runner stores the terminal receipt in manifest.json and
        # nests the immutable result (including its artifact inventory) below
        # ``result``.  Older runner receipts put these fields at the root.
        terminal_status = result.get("status")
        if terminal_status is not None:
            status = terminal_status
    if status != "completed":
        return (
            _check(
                "raw_run_receipt",
                False,
                expected="completed",
                observed=status,
                reason="raw pilot run is not terminal-completed",
            ),
            _sha256(receipt_path),
            None,
        )
    inventory = receipt.get("inventory")
    if inventory is None and isinstance(result, Mapping):
        inventory = result.get("artifacts")
    if inventory is None:
        result_path = run_root / "result.json"
        if result_path.is_file() and not result_path.is_symlink():
            try:
                result_receipt = _load_json(result_path)
            except ValidationError:
                result_receipt = None
            if isinstance(result_receipt, Mapping):
                inventory = result_receipt.get("artifacts")
    inventory_summary = inventory if isinstance(inventory, Mapping) else None
    if isinstance(inventory, Mapping):
        # run-lq-eda-pilot records the immutable artifact records grouped by
        # kind, whereas older receipts used a flat inventory array.
        grouped: list[Mapping[str, Any]] = []
        for key in ("native_artifacts", "board_svgs", "receipts"):
            rows = inventory.get(key)
            if isinstance(rows, list):
                grouped.extend(item for item in rows if isinstance(item, Mapping))
        inventory = grouped
    if not isinstance(inventory, list):
        return (
            _check(
                "raw_run_receipt",
                None,
                reason="terminal run inventory is missing",
            ),
            _sha256(receipt_path),
            None,
        )
    artifact_root = run_root / "artifacts"
    if not artifact_root.is_dir():
        artifact_root = run_root / "output"
    mismatches: list[dict[str, Any]] = []
    if result_hash_mismatch is not None:
        mismatches.append({"reason": result_hash_mismatch})
    if inventory_summary is not None and inventory_summary.get("project_count") != 1:
        mismatches.append(
            {
                "reason": "runner inventory must contain exactly one project",
                "project_count": inventory_summary.get("project_count"),
            }
        )
    canonical_inventory: list[dict[str, Any]] = []
    for index, item in enumerate(inventory):
        if not isinstance(item, Mapping):
            mismatches.append({"index": index, "reason": "inventory row is malformed"})
            continue
        relative = item.get("path")
        expected_hash = item.get("sha256")
        expected_size = item.get("size_bytes", item.get("bytes"))
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(expected_hash, str)
            or not isinstance(expected_size, int)
        ):
            mismatches.append({"index": index, "reason": "inventory row is malformed"})
            continue
        candidate = artifact_root / relative
        if (
            not candidate.exists()
            and (artifact_root / "repository" / relative).exists()
        ):
            candidate = artifact_root / "repository" / relative
        canonical_inventory.append(
            {"path": relative, "sha256": expected_hash, "size_bytes": expected_size}
        )
        if candidate.is_symlink() or not candidate.is_file():
            mismatches.append({"path": relative, "reason": "missing_or_unsafe"})
            continue
        observed_size = candidate.stat().st_size
        observed_hash = _sha256(candidate)
        if observed_size != expected_size or observed_hash != expected_hash:
            mismatches.append(
                {
                    "path": relative,
                    "expected": {"size_bytes": expected_size, "sha256": expected_hash},
                    "observed": {"size_bytes": observed_size, "sha256": observed_hash},
                }
            )
    inventory_bytes = json.dumps(
        sorted(canonical_inventory, key=lambda item: str(item["path"])),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    inventory_hash = hashlib.sha256(inventory_bytes).hexdigest()
    listed_paths = {str(item["path"]) for item in canonical_inventory}
    required_native: list[Path] = []
    if source_project.is_dir():
        required_native = sorted(
            path
            for path in source_project.rglob("*")
            if path.is_file()
            and path.suffix in {".kicad_pro", ".kicad_sch", ".kicad_pcb"}
        )
    missing_native: list[str] = []
    for path in required_native:
        try:
            relative = path.relative_to(artifact_root).as_posix()
        except ValueError:
            missing_native.append(path.name)
            continue
        alternatives = {relative}
        if relative.startswith("repository/"):
            alternatives.add(relative.removeprefix("repository/"))
        if not alternatives & listed_paths:
            missing_native.append(relative)
    if len(required_native) < 3:
        missing_native.append("selected project lacks .kicad_pro/.kicad_sch/.kicad_pcb")
    if missing_native:
        mismatches.append(
            {
                "reason": "selected project native artifacts are not in terminal inventory",
                "missing": missing_native,
            }
        )
    check = _check(
        "raw_run_receipt",
        not mismatches,
        expected="terminal receipt and artifact inventory",
        observed={
            "status": status,
            "inventory_sha256": inventory_hash,
            "mismatches": mismatches[:32],
        },
        reason=None if not mismatches else "raw artifact hash inventory changed",
    )
    del source_project
    return check, _sha256(receipt_path), inventory_hash


def _input_contract_binding_check(
    run_root: Path, answer_key: Mapping[str, Any]
) -> dict[str, Any]:
    binding = answer_key.get("public_input_binding")
    if not isinstance(binding, Mapping):
        return _check(
            "input_contract_binding",
            None,
            reason="answer key has no public_input_binding",
        )
    receipt_path, receipt = _run_receipt(run_root)
    if receipt_path is None or receipt is None:
        return _check(
            "input_contract_binding", None, reason="run manifest is unavailable"
        )
    contract = receipt.get("contract")
    task = receipt.get("task")
    task_records = task.get("input_files") if isinstance(task, Mapping) else None
    recorded: dict[str, str] = {}
    if isinstance(task_records, list):
        for item in task_records:
            if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                value = item.get("sha256")
                if isinstance(value, str):
                    recorded[item["path"]] = value
    mismatches: list[dict[str, Any]] = []
    unknown: list[str] = []

    expected_contract = binding.get("contract_sha256")
    observed_contract = (
        contract.get("sha256") if isinstance(contract, Mapping) else None
    )
    if isinstance(expected_contract, str) and isinstance(observed_contract, str):
        if expected_contract != observed_contract:
            mismatches.append(
                {
                    "field": "contract_sha256",
                    "expected": expected_contract,
                    "observed": observed_contract,
                }
            )
    else:
        unknown.append("contract_sha256")

    expected_prompt = binding.get("prompt_sha256")
    observed_prompts = []
    if isinstance(contract, Mapping) and isinstance(contract.get("prompt_sha256"), str):
        observed_prompts.append(contract["prompt_sha256"])
    if isinstance(task, Mapping) and isinstance(task.get("prompt_sha256"), str):
        observed_prompts.append(task["prompt_sha256"])
    prompt_path = run_root / "input" / "prompt.txt"
    actual_prompt = _sha256(prompt_path) if prompt_path.is_file() else None
    if isinstance(expected_prompt, str):
        for field, observed in (
            (
                "contract.prompt_sha256",
                observed_prompts[0] if observed_prompts else None,
            ),
            (
                "task.prompt_sha256",
                observed_prompts[1] if len(observed_prompts) > 1 else None,
            ),
            ("input/prompt.txt", actual_prompt),
        ):
            if observed is None:
                unknown.append(field)
            elif observed != expected_prompt:
                mismatches.append(
                    {"field": field, "expected": expected_prompt, "observed": observed}
                )
    else:
        unknown.append("prompt_sha256")

    resource_hashes = binding.get("resource_sha256")
    if isinstance(resource_hashes, Mapping):
        for relative, expected in resource_hashes.items():
            if not isinstance(relative, str) or not isinstance(expected, str):
                unknown.append(str(relative))
                continue
            try:
                resource_path = _safe_relative_path(
                    run_root, relative, "input resource"
                )
            except ValidationError:
                mismatches.append({"field": relative, "reason": "unsafe input path"})
                continue
            observed = _sha256(resource_path) if resource_path.is_file() else None
            record_path = relative.removeprefix("input/")
            recorded_hash = recorded.get(record_path)
            for field, value in (
                (relative, observed),
                (f"task.input_files:{record_path}", recorded_hash),
            ):
                if value is None:
                    unknown.append(field)
                elif value != expected:
                    mismatches.append(
                        {"field": field, "expected": expected, "observed": value}
                    )
    else:
        unknown.append("resource_sha256")
    if mismatches:
        status: bool | None = False
    elif unknown:
        status = None
    else:
        status = True
    return _check(
        "input_contract_binding",
        status,
        expected="answer binding equals run contract, prompt, and input resources",
        observed={"mismatches": mismatches, "unknown": unknown},
        reason=(
            "run input binding differs from the private answer key"
            if mismatches
            else "run input binding evidence is incomplete"
            if unknown
            else None
        ),
    )


def _copy_project(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValidationError(f"derived project path is not fresh: {destination}")
    for member in source.rglob("*"):
        if member.is_symlink():
            raise ValidationError(f"raw project contains a symlink: {member}")
    shutil.copytree(source, destination, symlinks=False)


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValidationError(f"{label} must be a string")
    return value


def _component_rows(answer_key: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw = answer_key.get("components", answer_key.get("expected_components"))
    if isinstance(raw, Mapping):
        raw = [
            dict(value, ref=reference) if isinstance(value, Mapping) else value
            for reference, value in raw.items()
        ]
    if not isinstance(raw, list):
        raise ValidationError("answer key components must be an array or object")
    result: dict[str, dict[str, str]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationError(f"components[{index}] must be an object")
        reference = item.get("ref", item.get("reference"))
        row = {
            "reference": _text(reference, f"components[{index}].ref"),
            "value": _text(item.get("value"), f"components[{index}].value", empty=True),
            "symbol": _text(item.get("symbol"), f"components[{index}].symbol"),
            "footprint": _text(item.get("footprint"), f"components[{index}].footprint"),
        }
        if row["reference"] in result:
            raise ValidationError(f"duplicate answer component: {row['reference']}")
        result[row["reference"]] = row
    return result


def _canonical_pin_function(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    # KiCad's XML exporter appends a unit suffix (for example A_2).  The
    # stock connector's meaningful function is itself Pin_1/Pin_2, so only
    # strip a trailing unit when there is already a Pin_N base component.
    pieces = value.split("_")
    if len(pieces) >= 3 and pieces[0].casefold() == "pin" and pieces[-1].isdigit():
        value = "_".join(pieces[:-1])
    elif (
        "_" in value
        and value.rsplit("_", 1)[1].isdigit()
        and not (len(pieces) == 2 and pieces[0].casefold() == "pin")
    ):
        value = value.rsplit("_", 1)[0]
    return value.casefold()


def _canonical_polarity(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().casefold()
    aliases = {
        "anode": "a",
        "cathode": "k",
        "positive": "+",
        "negative": "-",
        "vdd": "vdd",
        "vss": "vss",
    }
    return aliases.get(value, value)


def _endpoint(value: Any, label: str) -> _Endpoint:
    if isinstance(value, str):
        pieces = value.split(".", 1)
        if len(pieces) != 2:
            raise ValidationError(f"{label} must include reference and pin")
        return _Endpoint(_text(pieces[0], label), _text(pieces[1], label))
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an endpoint object")
    reference = value.get("ref", value.get("reference"))
    pin = value.get("pin", value.get("number"))
    return _Endpoint(
        _text(reference, f"{label}.ref"),
        _text(pin, f"{label}.pin"),
        _canonical_pin_function(
            value.get("function", value.get("pin_function", value.get("pinfunction")))
        ),
        _canonical_polarity(value.get("polarity")),
    )


def _expected_networks(
    answer_key: Mapping[str, Any],
) -> dict[str, tuple[_Endpoint, ...]]:
    raw = answer_key.get(
        "networks", answer_key.get("network_endpoints", answer_key.get("expected_nets"))
    )
    if not isinstance(raw, Mapping):
        raise ValidationError("answer key networks must be an object")
    result: dict[str, tuple[_Endpoint, ...]] = {}
    for name, endpoints in raw.items():
        net = _text(name, "network name").lstrip("/")
        if not isinstance(endpoints, list) or not endpoints:
            raise ValidationError(f"network {net} endpoints must be a non-empty array")
        parsed = tuple(
            _endpoint(item, f"networks.{net}[{index}]")
            for index, item in enumerate(endpoints)
        )
        if len(set(parsed)) != len(parsed):
            raise ValidationError(f"network {net} contains duplicate endpoints")
        result[net] = parsed
    if len(result) != len(raw):
        raise ValidationError("answer key networks contain duplicate normalized names")
    return result


def _swappable_references(answer_key: Mapping[str, Any]) -> frozenset[str]:
    values: Any = answer_key.get("pin_swap_components")
    policy = answer_key.get("pin_policy")
    if values is None and isinstance(policy, Mapping):
        values = policy.get("swappable_references", policy.get("pin_swap_components"))
    if values is None:
        values = answer_key.get(
            "non_polarized_references",
            answer_key.get("symmetric_two_terminal_components", []),
        )
    if not isinstance(values, list) or not all(
        isinstance(item, str) and item for item in values
    ):
        raise ValidationError(
            "answer key pin-swap references must be an array of strings"
        )
    return frozenset(values)


def _apply_pin_checks(
    networks: dict[str, tuple[_Endpoint, ...]], answer_key: Mapping[str, Any]
) -> dict[str, tuple[_Endpoint, ...]]:
    """Attach named-pin and polarity obligations to their answer endpoints."""

    raw = answer_key.get("polarized_or_named_pin_checks", {})
    if raw is None:
        return networks
    if not isinstance(raw, Mapping):
        raise ValidationError("polarized_or_named_pin_checks must be an object")
    updates: dict[tuple[str, str], _Endpoint] = {}
    for reference, value in raw.items():
        if not isinstance(reference, str) or not isinstance(value, Mapping):
            raise ValidationError("polarized pin check entries are malformed")
        pin_names = value.get("pin_names")
        if isinstance(pin_names, Mapping):
            for pin, function in pin_names.items():
                updates[(reference, str(pin))] = _Endpoint(
                    reference,
                    str(pin),
                    _canonical_pin_function(_text(function, "pin name")),
                )
        for polarity_name, endpoint_value in value.items():
            if polarity_name not in {"anode", "cathode", "positive", "negative"}:
                continue
            endpoint = _endpoint(endpoint_value, f"{reference}.{polarity_name}")
            updates[(endpoint.reference, endpoint.pin)] = _Endpoint(
                endpoint.reference,
                endpoint.pin,
                endpoint.function,
                _canonical_polarity(polarity_name),
            )
    result: dict[str, tuple[_Endpoint, ...]] = {}
    for name, endpoints in networks.items():
        result[name] = tuple(
            updates.get((endpoint.reference, endpoint.pin), endpoint)
            for endpoint in endpoints
        )
    return result


def _parse_netlist(
    path: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, tuple[_ObservedEndpoint, ...]]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > XML_LIMIT:
        raise ValidationError("exported KiCad XML netlist is missing or unsafe")
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValidationError("netlist XML must not contain external entities")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValidationError("exported KiCad XML netlist is malformed") from exc
    components: dict[str, dict[str, str]] = {}
    components_node = root.find("components")
    if components_node is None:
        raise ValidationError("netlist components are missing")
    for item in components_node.findall("comp"):
        reference = _text(item.attrib.get("ref"), "netlist component reference")
        libsource = item.find("libsource")
        if libsource is None:
            raise ValidationError(
                f"netlist component has no library source: {reference}"
            )
        lib = _text(libsource.attrib.get("lib"), "netlist component library")
        part = _text(libsource.attrib.get("part"), "netlist component symbol")
        value = item.findtext("value")
        footprint = item.findtext("footprint")
        components[reference] = {
            "reference": reference,
            "value": "" if value is None else value,
            "symbol": f"{lib}:{part}",
            "footprint": "" if footprint is None else footprint,
        }
    nets_node = root.find("nets")
    if nets_node is None:
        raise ValidationError("netlist networks are missing")
    networks: dict[str, tuple[_ObservedEndpoint, ...]] = {}
    for net in nets_node.findall("net"):
        name = _text(net.attrib.get("name"), "netlist network name").lstrip("/")
        nodes: list[_ObservedEndpoint] = []
        for node in net.findall("node"):
            nodes.append(
                _ObservedEndpoint(
                    _text(node.attrib.get("ref"), "netlist endpoint reference"),
                    _text(node.attrib.get("pin"), "netlist endpoint pin"),
                    _canonical_pin_function(node.attrib.get("pinfunction")),
                )
            )
        if name in networks:
            raise ValidationError(f"netlist contains duplicate network: {name}")
        networks[name] = tuple(sorted(nodes))
    return components, networks


def _endpoint_matches(expected: _Endpoint, observed: _ObservedEndpoint) -> bool:
    if expected.reference != observed.reference or expected.pin != observed.pin:
        return False
    if expected.function is not None and expected.function != observed.function:
        return False
    if expected.polarity is not None:
        return _canonical_polarity(observed.function) == expected.polarity
    return True


def _compare_networks(
    expected: Mapping[str, tuple[_Endpoint, ...]],
    observed: Mapping[str, tuple[_ObservedEndpoint, ...]],
    swappable: frozenset[str],
) -> tuple[bool | None, dict[str, Any]]:
    if not expected:
        return None, {"reason": "answer key has no endpoint networks"}
    if set(expected) != set(observed):
        return False, {
            "missing_networks": sorted(set(expected) - set(observed)),
            "extra_networks": sorted(set(observed) - set(expected)),
        }
    refs = sorted(swappable)
    if any(
        ref
        not in {endpoint.reference for rows in expected.values() for endpoint in rows}
        for ref in refs
    ):
        return None, {
            "reason": "pin-swap reference is absent from expected network endpoints"
        }
    mismatches: list[dict[str, Any]] = []
    for choices in itertools.product((False, True), repeat=len(refs)):
        mapping = dict(zip(refs, choices, strict=True))
        local: list[dict[str, Any]] = []
        all_match = True
        for name in sorted(expected):
            want = expected[name]
            got = observed[name]
            if len(want) != len(got):
                all_match = False
                local.append(
                    {"network": name, "expected": len(want), "observed": len(got)}
                )
                continue
            transformed: list[_ObservedEndpoint] = []
            for item in got:
                pin = item.pin
                if mapping.get(item.reference, False) and pin in {"1", "2"}:
                    pin = "2" if pin == "1" else "1"
                transformed.append(
                    _ObservedEndpoint(item.reference, pin, item.function)
                )
            remaining = list(transformed)
            for item in want:
                matches = [
                    candidate
                    for candidate in remaining
                    if _endpoint_matches(item, candidate)
                ]
                if not matches:
                    all_match = False
                    local.append(
                        {
                            "network": name,
                            "expected": item._asdict(),
                            "observed": [
                                candidate._asdict() for candidate in transformed
                            ],
                        }
                    )
                else:
                    remaining.remove(matches[0])
            if remaining:
                all_match = False
                local.append(
                    {
                        "network": name,
                        "extra_observed": [item._asdict() for item in remaining],
                    }
                )
        if all_match:
            return True, {"pin_swap_mapping": mapping}
        mismatches.extend(local)
    return False, {"mismatches": mismatches[:64], "pin_swap_references": refs}


def _check(
    name: str,
    passed: bool | None,
    *,
    expected: Any = None,
    observed: Any = None,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "status": "pass"
        if passed is True
        else "fail"
        if passed is False
        else "unknown",
        "passed": passed is True,
    }
    if expected is not None:
        result["expected"] = expected
    if observed is not None:
        result["observed"] = observed
    if reason:
        result["reason"] = reason
    return result


def _component_check(
    expected: Mapping[str, dict[str, str]], observed: Mapping[str, dict[str, str]]
) -> dict[str, Any]:
    if set(expected) != set(observed):
        return _check(
            "components",
            False,
            expected=sorted(expected),
            observed=sorted(observed),
            reason="component reference set differs",
        )
    mismatches = []
    for reference in sorted(expected):
        for field in ("value", "symbol", "footprint"):
            if expected[reference][field] != observed[reference].get(field, ""):
                mismatches.append(
                    {
                        "reference": reference,
                        "field": field,
                        "expected": expected[reference][field],
                        "observed": observed[reference].get(field, ""),
                    }
                )
    return _check(
        "components",
        not mismatches,
        reason=None if not mismatches else "component metadata differs",
        observed=mismatches or "exact",
    )


def _read_project_settings(project: ManagedProject) -> dict[str, Any] | None:
    try:
        return _load_json(project.project_path)
    except ValidationError:
        return None


def _native_rules_check(
    project: ManagedProject, native: Mapping[str, Any], rules: Mapping[str, Any]
) -> dict[str, Any]:
    observed_board = native.get("board")
    if not isinstance(observed_board, Mapping):
        return _check(
            "native_board_rules", None, reason="native board settings are unavailable"
        )
    settings = _read_project_settings(project)
    native_rule_root: Mapping[str, Any] | None = None
    if isinstance(settings, Mapping):
        board = settings.get("board")
        design_settings = (
            board.get("design_settings") if isinstance(board, Mapping) else None
        )
        candidate = (
            design_settings.get("rules")
            if isinstance(design_settings, Mapping)
            else None
        )
        if isinstance(candidate, Mapping):
            native_rule_root = candidate
    mismatches: list[dict[str, Any]] = []
    unknown: list[str] = []
    layers = rules.get("layers")
    if isinstance(layers, int) and not isinstance(layers, bool):
        if observed_board.get("layers") != layers:
            mismatches.append(
                {
                    "field": "layers",
                    "expected": layers,
                    "observed": observed_board.get("layers"),
                }
            )
    elif layers is not None:
        unknown.append("layers")
    conversions = {
        "minimum_track_width_mil": "min_track_width",
        "other_clearance_mil": "min_clearance",
        "pad_to_pad_clearance_mil": "min_clearance",
    }
    for key, native_key in conversions.items():
        expected = rules.get(key)
        if expected is None:
            continue
        if (
            not isinstance(expected, (int, float))
            or isinstance(expected, bool)
            or not math.isfinite(float(expected))
        ):
            unknown.append(key)
            continue
        if native_rule_root is None or native_key not in native_rule_root:
            unknown.append(key)
            continue
        observed = native_rule_root[native_key]
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            unknown.append(key)
            continue
        # A rule is acceptable when the configured minimum is at least the
        # task minimum; weaker rules cannot be rescued by a clean DRC report.
        if float(observed) + 1e-9 < float(expected) * MIL_TO_MM:
            mismatches.append(
                {
                    "field": key,
                    "expected_mm": float(expected) * MIL_TO_MM,
                    "observed_mm": observed,
                }
            )
    if rules.get("pad_to_slot_clearance_mil") is not None:
        unknown.append("pad_to_slot_clearance_mil")
    if mismatches:
        return _check(
            "native_board_rules",
            False,
            expected=dict(rules),
            observed={"unknown": unknown, "mismatches": mismatches},
            reason="native rule is weaker than the task rule",
        )
    if unknown:
        return _check(
            "native_board_rules",
            None,
            expected=dict(rules),
            observed={"unknown": unknown, "mismatches": mismatches},
            reason="required native rule evidence is missing",
        )
    return _check(
        "native_board_rules",
        not mismatches,
        expected=dict(rules),
        observed={"mismatches": mismatches or "satisfied"},
        reason=None if not mismatches else "native rule is weaker than the task rule",
    )


def _board_checks(
    project: ManagedProject, native: Mapping[str, Any], rules: Mapping[str, Any]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(_native_rules_check(project, native, rules))
    components = native.get("components")
    top_required = rules.get("all_components_on")
    if top_required is None:
        top_required = rules.get("components_on")
    if top_required is not None:
        if not isinstance(components, list) or any(
            not isinstance(item, Mapping) for item in components
        ):
            checks.append(
                _check(
                    "top_layer_components",
                    None,
                    reason="native component placement evidence is unavailable",
                )
            )
        else:
            expected_side = (
                "front"
                if str(top_required).casefold() in {"f.cu", "front", "top"}
                else "back"
            )
            observed_sides = {str(item.get("side")) for item in components}
            checks.append(
                _check(
                    "top_layer_components",
                    observed_sides == {expected_side},
                    expected=expected_side,
                    observed=sorted(observed_sides),
                )
            )
    tracks = native.get("tracks")
    if not isinstance(tracks, list):
        checks.append(
            _check("track_width", None, reason="native copper evidence is unavailable")
        )
    else:
        segments = [
            item
            for item in tracks
            if isinstance(item, Mapping) and item.get("kind") == "segment"
        ]
        minimum = rules.get("minimum_track_width_mil")
        if minimum is None:
            checks.append(
                _check(
                    "track_width", None, reason="answer key omits minimum track width"
                )
            )
        elif not segments:
            checks.append(
                _check(
                    "track_width",
                    None,
                    expected=float(minimum) * MIL_TO_MM,
                    reason="no native track segments were observed",
                )
            )
        else:
            widths = [item.get("width_mm") for item in segments]
            if any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                for value in widths
            ):
                checks.append(
                    _check("track_width", None, reason="native track width is missing")
                )
            else:
                minimum_observed = min(float(value) for value in widths)
                checks.append(
                    _check(
                        "track_width",
                        minimum_observed + 1e-9 >= float(minimum) * MIL_TO_MM,
                        expected=float(minimum) * MIL_TO_MM,
                        observed=minimum_observed,
                    )
                )
    vias = (
        [
            item
            for item in tracks
            if isinstance(item, Mapping) and item.get("kind") == "via"
        ]
        if isinstance(tracks, list)
        else []
    )
    for field, key in (
        ("via_outer_diameter", "via_outer_diameter_min_mil"),
        ("via_drill", "via_drill_min_mil"),
    ):
        expected = rules.get(key)
        if expected is None:
            continue
        native_field = "width_mm" if field == "via_outer_diameter" else "drill_mm"
        values = [item.get(native_field) for item in vias]
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in values
        ):
            checks.append(_check(field, None, reason="native via dimension is missing"))
        elif values:
            observed = min(float(value) for value in values)
            checks.append(
                _check(
                    field,
                    observed + 1e-9 >= float(expected) * MIL_TO_MM,
                    expected=float(expected) * MIL_TO_MM,
                    observed=observed,
                )
            )
        else:
            checks.append(
                _check(
                    field,
                    None,
                    expected=float(expected) * MIL_TO_MM,
                    observed="no vias observed",
                    reason="via dimensions cannot be established without a via or native rule evidence",
                )
            )
    connectivity = native.get("connectivity")
    unconnected_expected = rules.get("unconnected_items")
    if unconnected_expected is not None:
        count = (
            connectivity.get("unconnected_count")
            if isinstance(connectivity, Mapping)
            else None
        )
        if isinstance(count, int) and not isinstance(count, bool):
            checks.append(
                _check(
                    "unconnected_items",
                    count == int(unconnected_expected),
                    expected=unconnected_expected,
                    observed=count,
                )
            )
        else:
            checks.append(
                _check(
                    "unconnected_items",
                    None,
                    expected=unconnected_expected,
                    reason="native unconnected-count evidence is unavailable",
                )
            )
    ground_net = str(rules.get("ground_net", "GND")).lstrip("/")
    zone_layers = rules.get("ground_copper_zones")
    if zone_layers is not None:
        zones = native.get("zones")
        if not isinstance(zones, list):
            checks.append(
                _check(
                    "ground_copper_zones",
                    None,
                    reason="native zone evidence is unavailable",
                )
            )
        else:
            found = {
                str(item.get("layer"))
                for item in zones
                if isinstance(item, Mapping)
                and str(item.get("net", "")).lstrip("/") == ground_net
                and item.get("filled") is True
                and isinstance(item.get("area_mm2"), (int, float))
                and float(item["area_mm2"]) > 0
            }
            expected_layers = (
                {str(value) for value in zone_layers}
                if isinstance(zone_layers, list)
                else set()
            )
            checks.append(
                _check(
                    "ground_copper_zones",
                    found >= expected_layers,
                    expected=sorted(expected_layers),
                    observed=sorted(found),
                )
            )
    silkscreen = rules.get("silkscreen")
    if silkscreen is not None:
        try:
            raw = project.board_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            checks.append(
                _check(
                    "top_silkscreen", None, reason="native board text is unavailable"
                )
            )
        else:
            layer = str(silkscreen)
            checks.append(
                _check(
                    "top_silkscreen_presence",
                    f'(layer "{layer}")' in raw,
                    expected=layer,
                    reason="presence check only; text dimensions and complete coverage require human review",
                )
            )
    return checks


@contextmanager
def _scoped_library_environment(library_root: Path) -> Iterator[None]:
    """Expose one run's private KiCad libraries during KiCad checks."""

    paths = {
        "KICAD_SYMBOL_DIR": library_root / "libraries" / "symbols",
        "KICAD10_SYMBOL_DIR": library_root / "libraries" / "symbols",
        "KICAD_FOOTPRINT_DIR": library_root / "libraries" / "footprints",
        "KICAD10_FOOTPRINT_DIR": library_root / "libraries" / "footprints",
    }
    saved = {name: os.environ.get(name) for name in paths}
    try:
        for name, path in paths.items():
            if path.is_dir() and not path.is_symlink():
                os.environ[name] = str(path.resolve(strict=True))
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _export_netlist(project: ManagedProject, output: Path, timeout: float) -> Path:
    executable = find_kicad_cli()
    if executable is None:
        raise PCBDraftError("kicad-cli not found")
    target = output / "netlist.kicadxml"
    result = run_command(
        [
            executable,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "--output",
            str(target),
            str(project.schematic_path),
        ],
        cwd=project.root,
        timeout=timeout,
        max_output_bytes=COMMAND_OUTPUT_LIMIT,
    )
    if result.timed_out or result.output_limited or result.returncode != 0:
        raise PCBDraftError(
            f"KiCad netlist export failed: exit_code_{result.returncode}"
        )
    return target


def _validation_check(
    project: ManagedProject, output: Path, timeout: float
) -> dict[str, Any]:
    try:
        validation = validate_managed_project(
            project, output=output / "validation", timeout=timeout
        )
    except (PCBDraftError, OSError) as exc:
        return _check(
            "managed_validation", None, reason=f"validation unavailable: {exc}"
        )
    report: dict[str, Any] = {}
    receipt: dict[str, Any] = {}
    try:
        report = _load_json(validation.report_path)
        receipt = _load_json(validation.output_dir / "receipt.json")
    except ValidationError as exc:
        return _check("managed_validation", None, reason=str(exc))
    bound = (
        receipt.get("design_content_hash") == project.design.content_hash()
        and receipt.get("source_design_revision") == 0
        and isinstance(report.get("design"), Mapping)
        and report["design"].get("content_hash") == project.design.content_hash()
    )
    passed = (
        validation.candidate_ready is True
        and report.get("readiness", {}).get("engineering_candidate") is True
        and bound
    )
    return _check(
        "managed_validation",
        passed,
        expected="candidate-ready and revision/hash bound",
        observed={
            "candidate_ready": validation.candidate_ready,
            "bound": bound,
            "validation_status": receipt.get("status"),
        },
        reason=None
        if passed
        else "validation did not establish a bound candidate-ready receipt",
    )


def _write_score(
    answer_key: Mapping[str, Any],
    identity: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
    output: Path,
    netlist_path: Path | None,
) -> dict[str, Any]:
    statuses = {str(item.get("status")) for item in checks}
    overall = (
        "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
    )
    score = {
        "schema": SCORE_SCHEMA,
        "version": 1,
        "status": "complete",
        "task_id": answer_key.get("task_id"),
        "overall": {
            "status": overall,
            "passed": overall == "pass",
            "production_ready": False,
        },
        "human_review": {"status": "not_reviewed", "passed": False},
        "identity": dict(identity),
        "checks": list(checks),
        "derived": {
            "project": "project" if (output / "project").is_dir() else None,
            "netlist": "netlist.kicadxml" if netlist_path is not None else None,
            "validation": "validation" if (output / "validation").exists() else None,
        },
        "limitations": [
            "Human engineering review was not performed.",
            "Production readiness and hardware operation are not scored or asserted.",
            "Pad-to-slot clearance is unknown unless an explicit native rule evidence is available.",
            "Top silkscreen is a layer-presence check, not complete placement or dimension review.",
            "Ground-zone checks require a filled positive-area zone on each requested layer, not full-plane continuity.",
            "Managed validation uses the raw run's private library overlay read-only; the derived score does not copy that library tree.",
        ],
    }
    (output / "score.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return score


def score_run(
    run: Path, answer_key_path: Path, output: Path, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ValidationError("timeout must be in (0, 3600]")
    run_root = _safe_directory(run.expanduser(), "run")
    answer_key_path = answer_key_path.expanduser().resolve(strict=True)
    answer_key = _load_json(answer_key_path)
    if answer_key.get("schema") != ANSWER_SCHEMA or answer_key.get("version") != 1:
        raise ValidationError("unsupported LQ-EDA answer-key schema")
    expected_components = _component_rows(answer_key)
    expected_networks = _apply_pin_checks(_expected_networks(answer_key), answer_key)
    swappable = _swappable_references(answer_key)
    rules = answer_key.get("rules")
    if not isinstance(rules, Mapping):
        raise ValidationError("answer key rules must be an object")
    if output.exists() or output.is_symlink():
        raise ValidationError("score output must be fresh and absent")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    binding = answer_key.get("public_input_binding")
    contract_hash = (
        binding.get("contract_sha256") if isinstance(binding, Mapping) else None
    )
    checks: list[dict[str, Any]] = [_input_contract_binding_check(run_root, answer_key)]
    receipt_hash: str | None = None
    inventory_hash: str | None = None
    identity: dict[str, Any] = {
        "answer_key_sha256": _sha256(answer_key_path),
        "input_contract_sha256": contract_hash,
        "answer_binding": dict(binding) if isinstance(binding, Mapping) else None,
    }
    try:
        source_project = _locate_project(run_root, answer_key)
    except (PCBDraftError, ValidationError, OSError) as exc:
        receipt_check, receipt_hash, inventory_hash = _artifact_inventory_check(
            run_root, run_root
        )
        checks.insert(0, receipt_check)
        identity.update(
            {
                "run_receipt_sha256": receipt_hash,
                "artifact_inventory_sha256": inventory_hash,
            }
        )
        checks.append(_check("project_selection", None, reason=str(exc)))
        return _write_score(answer_key, identity, checks, output, None)

    receipt_check, receipt_hash, inventory_hash = _artifact_inventory_check(
        run_root, source_project
    )
    checks.insert(0, receipt_check)
    identity.update(
        {
            "run_receipt_sha256": receipt_hash,
            "artifact_inventory_sha256": inventory_hash,
        }
    )

    copied_project = output / "project"
    try:
        _copy_project(source_project, copied_project)
        project = open_managed_project(copied_project)
        project.assert_synchronized()
        source_manifest = source_project / MANIFEST_NAME
        source_manifest_hash = _sha256(source_manifest)
        copied_manifest_hash = _sha256(project.manifest_path)
    except (PCBDraftError, ValidationError, OSError) as exc:
        checks.append(_check("managed_project", None, reason=str(exc)))
        return _write_score(answer_key, identity, checks, output, None)

    identity.update(
        {
            "source_project_manifest_sha256": source_manifest_hash,
            "derived_project_manifest_sha256": copied_manifest_hash,
            "design_content_hash": project.design.content_hash(),
            "design_revision": project.design.revision,
            "manifest_design_content_hash": project.manifest["design"]["content_hash"],
        }
    )
    checks.append(
        _check(
            "manifest_copy",
            source_manifest_hash == copied_manifest_hash,
            expected=source_manifest_hash,
            observed=copied_manifest_hash,
        )
    )
    netlist_path: Path | None = None
    native: Mapping[str, Any] | None = None
    try:
        with _scoped_library_environment(run_root):
            netlist_path = _export_netlist(project, output, timeout=min(timeout, 120.0))
        identity["netlist_sha256"] = _sha256(netlist_path)
        observed_components, observed_networks = _parse_netlist(netlist_path)
        checks.append(_component_check(expected_components, observed_components))
        network_passed, network_details = _compare_networks(
            expected_networks, observed_networks, swappable
        )
        checks.append(
            _check(
                "netlist_endpoints",
                network_passed,
                expected="complete endpoint sets",
                observed=network_details,
            )
        )
    except (PCBDraftError, ValidationError, OSError) as exc:
        checks.append(_check("netlist_export", None, reason=str(exc)))
    try:
        with _scoped_library_environment(run_root):
            native = inspect_native_board(
                project.design,
                project.board_path,
                include_connectivity=True,
                include_spatial=True,
            )
        checks.extend(_board_checks(project, native, rules))
    except (PCBDraftError, ValidationError, OSError) as exc:
        checks.append(
            _check(
                "native_board",
                None,
                reason=f"native board inspection unavailable: {exc}",
            )
        )
    with _scoped_library_environment(run_root):
        checks.append(_validation_check(project, output, timeout=min(timeout, 180.0)))
    return _write_score(answer_key, identity, checks, output, netlist_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score one private LQ-EDA KiCad pilot run"
    )
    parser.add_argument(
        "--run", required=True, type=Path, help="private pilot run directory"
    )
    parser.add_argument(
        "--answer-key", required=True, type=Path, help="private JSON answer key"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="fresh derived score directory"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"KiCad timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        score = score_run(args.run, args.answer_key, args.output, timeout=args.timeout)
    except (PCBDraftError, OSError) as exc:
        print(f"LQ-EDA scoring failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(score, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
