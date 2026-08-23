"""Immutable BoardBench correction capture and normalized structural diffs.

The raw run tree is always a read-only source.  A correction capture copies the
generated and engineer-corrected snapshots into a fresh campaign-local bundle,
normalizes semantic and native evidence, and writes a source-hashed correction
record without modifying either source tree.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_limited,
    make_directory,
    privatize_tree,
)
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.project import discover_project
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.domain.ir import Component, Design, load_design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.pcb import inspect_native_board
from pcbdraft.kicad.schematic import inspect_native_schematic
from pcbdraft.services.managed import (
    IR_NAME,
    MANAGED_MANIFEST,
    PART_CATALOG_NAME,
    open_managed_project,
)
from pcbdraft.verification.boardbench import (
    ARTIFACT_FILE_LIMIT,
    BOARD_BENCH_LOCK_DIR,
    DIFF_AREAS,
    BoardBenchCorrection,
    BoardBenchReview,
    BoardBenchRun,
    InventoryEntry,
    StructuralDiffEntry,
    artifact_sha256,
    build_inventory,
    canonical_json_bytes,
    load_campaign,
    load_correction,
    load_review,
    load_run,
    write_artifact,
)

CORRECTIONS_DIRECTORY = "corrections"
CORRECTION_RECORD_NAME = "correction.json"
CORRECTION_ARTIFACTS_DIRECTORY = "artifacts"
NORMALIZED_SCHEMA = "pcbdraft-boardbench-normalized-snapshot"
NORMALIZED_VERSION = 1
STRUCTURAL_DIFF_SCHEMA = "pcbdraft-boardbench-structural-diff"
STRUCTURAL_DIFF_VERSION = 1
NORMALIZED_FILE_LIMIT = ARTIFACT_FILE_LIMIT
MAX_NORMALIZED_ENTRIES = 20_000
MAX_NORMALIZED_BYTES = NORMALIZED_FILE_LIMIT - 1

AREA_ORDER = (
    "components",
    "identities",
    "footprints",
    "nets",
    "power_rules",
    "board_geometry",
    "placement",
    "routes",
    "files",
)
if set(AREA_ORDER) != set(DIFF_AREAS):  # pragma: no cover - import-time contract guard
    raise RuntimeError("BoardBench diff areas disagree with the artifact contract")


class NativeSnapshotInspector(Protocol):
    def __call__(
        self, design: Design, schematic: Path, board: Path
    ) -> tuple[Mapping[str, object], Mapping[str, object]]: ...


@dataclass(frozen=True)
class NormalizedSnapshot:
    """Canonical, path-free semantic/native state used for structural comparison."""

    semantic_state: str
    areas: Mapping[str, Mapping[str, object]]

    def __post_init__(self) -> None:
        if self.semantic_state not in {
            "semantic_ir_only",
            "managed_synchronized",
            "managed_drifted",
        }:
            raise ValidationError("normalized snapshot semantic state is invalid")
        if set(self.areas) != set(AREA_ORDER):
            raise ValidationError("normalized snapshot areas are incomplete")
        entry_count = 0
        for area in AREA_ORDER:
            values = self.areas[area]
            if not isinstance(values, Mapping):
                raise ValidationError(f"normalized snapshot {area} must be an object")
            for identity, value in values.items():
                if (
                    not isinstance(identity, str)
                    or not identity
                    or "\x00" in identity
                    or len(identity.encode("utf-8")) > 1_024
                ):
                    raise ValidationError("normalized snapshot identity is invalid")
                canonical_json_bytes({"value": value})
            entry_count += len(values)
        if entry_count > MAX_NORMALIZED_ENTRIES:
            raise ValidationError("normalized snapshot contains too many entries")
        if len(canonical_json_bytes(self.to_dict())) > MAX_NORMALIZED_BYTES:
            raise ValidationError("normalized snapshot exceeds its byte limit")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": NORMALIZED_SCHEMA,
            "version": NORMALIZED_VERSION,
            "semantic_state": self.semantic_state,
            "areas": {
                area: {
                    identity: self.areas[area][identity]
                    for identity in sorted(self.areas[area])
                }
                for area in AREA_ORDER
            },
        }


@dataclass(frozen=True)
class CorrectionCapture:
    """Published correction bundle and its fixed discovery path."""

    root: Path
    record_path: Path
    correction: BoardBenchCorrection


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.expanduser().absolute()
    for component in (absolute, *absolute.parents):
        if component.exists() and component.is_symlink():
            raise ValidationError(f"BoardBench {label} path traverses a symlink")


def _existing_tree(value: str | Path, label: str) -> Path:
    root = _existing_directory(value, label)
    build_inventory(root)
    return root


def _existing_directory(value: str | Path, label: str) -> Path:
    raw = Path(value).expanduser()
    _reject_symlink_components(raw, label)
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"BoardBench {label} snapshot is unavailable") from exc
    if not root.is_dir():
        raise ValidationError(f"BoardBench {label} snapshot must be a directory")
    return root


def _tree_sha256(inventory: tuple[InventoryEntry, ...]) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"files": [item.to_dict() for item in inventory]})
    ).hexdigest()


def _copy_stable_tree(
    source: Path, destination: Path, label: str
) -> tuple[InventoryEntry, ...]:
    before = build_inventory(source)
    try:
        shutil.copytree(source, destination, symlinks=False)
    except OSError as exc:
        raise PCBDraftError(f"cannot copy BoardBench {label} snapshot") from exc
    copied = build_inventory(destination)
    after = build_inventory(source)
    if before != copied or before != after:
        raise ValidationError(f"BoardBench {label} snapshot changed while importing")
    return copied


def _unique_named_file(root: Path, name: str, label: str) -> Path:
    matches = sorted(
        path for path in root.rglob(name) if path.is_file() and not path.is_symlink()
    )
    if len(matches) != 1:
        raise ValidationError(f"BoardBench snapshot needs exactly one {label}")
    return matches[0]


def _default_native_inspector(
    design: Design, schematic: Path, board: Path
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    return inspect_native_schematic(schematic), inspect_native_board(design, board)


def _mapping_list(value: object, path: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or len(value) > MAX_NORMALIZED_ENTRIES:
        raise ValidationError(f"{path} must be a bounded array")
    result: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError(f"{path}[{index}] must be an object")
        canonical_json_bytes({"value": item})
        result.append(item)
    return tuple(result)


def _native_snapshots(
    inspector: NativeSnapshotInspector,
    design: Design,
    schematic: Path,
    board: Path,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    schematic_snapshot, board_snapshot = inspector(design, schematic, board)
    if (
        not isinstance(schematic_snapshot, Mapping)
        or schematic_snapshot.get("schema") != "pcbdraft-schematic-snapshot"
        or schematic_snapshot.get("version") != 1
        or not isinstance(board_snapshot, Mapping)
        or board_snapshot.get("schema") != "pcbdraft-pcbnew-result"
        or board_snapshot.get("version") != 1
        or board_snapshot.get("mode") != "inspect_board"
    ):
        raise ValidationError("native snapshot inspector returned malformed evidence")
    canonical_json_bytes({"schematic": schematic_snapshot, "board": board_snapshot})
    return schematic_snapshot, board_snapshot


def _semantic_state(root: Path, ir_path: Path) -> tuple[str, PartGraph | None]:
    manifests = sorted(
        path
        for path in root.rglob(MANAGED_MANIFEST)
        if path.is_file() and not path.is_symlink()
    )
    if not manifests:
        catalogs = sorted(
            path
            for path in root.rglob(PART_CATALOG_NAME)
            if path.is_file() and not path.is_symlink()
        )
        if len(catalogs) > 1:
            raise ValidationError("BoardBench snapshot has ambiguous part catalogs")
        graph = PartGraph.load(catalogs[0]) if catalogs else None
        return "semantic_ir_only", graph
    if len(manifests) != 1:
        raise ValidationError("BoardBench snapshot has ambiguous managed manifests")
    managed = open_managed_project(manifests[0].parent)
    if managed.ir_path.resolve(strict=True) != ir_path.resolve(strict=True):
        raise ValidationError("managed manifest does not own the normalized IR")
    state = "managed_drifted" if managed.drift() else "managed_synchronized"
    return state, managed.graph


def _component_footprint(
    component: Component, graph: PartGraph | None
) -> tuple[str | None, str | None]:
    override = component.attributes.get("footprint")
    part = graph.get_optional(component.part_id) if graph is not None else None
    footprint = override if isinstance(override, str) and override else None
    if footprint is None and part is not None:
        footprint = part.footprint
    return footprint, part.symbol if part is not None else None


def normalize_snapshot(
    snapshot: str | Path,
    *,
    native_inspector: NativeSnapshotInspector = _default_native_inspector,
) -> NormalizedSnapshot:
    """Normalize one copied project snapshot without retaining absolute paths."""

    root = _existing_tree(snapshot, "normalized")
    ir_path = _unique_named_file(root, IR_NAME, "semantic IR")
    design = load_design(ir_path)
    semantic_state, graph = _semantic_state(root, ir_path)
    project_files = discover_project(root)
    project_path = project_files.schematic.with_suffix(".kicad_pro")
    if project_path.is_symlink() or not project_path.is_file():
        raise ValidationError("BoardBench snapshot needs a matching .kicad_pro file")
    schematic_native, board_native = _native_snapshots(
        native_inspector, design, project_files.schematic, project_files.board
    )

    areas: dict[str, dict[str, object]] = {area: {} for area in AREA_ORDER}
    for component in design.components:
        areas["components"][component.id] = {
            "value": component.value,
            "block_id": component.block_id,
            "attributes": component.attributes,
        }
        footprint, symbol = _component_footprint(component, graph)
        areas["identities"][component.id] = {
            "reference": component.reference,
            "part_id": component.part_id,
            "symbol": symbol,
        }
        areas["footprints"][component.id] = {"footprint": footprint}
        areas["placement"][component.id] = (
            component.placement.to_dict() if component.placement is not None else None
        )
    for net in design.nets:
        areas["nets"][net.id] = net.to_dict()
    for interface in design.interfaces:
        areas["nets"][f"interface:{interface.id}"] = interface.to_dict()
    for domain in design.power_domains:
        areas["power_rules"][f"domain:{domain.id}"] = domain.to_dict()
    for constraint in design.constraints:
        areas["power_rules"][f"constraint:{constraint.id}"] = constraint.to_dict()
    areas["board_geometry"]["semantic-board"] = design.board.to_dict()
    areas["board_geometry"]["semantic-outline"] = [
        point.to_dict() for point in design.native_intent.outline
    ]
    for pose in design.native_intent.footprint_poses:
        areas["placement"][f"native-intent:{pose.component}"] = pose.to_dict()
    for route in design.native_intent.routes:
        areas["routes"][f"semantic-segment:{route.id}"] = route.to_dict()
    for index, via in enumerate(design.native_intent.vias):
        areas["routes"][f"semantic-via:{index:06d}"] = via.to_dict()
    areas["routes"]["semantic-unrouted-nets"] = list(design.native_intent.unrouted_nets)

    schematic_components = _mapping_list(
        schematic_native.get("components"), "native schematic components"
    )
    for native_component in schematic_components:
        reference = native_component.get("reference")
        if not isinstance(reference, str) or not reference:
            raise ValidationError("native schematic component reference is invalid")
        key = f"native-schematic:{reference}"
        areas["components"][key] = {
            "value": native_component.get("value"),
            "symbol": native_component.get("symbol"),
        }
        areas["identities"][key] = native_component.get("properties", {})
        areas["footprints"][key] = {"footprint": native_component.get("footprint")}
        areas["placement"][key] = {
            "position_mm": native_component.get("position_mm"),
            "rotation_deg": native_component.get("rotation_deg"),
        }
    label_names = schematic_native.get("label_names")
    if not isinstance(label_names, list) or not all(
        isinstance(item, str) for item in label_names
    ):
        raise ValidationError("native schematic label evidence is invalid")
    areas["nets"]["native-schematic-labels"] = {
        "names": sorted(label_names),
        "label_count": schematic_native.get("label_count"),
        "no_connect_count": schematic_native.get("no_connect_count"),
    }

    board_components = _mapping_list(
        board_native.get("components"), "native board components"
    )
    for native_component in board_components:
        reference = native_component.get("reference")
        if not isinstance(reference, str) or not reference:
            raise ValidationError("native board component reference is invalid")
        key = f"native-board:{reference}"
        areas["footprints"][key] = {"footprint": native_component.get("footprint")}
        areas["placement"][key] = {
            "x_mm": native_component.get("x_mm"),
            "y_mm": native_component.get("y_mm"),
            "rotation_deg": native_component.get("rotation_deg"),
            "side": native_component.get("side"),
        }
        areas["nets"][key] = {"pads": native_component.get("pads")}
    native_board_rules = board_native.get("board")
    if not isinstance(native_board_rules, Mapping):
        raise ValidationError("native board rule evidence is invalid")
    areas["power_rules"]["native-board-rules"] = native_board_rules
    areas["board_geometry"]["native-board-settings"] = native_board_rules
    for name in ("tracks", "zones"):
        for index, item in enumerate(
            _mapping_list(board_native.get(name), f"native board {name}")
        ):
            areas["routes"][f"native-{name}:{index:06d}"] = item

    inventory = build_inventory(root)
    for entry in inventory:
        areas["files"][entry.path] = {
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
        }
    return NormalizedSnapshot(semantic_state=semantic_state, areas=areas)


def _value_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes({"value": value})).hexdigest()


def diff_snapshots(
    generated: NormalizedSnapshot, corrected: NormalizedSnapshot
) -> tuple[StructuralDiffEntry, ...]:
    """Return a deterministic area/identity diff between two normalized states."""

    changes: list[StructuralDiffEntry] = []
    for area in AREA_ORDER:
        before_values = generated.areas[area]
        after_values = corrected.areas[area]
        for identity in sorted(set(before_values) | set(after_values)):
            before = before_values.get(identity)
            after = after_values.get(identity)
            if identity not in before_values:
                operation = "added"
                before_hash = None
                after_hash = _value_sha256(after)
            elif identity not in after_values:
                operation = "removed"
                before_hash = _value_sha256(before)
                after_hash = None
            else:
                before_hash = _value_sha256(before)
                after_hash = _value_sha256(after)
                if before_hash == after_hash:
                    continue
                operation = "changed"
            changes.append(
                StructuralDiffEntry(
                    area=area,
                    operation=operation,
                    identity=identity,
                    before_sha256=before_hash,
                    after_sha256=after_hash,
                    description=f"{area} {identity} {operation}",
                )
            )
    return tuple(changes)


def _load_normalized(path: Path) -> NormalizedSnapshot:
    value = load_json_limited(path, NORMALIZED_FILE_LIMIT)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "version",
        "semantic_state",
        "areas",
    }:
        raise ValidationError("normalized snapshot artifact is malformed")
    if value["schema"] != NORMALIZED_SCHEMA or value["version"] != NORMALIZED_VERSION:
        raise ValidationError("normalized snapshot schema/version is unsupported")
    areas = value["areas"]
    if not isinstance(areas, dict) or set(areas) != set(AREA_ORDER):
        raise ValidationError("normalized snapshot artifact has incomplete areas")
    return NormalizedSnapshot(
        semantic_state=str(value["semantic_state"]),
        areas={area: areas[area] for area in AREA_ORDER},
    )


def _write_normalized(path: Path, snapshot: NormalizedSnapshot) -> None:
    data = canonical_json_bytes(snapshot.to_dict()) + b"\n"
    if len(data) > NORMALIZED_FILE_LIMIT:  # Defensive against a contract mismatch.
        raise ValidationError("normalized snapshot exceeds its artifact byte limit")
    atomic_write_bytes(path, data, mode=0o600)


def _validate_sources(
    campaign_root: Path,
    run_path: Path,
    review_path: Path,
) -> tuple[BoardBenchRun, BoardBenchReview, str, str, Path]:
    _reject_symlink_components(run_path, "run receipt")
    _reject_symlink_components(review_path, "review")
    campaign = load_campaign(campaign_root / "campaign.json")
    run = load_run(run_path)
    review = load_review(review_path)
    run_hash = artifact_sha256(run)
    review_hash = artifact_sha256(review)
    expected_run_path = campaign_root / "runs" / run.run_id / "run.json"
    if run_path.resolve(strict=True) != expected_run_path.resolve(strict=True):
        raise ValidationError("run receipt is outside its campaign run path")
    expected_review_path = campaign_root / "reviews" / run.run_id / "review.json"
    if review_path.resolve(strict=True) != expected_review_path.resolve(strict=True):
        raise ValidationError(
            "review artifact does not match its canonical campaign path"
        )
    plan = next((item for item in campaign.runs if item.run_id == run.run_id), None)
    if (
        plan is None
        or run.campaign_id != campaign.campaign_id
        or run.case_id != plan.case_id
        or run.repetition != plan.repetition
    ):
        raise ValidationError("correction source run is outside the campaign plan")
    if not run.terminal:
        raise ValidationError("correction capture requires a terminal run")
    if (
        review.campaign_id != run.campaign_id
        or review.run_id != run.run_id
        or review.source_run_sha256 != run_hash
    ):
        raise ValidationError("review artifact does not match its source run")
    if not review.modifications:
        raise ValidationError("correction capture requires modification decisions")
    artifacts = run_path.parent / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise ValidationError("run artifact tree is unavailable")
    if build_inventory(artifacts) != run.inventory:
        raise ValidationError("run artifact tree changed after its terminal receipt")
    return run, review, run_hash, review_hash, artifacts.resolve(strict=True)


def _publish_staging(staging: Path, target: Path) -> None:
    lock_parent = target.parent / BOARD_BENCH_LOCK_DIR
    with ResourceLock(target, lock_parent, timeout=10.0):
        if target.exists() or target.is_symlink():
            raise ValidationError("BoardBench correction bundle already exists")
        try:
            os.rename(staging, target)
        except OSError as exc:
            raise PCBDraftError("cannot publish BoardBench correction bundle") from exc


def capture_correction(
    campaign_root: str | Path,
    *,
    run_receipt_path: str | Path,
    review_path: str | Path,
    generated_snapshot: str | Path,
    corrected_snapshot: str | Path,
    manufacturing_candidate_snapshot: str | Path | None = None,
    created_at: str | None = None,
    native_inspector: NativeSnapshotInspector = _default_native_inspector,
) -> CorrectionCapture:
    """Publish ``corrections/<run_id>/correction.json`` and copied evidence."""

    campaign = _existing_directory(campaign_root, "campaign")
    run_path = Path(run_receipt_path).expanduser()
    review_artifact_path = Path(review_path).expanduser()
    run, review, run_hash, review_hash, run_artifacts = _validate_sources(
        campaign, run_path, review_artifact_path
    )
    generated = _existing_tree(generated_snapshot, "generated")
    corrected = _existing_tree(corrected_snapshot, "corrected")
    if not generated.is_relative_to(run_artifacts):
        raise ValidationError("generated snapshot is not retained by the raw run")
    candidate = (
        None
        if manufacturing_candidate_snapshot is None
        else _existing_tree(manufacturing_candidate_snapshot, "manufacturing candidate")
    )

    corrections_path = campaign / CORRECTIONS_DIRECTORY
    _reject_symlink_components(corrections_path, "corrections")
    corrections = make_directory(corrections_path)
    target = corrections / run.run_id
    if target.exists() or target.is_symlink():
        raise ValidationError("BoardBench correction bundle already exists")
    staging = Path(
        tempfile.mkdtemp(prefix=f".{run.run_id}.capturing-", dir=corrections)
    )
    published = False
    try:
        artifacts = make_directory(staging / CORRECTION_ARTIFACTS_DIRECTORY)
        generated_copy = artifacts / "generated"
        corrected_copy = artifacts / "corrected"
        generated_inventory = _copy_stable_tree(generated, generated_copy, "generated")
        corrected_inventory = _copy_stable_tree(corrected, corrected_copy, "corrected")
        generated_hash = _tree_sha256(generated_inventory)
        corrected_hash = _tree_sha256(corrected_inventory)
        if generated_hash == corrected_hash:
            raise ValidationError(
                "generated and corrected snapshots have an empty diff"
            )

        generated_normalized = normalize_snapshot(
            generated_copy, native_inspector=native_inspector
        )
        corrected_normalized = normalize_snapshot(
            corrected_copy, native_inspector=native_inspector
        )
        changes = diff_snapshots(generated_normalized, corrected_normalized)
        if not changes:
            raise ValidationError(
                "generated and corrected snapshots have an empty diff"
            )
        normalized_root = make_directory(artifacts / "normalized")
        _write_normalized(normalized_root / "generated.json", generated_normalized)
        _write_normalized(normalized_root / "corrected.json", corrected_normalized)
        atomic_write_json(
            artifacts / "structural-diff.json",
            {
                "schema": STRUCTURAL_DIFF_SCHEMA,
                "version": STRUCTURAL_DIFF_VERSION,
                "generated_snapshot_sha256": generated_hash,
                "corrected_snapshot_sha256": corrected_hash,
                "changes": [item.to_dict() for item in changes],
            },
            mode=0o600,
        )
        candidate_hash: str | None = None
        if candidate is not None:
            candidate_inventory = _copy_stable_tree(
                candidate,
                artifacts / "manufacturing-candidate",
                "manufacturing candidate",
            )
            candidate_hash = _tree_sha256(candidate_inventory)

        if build_inventory(run_artifacts) != run.inventory:
            raise ValidationError("raw run changed during correction capture")
        if artifact_sha256(load_run(run_path)) != run_hash:
            raise ValidationError("run receipt changed during correction capture")
        if artifact_sha256(load_review(review_artifact_path)) != review_hash:
            raise ValidationError("review artifact changed during correction capture")

        correction = BoardBenchCorrection(
            campaign_id=run.campaign_id,
            run_id=run.run_id,
            source_run_sha256=run_hash,
            source_review_sha256=review_hash,
            created_at=created_at or utc_timestamp(),
            generated_snapshot_sha256=generated_hash,
            corrected_snapshot_sha256=corrected_hash,
            manufacturing_candidate_sha256=candidate_hash,
            decision_ids=tuple(item.id for item in review.modifications),
            changes=changes,
            inventory=build_inventory(artifacts),
        )
        write_artifact(staging / CORRECTION_RECORD_NAME, correction)
        privatize_tree(staging)
        _publish_staging(staging, target)
        published = True
        return CorrectionCapture(
            root=target,
            record_path=target / CORRECTION_RECORD_NAME,
            correction=correction,
        )
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def verify_correction_bundle(
    bundle_root: str | Path,
    *,
    run_receipt_path: str | Path,
    review_path: str | Path,
) -> BoardBenchCorrection:
    """Verify source bindings, copied snapshots, inventory, and normalized diff."""

    raw_run_path = Path(run_receipt_path).expanduser()
    if raw_run_path.name != "run.json" or raw_run_path.parent.parent.name != "runs":
        raise ValidationError("correction run receipt path is not canonical")
    campaign_root = raw_run_path.parent.parent.parent
    run, review, run_hash, review_hash, _run_artifacts = _validate_sources(
        _existing_directory(campaign_root, "campaign"),
        raw_run_path,
        Path(review_path).expanduser(),
    )
    root = _existing_tree(bundle_root, "correction")
    expected_root = campaign_root / CORRECTIONS_DIRECTORY / run.run_id
    if root != expected_root.resolve(strict=True):
        raise ValidationError(
            "correction bundle is outside its canonical campaign path"
        )
    correction = load_correction(root / CORRECTION_RECORD_NAME)
    if (
        correction.campaign_id != run.campaign_id
        or correction.run_id != run.run_id
        or correction.source_run_sha256 != run_hash
        or correction.source_review_sha256 != review_hash
        or set(correction.decision_ids)
        != {decision.id for decision in review.modifications}
    ):
        raise ValidationError("correction source hash binding is invalid")
    artifacts = root / CORRECTION_ARTIFACTS_DIRECTORY
    if build_inventory(artifacts) != correction.inventory:
        raise ValidationError("correction artifact inventory has changed")
    generated_hash = _tree_sha256(build_inventory(artifacts / "generated"))
    corrected_hash = _tree_sha256(build_inventory(artifacts / "corrected"))
    if (
        generated_hash != correction.generated_snapshot_sha256
        or corrected_hash != correction.corrected_snapshot_sha256
    ):
        raise ValidationError("correction snapshot hash binding is invalid")
    candidate_path = artifacts / "manufacturing-candidate"
    if candidate_path.exists():
        if (
            correction.manufacturing_candidate_sha256 is None
            or _tree_sha256(build_inventory(candidate_path))
            != correction.manufacturing_candidate_sha256
        ):
            raise ValidationError("manufacturing candidate hash binding is invalid")
    elif correction.manufacturing_candidate_sha256 is not None:
        raise ValidationError("manufacturing candidate snapshot is missing")
    generated_normalized = _load_normalized(artifacts / "normalized/generated.json")
    corrected_normalized = _load_normalized(artifacts / "normalized/corrected.json")
    expected_changes = diff_snapshots(generated_normalized, corrected_normalized)
    if expected_changes != correction.changes:
        raise ValidationError("correction normalized diff does not match its record")
    structural_diff = load_json_limited(
        artifacts / "structural-diff.json", NORMALIZED_FILE_LIMIT
    )
    if structural_diff != {
        "schema": STRUCTURAL_DIFF_SCHEMA,
        "version": STRUCTURAL_DIFF_VERSION,
        "generated_snapshot_sha256": generated_hash,
        "corrected_snapshot_sha256": corrected_hash,
        "changes": [item.to_dict() for item in expected_changes],
    }:
        raise ValidationError("correction structural diff artifact is inconsistent")
    return correction
