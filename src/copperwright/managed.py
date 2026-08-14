"""Atomic managed-project orchestration for semantic IR and native KiCad files."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .agent_design import AgentDesignRequest, CircuitPlan
from .blocks import BlockRegistry
from .errors import ValidationError
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
)
from .ir import Design, load_design
from .kicad_pcb import PcbGeneration, generate_pcb, inspect_native_board
from .kicad_schematic import (
    SchematicGeneration,
    generate_schematic,
    inspect_native_schematic,
)
from .kicad_support import assert_supported_kicad_version
from .locking import ResourceLock
from .parts import PartGraph
from .project import canonical_project, sha256_file, validate_agent_tree
from .requirements import RequirementsSpec, compile_requirements, load_requirements

MANAGED_SCHEMA = "copperwright-managed-project"
MANAGED_VERSION = 1
MANAGED_MANIFEST = "project.copperwright.json"
MANAGED_MANIFEST_LIMIT = 16 * 1024 * 1024
REQUIREMENTS_NAME = "requirements.pcbreq.json"
IR_NAME = "design.pcbir.json"
PART_CATALOG_NAME = "parts.copperwright.json"
CIRCUIT_PLAN_NAME = "circuit-plan.json"
GenerationRequest = RequirementsSpec | AgentDesignRequest


@dataclass(frozen=True)
class ManagedProject:
    root: Path
    requirements_path: Path
    ir_path: Path
    schematic_path: Path
    board_path: Path
    project_path: Path
    manifest_path: Path
    plan_path: Path | None
    design: Design
    graph: PartGraph
    plan: CircuitPlan | None
    manifest: dict[str, Any]

    def drift(self) -> tuple[str, ...]:
        """Return tracked files whose bytes no longer match the sync manifest."""
        differences: list[str] = []
        hashes = self.manifest["hashes"]
        for name, relative in self.manifest["files"].items():
            if name == "manifest":
                continue
            path = self.root / relative
            if path.is_symlink() or not path.is_file():
                differences.append(f"{name}:missing_or_unsafe")
                continue
            expected = hashes.get(name)
            actual = sha256_file(path, max_bytes=128 * 1024 * 1024)
            if actual != expected:
                differences.append(f"{name}:hash_mismatch")
        if self.design.content_hash() != self.manifest["design"]["content_hash"]:
            differences.append("ir:semantic_hash_mismatch")
        return tuple(sorted(set(differences)))

    def assert_synchronized(self) -> None:
        differences = self.drift()
        if differences:
            raise ValidationError(
                "managed project drifted from its synchronization manifest: "
                + ", ".join(differences)
            )


@dataclass(frozen=True)
class ManagedGeneration:
    project: ManagedProject
    schematic: SchematicGeneration
    pcb: PcbGeneration


def generate_managed_project(
    requirements: RequirementsSpec | str | Path,
    output: str | Path,
    *,
    graph: PartGraph | None = None,
    registry: BlockRegistry | None = None,
    system_python: str | Path | None = None,
    retain_failed_attempt: str | Path | None = None,
    lock_timeout: float = 10.0,
) -> ManagedGeneration:
    """Compile a new project in a sibling staging directory, then publish atomically.

    Existing output paths are never overwritten.  A failed compiler, router, or
    KiCad backend leaves no partially published project.
    """
    spec = (
        requirements
        if isinstance(requirements, RequirementsSpec)
        else load_requirements(requirements)
    )
    resolved_graph = graph or PartGraph.bundled()
    resolved_registry = registry or BlockRegistry.bundled(resolved_graph)
    design = compile_requirements(
        spec,
        graph=resolved_graph,
        registry=resolved_registry,
        check_libraries=True,
    )
    return materialize_managed_design(
        spec,
        design,
        output,
        graph=resolved_graph,
        system_python=system_python,
        retain_failed_attempt=retain_failed_attempt,
        lock_timeout=lock_timeout,
    )


def materialize_managed_design(
    requirements: GenerationRequest,
    design: Design,
    output: str | Path,
    *,
    graph: PartGraph | None = None,
    plan: CircuitPlan | None = None,
    system_python: str | Path | None = None,
    retain_failed_attempt: str | Path | None = None,
    lock_timeout: float = 10.0,
) -> ManagedGeneration:
    """Publish an already-validated semantic design as a new managed project."""
    resolved_graph = graph or PartGraph.bundled()
    design.assert_valid()
    expected_requirements_hash = hashlib.sha256(
        requirements.canonical_bytes()
    ).hexdigest()
    if design.metadata.get("requirements_hash") != expected_requirements_hash:
        raise ValidationError(
            "semantic design does not originate from the supplied requirements"
        )
    if isinstance(requirements, AgentDesignRequest):
        if plan is None:
            raise ValidationError(
                "generic managed generation requires the reviewed circuit plan"
            )
        if (
            plan.design_id != requirements.design_id
            or plan.design_id != design.design_id
        ):
            raise ValidationError(
                "reviewed circuit plan identity does not match the generic design"
            )
        expected_plan_hash = hashlib.sha256(plan.canonical_bytes()).hexdigest()
        if design.metadata.get("plan_hash") != expected_plan_hash:
            raise ValidationError(
                "semantic design does not originate from the reviewed circuit plan"
            )
    elif plan is not None:
        raise ValidationError(
            "legacy requirements generation cannot include a generic circuit plan"
        )
    raw_target = Path(output).expanduser()
    if raw_target.name in {"", ".", ".."} or raw_target.is_symlink():
        raise ValidationError("managed project output path is unsafe")
    raw_parent = raw_target.parent
    try:
        raw_parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationError("cannot create managed project output parent") from exc
    try:
        parent = raw_parent.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("managed project output parent is unavailable") from exc
    target = parent / raw_target.name
    retained_target: Path | None = None
    if retain_failed_attempt is not None:
        raw_retained = Path(retain_failed_attempt).expanduser()
        if (
            raw_retained.name in {"", ".", ".."}
            or raw_retained.is_symlink()
            or raw_retained.exists()
        ):
            raise ValidationError("failed-attempt retention path is unsafe or occupied")
        try:
            retained_parent = raw_retained.parent.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(
                "failed-attempt retention parent is unavailable"
            ) from exc
        retained_target = retained_parent / raw_retained.name

    with ResourceLock(target, parent / ".copperwright-locks", timeout=lock_timeout):
        if target.exists() or target.is_symlink():
            raise ValidationError("managed project output already exists")
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.building-", dir=parent)
        )
        os.chmod(temporary, 0o700)
        published = False
        try:
            requirements_path = temporary / REQUIREMENTS_NAME
            ir_path = temporary / IR_NAME
            atomic_write_bytes(
                requirements_path, requirements.canonical_bytes(), mode=0o644
            )
            atomic_write_bytes(ir_path, design.canonical_bytes(), mode=0o644)
            part_catalog_path = temporary / PART_CATALOG_NAME
            atomic_write_json(part_catalog_path, resolved_graph.to_dict(), mode=0o644)
            if plan is not None:
                atomic_write_bytes(
                    temporary / CIRCUIT_PLAN_NAME, plan.canonical_bytes(), mode=0o644
                )

            stem = design.design_id
            schematic = generate_schematic(
                design, temporary / f"{stem}.kicad_sch", graph=resolved_graph
            )
            pcb = generate_pcb(
                design,
                temporary / f"{stem}.kicad_pcb",
                graph=resolved_graph,
                system_python=system_python,
            )
            files = {
                "manifest": MANAGED_MANIFEST,
                "requirements": REQUIREMENTS_NAME,
                "ir": IR_NAME,
                "part_catalog": PART_CATALOG_NAME,
                "schematic": schematic.path.name,
                "board": pcb.path.name,
                "kicad_project": pcb.project_path.name,
                "worker_receipt": pcb.worker_receipt.name,
            }
            if plan is not None:
                files["circuit_plan"] = CIRCUIT_PLAN_NAME
            hashes = {
                name: sha256_file(temporary / relative, max_bytes=128 * 1024 * 1024)
                for name, relative in files.items()
                if name != "manifest"
            }
            manifest = {
                "schema": MANAGED_SCHEMA,
                "version": MANAGED_VERSION,
                "runtime_version": __version__,
                "design": {
                    "id": design.design_id,
                    "name": design.name,
                    "revision": design.revision,
                    "content_hash": design.content_hash(),
                },
                "files": files,
                "hashes": hashes,
                "generation": {
                    "schematic": schematic.to_dict(),
                    "pcb": pcb.to_dict(),
                },
                "native_snapshots": {
                    "schematic": inspect_native_schematic(schematic.path),
                    "board": inspect_native_board(
                        design, pcb.path, system_python=system_python
                    ),
                    "project": inspect_native_project(pcb.project_path),
                },
                "sync": {
                    "authority": "semantic_ir",
                    "state": "synchronized",
                    "kicad_support": pcb.kicad_version,
                },
            }
            _relativize_generation_paths(manifest["generation"])
            atomic_write_json(temporary / MANAGED_MANIFEST, manifest, mode=0o644)
            for member in temporary.iterdir():
                if member.is_file():
                    member.chmod(0o644)
            temporary.chmod(0o755)
            os.rename(temporary, target)
            published = True
            _fsync_parent(parent)
        except BaseException:
            if not published:
                retained = False
                if retained_target is not None:
                    try:
                        os.rename(temporary, retained_target)
                        _fsync_parent(retained_target.parent)
                        retained = True
                    except OSError:
                        retained = False
                if not retained:
                    _remove_private_staging(temporary, parent)
            raise

    project = open_managed_project(target)
    return ManagedGeneration(project=project, schematic=schematic, pcb=pcb)


def open_managed_project(value: str | Path) -> ManagedProject:
    root = canonical_project(value)
    validate_agent_tree(root)
    manifest_path = root / MANAGED_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValidationError("managed project manifest is missing or unsafe")
    manifest = load_json_limited(manifest_path, MANAGED_MANIFEST_LIMIT)
    _validate_manifest(manifest)
    assert_supported_kicad_version(str(manifest["sync"].get("kicad_support", "")))
    files = manifest["files"]
    paths = {name: _managed_member(root, relative) for name, relative in files.items()}
    design = load_design(paths["ir"])
    graph = (
        PartGraph.load(paths["part_catalog"])
        if "part_catalog" in paths
        else PartGraph.bundled()
    )
    generation_request = load_generation_request(paths["requirements"])
    plan_path = paths.get("circuit_plan")
    plan = (
        CircuitPlan.from_dict(load_json_limited(plan_path, MANAGED_MANIFEST_LIMIT))
        if plan_path is not None
        else None
    )
    if isinstance(generation_request, AgentDesignRequest):
        if plan is not None:
            if (
                plan.design_id != generation_request.design_id
                or plan.design_id != design.design_id
            ):
                raise ValidationError(
                    "managed circuit plan identity does not match the generic design"
                )
            plan_hash = hashlib.sha256(plan.canonical_bytes()).hexdigest()
            if design.metadata.get("plan_hash") != plan_hash:
                raise ValidationError(
                    "managed circuit plan does not match semantic design provenance"
                )
    elif plan is not None:
        raise ValidationError(
            "legacy managed project cannot contain a generic circuit plan"
        )
    project = ManagedProject(
        root=root,
        requirements_path=paths["requirements"],
        ir_path=paths["ir"],
        schematic_path=paths["schematic"],
        board_path=paths["board"],
        project_path=paths["kicad_project"],
        manifest_path=manifest_path,
        plan_path=plan_path,
        design=design,
        graph=graph,
        plan=plan,
        manifest=manifest,
    )
    return project


def _validate_manifest(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "version",
        "runtime_version",
        "design",
        "files",
        "hashes",
        "generation",
        "native_snapshots",
        "sync",
    }:
        raise ValidationError("managed project manifest fields are malformed")
    if value["schema"] != MANAGED_SCHEMA or value["version"] != MANAGED_VERSION:
        raise ValidationError("unsupported managed project manifest")
    legacy_files = {
        "manifest",
        "requirements",
        "ir",
        "schematic",
        "board",
        "kicad_project",
        "worker_receipt",
    }
    project_local_files = legacy_files | {"part_catalog"}
    generic_files = project_local_files | {"circuit_plan"}
    if not isinstance(value["files"], dict) or set(value["files"]) not in (
        legacy_files,
        project_local_files,
        generic_files,
    ):
        raise ValidationError("managed project file map is malformed")
    if not isinstance(value["hashes"], dict) or set(value["hashes"]) != set(
        value["files"]
    ) - {"manifest"}:
        raise ValidationError("managed project hash map is malformed")
    for digest in value["hashes"].values():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError("managed project contains an invalid hash")
    design = value["design"]
    if not isinstance(design, dict) or set(design) != {
        "id",
        "name",
        "revision",
        "content_hash",
    }:
        raise ValidationError("managed project design identity is malformed")
    if not all(isinstance(item, str) and item for item in design.values()):
        raise ValidationError("managed project design identity is incomplete")
    if not isinstance(value["generation"], dict) or not isinstance(value["sync"], dict):
        raise ValidationError("managed project generation/sync record is malformed")
    if not isinstance(value["native_snapshots"], dict) or set(
        value["native_snapshots"]
    ) != {"schematic", "board", "project"}:
        raise ValidationError("managed project native snapshots are malformed")


def _managed_member(root: Path, relative: Any) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or len(Path(relative).parts) != 1
        or relative in {".", ".."}
    ):
        raise ValidationError("managed project contains an unsafe file reference")
    member = root / relative
    try:
        info = member.lstat()
    except OSError as exc:
        raise ValidationError(f"managed project file is missing: {relative}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or member.is_symlink():
        raise ValidationError(f"managed project file is unsafe: {relative}")
    return member


def _relativize_generation_paths(generation: dict[str, Any]) -> None:
    generation["schematic"]["path"] = Path(generation["schematic"]["path"]).name
    pcb = generation["pcb"]
    for field in ("path", "project_path", "worker_receipt"):
        pcb[field] = Path(pcb[field]).name


def inspect_native_project(path: str | Path) -> dict[str, Any]:
    """Capture deterministic KiCad project settings, excluding inferred sheet UI data."""
    source = Path(path)
    if source.suffix != ".kicad_pro" or source.is_symlink():
        raise ValidationError("project inspection requires a non-symlink .kicad_pro")
    document = load_json_limited(source.resolve(strict=True), MANAGED_MANIFEST_LIMIT)
    if not isinstance(document, dict):
        raise ValidationError("KiCad project settings must be a JSON object")
    schematic = document.get("schematic")
    if isinstance(schematic, dict):
        # pcbnew may infer this cache on save without changing any design rule.
        schematic.pop("top_level_sheets", None)
    return document


def _remove_private_staging(temporary: Path, parent: Path) -> None:
    try:
        resolved = temporary.resolve(strict=False)
        if resolved.parent == parent and resolved.name.startswith("."):
            shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        pass


def _fsync_parent(parent: Path) -> None:
    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_generation_request(path: str | Path) -> GenerationRequest:
    """Parse either the legacy fixture requirements or a generic agent request."""

    value = load_json_limited(path, MANAGED_MANIFEST_LIMIT)
    if not isinstance(value, dict):
        raise ValidationError("managed project requirements must be an object")
    schema = value.get("schema")
    if schema == "copperwright-requirements":
        return RequirementsSpec.from_dict(value)
    if schema == "copperwright-agent-design-request":
        return AgentDesignRequest.from_dict(value)
    raise ValidationError(
        "managed project contains an unknown generation request schema"
    )


def requirements_hash(spec: GenerationRequest) -> str:
    return hashlib.sha256(spec.canonical_bytes()).hexdigest()
