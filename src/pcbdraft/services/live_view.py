"""Bounded, read-only projections for the resident local GUI.

The live view deliberately sits beside :class:`ApplicationService` rather than
inside it.  ApplicationService remains the sole write authority; this module
only takes the same project lock, reads one committed managed project, and
projects a small allowlisted scene.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import load_json_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.project import sha256_file
from pcbdraft.domain.ir import Design, canonical_json_bytes
from pcbdraft.kicad.previews import PREVIEW_MAX_BYTES, PreviewBundle, generate_preview
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.managed import ManagedProject, open_managed_project

LIVE_SCENE_SCHEMA = "pcbdraft-live-board-scene"
LIVE_SCENE_VERSION = 1
LIVE_DIFF_SCHEMA = "pcbdraft-live-board-diff"
LIVE_DIFF_VERSION = 1
MAX_PREVIEW_RECEIPT_BYTES = 128 * 1024

_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_CONTENT_HASH = re.compile(r"[0-9a-f]{64}")
_PREVIEW_KINDS = {
    "board_svg": ("render_board", "board_svg", "board.svg"),
    "board_3d": ("render_3d", "board_render", "board-top.png"),
}


@dataclass(frozen=True)
class SceneLimits:
    """Hard response limits applied before a scene can leave the backend."""

    max_footprints: int = 500
    max_routes: int = 20_000
    max_vias: int = 10_000
    max_unrouted_nets: int = 2_000
    max_unrouted_endpoints: int = 10_000
    max_outline_segments: int = 64
    max_layers: int = 32
    max_response_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.max_footprints,
            self.max_routes,
            self.max_vias,
            self.max_unrouted_nets,
            self.max_unrouted_endpoints,
            self.max_outline_segments,
            self.max_layers,
            self.max_response_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValidationError("live scene limits must be non-negative integers")
        if self.max_response_bytes == 0:
            raise ValidationError("live scene response limit must be positive")


ManagedLoader = Callable[[str | Path], ManagedProject]
PreviewGenerator = Callable[..., PreviewBundle]


class LiveViewService:
    """Project-bound, non-mutating live-board scene reader."""

    def __init__(
        self,
        service: ApplicationService,
        *,
        limits: SceneLimits | None = None,
        managed_loader: ManagedLoader = open_managed_project,
    ) -> None:
        self.service = service
        self.limits = limits or SceneLimits()
        self._managed_loader = managed_loader

    def snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        """Return one committed scene, or ``None`` while its writer is busy."""

        # Use the application's supported lock-aware read first.  We then
        # reacquire the same resource lock and read the public view again while
        # keeping that lock around the managed Design/native snapshot.  A
        # writer winning the small gap simply produces another busy ``None``.
        if self.service.try_open_project_snapshot(project_id, timeout=timeout) is None:
            return None
        root = _validated_project_root(self.service, project_id)
        lock = _try_project_lock(root, self.service.locks_root, timeout=0.0)
        if lock is None:
            return None
        try:
            view = self.service.open_project(project_id)
            design_root = root / "design"
            if design_root.is_symlink():
                raise ValidationError("managed design path is unsafe")
            if not design_root.is_dir():
                return _draft_scene(view)
            managed = self._managed_loader(design_root)
            managed.assert_synchronized()
            scene = _project_scene(view, managed, self.limits)
            if len(canonical_json_bytes(scene)) > self.limits.max_response_bytes:
                raise ValidationError(
                    "live board scene exceeds its response size limit"
                )
            return scene
        finally:
            lock.release()

    @staticmethod
    def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        """Return stable object identities needed for frontend diff animation."""

        return diff_scenes(before, after)


class ExactPreviewCache:
    """Content-addressed KiCad previews stored outside PCB projects."""

    def __init__(
        self,
        service: ApplicationService,
        cache_root: str | Path | None = None,
        *,
        generator: PreviewGenerator = generate_preview,
        managed_loader: ManagedLoader = open_managed_project,
    ) -> None:
        self.service = service
        self.cache_root = Path(
            cache_root or (Path.home() / ".cache/pcbdraft/gui")
        ).expanduser()
        self._generator = generator
        self._managed_loader = managed_loader

    def artifact(
        self,
        project_id: str,
        kind: str,
        *,
        generate: bool = False,
        timeout: float = 90.0,
    ) -> Path | None:
        """Return a verified fixed artifact, optionally generating a cache miss.

        ``kind`` is an allowlisted logical identity, never a caller-controlled
        relative path.  ``board_3d`` is generated only when ``generate=True``.
        """

        if kind not in _PREVIEW_KINDS:
            raise ValidationError("unknown exact preview kind")
        if timeout <= 0 or timeout > 600:
            raise ValidationError("preview timeout must be in (0, 600] seconds")
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
            raise ValidationError("application project id is invalid")

        cache_root = _private_cache_root(self.cache_root)
        root = _validated_project_root(self.service, project_id)
        lock = _try_project_lock(root, self.service.locks_root, timeout=0.0)
        if lock is None:
            return None

        staging: Path | None = None
        content_hash: str
        try:
            design_root = root / "design"
            if design_root.is_symlink() or not design_root.is_dir():
                return None
            managed = self._managed_loader(design_root)
            managed.assert_synchronized()
            content_hash = managed.design.content_hash()
            _validate_content_hash(content_hash)
            cached = self._cached_artifact(cache_root, project_id, content_hash, kind)
            if cached is not None or not generate:
                return cached

            staging_root = _private_child(cache_root, ".staging")
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f"{project_id}-{content_hash[:12]}-", dir=staging_root
                )
            )
            os.chmod(staging, 0o700)
            copied = staging / "source"
            if managed.root.resolve(strict=True) != design_root.resolve(strict=True):
                raise ValidationError(
                    "managed design root does not match the selected project"
                )
            _copy_managed_snapshot(managed, copied)
        finally:
            lock.release()

        if staging is None:  # pragma: no cover - guarded by the cache-miss branch
            raise PCBDraftError("exact preview staging was not initialized")
        try:
            copied_managed = self._managed_loader(staging / "source")
            copied_managed.assert_synchronized()
            if copied_managed.design.content_hash() != content_hash:
                raise ValidationError("copied preview source changed unexpectedly")
            render_kind, _file_key, _filename = _PREVIEW_KINDS[kind]
            output = staging / "output"
            bundle = self._generator(
                copied_managed,
                output,
                render_kind,
                timeout=timeout,
            )
            if bundle.design_content_hash != content_hash:
                raise ValidationError(
                    "preview generator returned a mismatched design hash"
                )
            generated = _verified_artifact(output, content_hash, kind)
            expected = bundle.files.get(_PREVIEW_KINDS[kind][1])
            if expected is None or expected.resolve(strict=True) != generated.resolve(
                strict=True
            ):
                raise ValidationError(
                    "preview generator returned an unexpected artifact"
                )

            parent = _cache_hash_parent(cache_root, project_id, content_hash)
            destination = parent / kind
            if destination.exists() or destination.is_symlink():
                existing = self._cached_artifact(
                    cache_root, project_id, content_hash, kind
                )
                if existing is None:
                    raise ValidationError(
                        "existing exact preview cache entry is invalid"
                    )
            else:
                try:
                    os.replace(output, destination)
                except OSError as exc:
                    # Another request may have atomically published the same
                    # content-addressed artifact while KiCad was rendering.
                    existing = self._cached_artifact(
                        cache_root, project_id, content_hash, kind
                    )
                    if existing is None:
                        raise PCBDraftError(
                            "cannot publish exact preview cache entry"
                        ) from exc
                else:
                    existing = _verified_artifact(destination, content_hash, kind)

            current_hash = self._current_content_hash(project_id)
            return existing if current_hash == content_hash else None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _cached_artifact(
        self, cache_root: Path, project_id: str, content_hash: str, kind: str
    ) -> Path | None:
        parent = cache_root / project_id / content_hash
        directory = parent / kind
        if not directory.exists():
            return None
        if directory.is_symlink() or not directory.is_dir():
            raise ValidationError("exact preview cache path is unsafe")
        return _verified_artifact(directory, content_hash, kind)

    def _current_content_hash(self, project_id: str) -> str | None:
        root = _validated_project_root(self.service, project_id)
        lock = _try_project_lock(root, self.service.locks_root, timeout=0.0)
        if lock is None:
            return None
        try:
            design_root = root / "design"
            if design_root.is_symlink() or not design_root.is_dir():
                return None
            managed = self._managed_loader(design_root)
            managed.assert_synchronized()
            return managed.design.content_hash()
        finally:
            lock.release()


def diff_scenes(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compute a compact, deterministic diff between two bounded scenes."""

    if (
        before.get("schema") != LIVE_SCENE_SCHEMA
        or after.get("schema") != LIVE_SCENE_SCHEMA
    ):
        raise ValidationError("live scene diff requires supported scene objects")
    footprint_diff = _collection_diff(before, after, "footprints")
    moved = []
    old_footprints = _rows_by_id(before.get("footprints"), "footprints")
    new_footprints = _rows_by_id(after.get("footprints"), "footprints")
    pose_fields = ("x_mm", "y_mm", "rotation_deg", "side", "placed")
    for identity in sorted(old_footprints.keys() & new_footprints.keys()):
        if any(
            old_footprints[identity].get(field) != new_footprints[identity].get(field)
            for field in pose_fields
        ):
            moved.append(identity)
    footprint_diff["moved"] = moved
    return {
        "schema": LIVE_DIFF_SCHEMA,
        "version": LIVE_DIFF_VERSION,
        "from_content_hash": before.get("content_hash"),
        "to_content_hash": after.get("content_hash"),
        "from_geometry_revision": before.get("geometry_revision"),
        "to_geometry_revision": after.get("geometry_revision"),
        "board_changed": before.get("board") != after.get("board"),
        "footprints": footprint_diff,
        "routes": _collection_diff(before, after, "routes"),
        "vias": _collection_diff(before, after, "vias"),
        "unrouted_nets": _collection_diff(before, after, "unrouted_nets"),
    }


def _try_project_lock(
    root: Path, locks_root: Path, *, timeout: float
) -> ResourceLock | None:
    lock = ResourceLock(root, locks_root, timeout=timeout)
    try:
        return lock.acquire()
    except PCBDraftError as exc:
        if "resource is locked by another runtime process" in str(exc):
            return None
        raise


def _validated_project_root(service: ApplicationService, project_id: str) -> Path:
    """Resolve a project path without reading its records before locking it."""

    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise ValidationError("application project id is invalid")
    projects_root = Path(service.projects_root)
    if projects_root.is_symlink() or not projects_root.is_dir():
        raise ValidationError("application projects path is unsafe")
    try:
        parent = projects_root.resolve(strict=True)
        candidate = parent / project_id
        if candidate.is_symlink():
            raise ValidationError("application project path is unsafe")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(
            f"application project does not exist: {project_id}"
        ) from exc
    if resolved.parent != parent or not resolved.is_dir():
        raise ValidationError("application project path escapes the workspace")
    return resolved


def _draft_scene(view: Mapping[str, Any]) -> dict[str, Any]:
    state = _mapping(view.get("state"), "project state")
    project = _mapping(view.get("project"), "project summary")
    return {
        "schema": LIVE_SCENE_SCHEMA,
        "version": LIVE_SCENE_VERSION,
        "source": "committed_project",
        "committed": True,
        "project_id": _bounded_text(project.get("id"), "project id", 80),
        "state_revision": _counter(state.get("revision"), "state revision"),
        "design_revision": _counter(state.get("design_revision"), "design revision"),
        "geometry_revision": 0,
        "content_hash": None,
        "event_sequence": _counter(state.get("event_sequence"), "event sequence"),
        "board": None,
        "outline": [],
        "footprints": [],
        "routes": [],
        "vias": [],
        "unrouted_nets": [],
        "status": _status_projection(view, design_available=False),
    }


def _project_scene(
    view: Mapping[str, Any], managed: ManagedProject, limits: SceneLimits
) -> dict[str, Any]:
    state = _mapping(view.get("state"), "project state")
    project = _mapping(view.get("project"), "project summary")
    design = managed.design
    content_hash = design.content_hash()
    _validate_content_hash(content_hash)
    native = _native_board_snapshot(managed.manifest)
    board = _board_projection(design, native, limits)
    footprints = _footprint_projection(design, managed, native, limits)
    routes, vias = _copper_projection(design, native, board["layers"], limits)
    unrouted = _unrouted_projection(design, managed.manifest, footprints, limits)
    scene = {
        "schema": LIVE_SCENE_SCHEMA,
        "version": LIVE_SCENE_VERSION,
        "source": "committed_project",
        "committed": True,
        "project_id": _bounded_text(project.get("id"), "project id", 80),
        "state_revision": _counter(state.get("revision"), "state revision"),
        "design_revision": _counter(state.get("design_revision"), "design revision"),
        "geometry_revision": design.native_intent.geometry_revision,
        "content_hash": content_hash,
        "event_sequence": _counter(state.get("event_sequence"), "event sequence"),
        "board": board,
        "outline": board["outline"],
        "footprints": footprints,
        "routes": routes,
        "vias": vias,
        "unrouted_nets": unrouted,
        "status": _status_projection(view, design_available=True),
    }
    scene["status"]["counts"] = {
        "footprints": len(footprints),
        "routes": len(routes),
        "vias": len(vias),
        "unrouted_nets": len(unrouted),
        "nets": len(design.nets),
    }
    return scene


def _native_board_snapshot(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    snapshots = _mapping(manifest.get("native_snapshots"), "native snapshots")
    board = _mapping(snapshots.get("board"), "native board snapshot")
    if (
        board.get("schema") != "pcbdraft-pcbnew-result"
        or board.get("version") != 1
        or board.get("mode") != "inspect_board"
    ):
        raise ValidationError("unsupported committed native board snapshot")
    return board


def _board_projection(
    design: Design, native: Mapping[str, Any], limits: SceneLimits
) -> dict[str, Any]:
    if design.board.layers > limits.max_layers:
        raise ValidationError("live scene exceeds its copper layer limit")
    layers = _logical_layers(design.board.layers)
    native_board = _mapping(native.get("board"), "native board rules")
    native_layer_count = _counter(native_board.get("layers"), "native layer count")
    if native_layer_count != design.board.layers:
        raise ValidationError("committed native and semantic layer counts differ")
    outline_rows = _array(native.get("outline", []), "native board outline")
    if len(outline_rows) > limits.max_outline_segments:
        raise ValidationError("live scene exceeds its board outline limit")
    outline = [
        {
            "x1_mm": _number(_mapping(row, "outline row").get("x1_mm"), "outline x1"),
            "y1_mm": _number(_mapping(row, "outline row").get("y1_mm"), "outline y1"),
            "x2_mm": _number(_mapping(row, "outline row").get("x2_mm"), "outline x2"),
            "y2_mm": _number(_mapping(row, "outline row").get("y2_mm"), "outline y2"),
        }
        for row in outline_rows
    ]
    if not outline:
        points = list(design.native_intent.outline)
        if points:
            outline = [
                {
                    "x1_mm": first.x_mm,
                    "y1_mm": first.y_mm,
                    "x2_mm": second.x_mm,
                    "y2_mm": second.y_mm,
                }
                for first, second in zip(points, (*points[1:], points[0]), strict=True)
            ]
        else:
            width, height = design.board.width_mm, design.board.height_mm
            outline = [
                {"x1_mm": 0.0, "y1_mm": 0.0, "x2_mm": width, "y2_mm": 0.0},
                {"x1_mm": width, "y1_mm": 0.0, "x2_mm": width, "y2_mm": height},
                {"x1_mm": width, "y1_mm": height, "x2_mm": 0.0, "y2_mm": height},
                {"x1_mm": 0.0, "y1_mm": height, "x2_mm": 0.0, "y2_mm": 0.0},
            ]
    if len(outline) > limits.max_outline_segments:
        raise ValidationError("live scene exceeds its board outline limit")
    return {
        "width_mm": design.board.width_mm,
        "height_mm": design.board.height_mm,
        "layer_count": design.board.layers,
        "layers": layers,
        "outline": outline,
    }


def _footprint_projection(
    design: Design,
    managed: ManagedProject,
    native: Mapping[str, Any],
    limits: SceneLimits,
) -> list[dict[str, Any]]:
    native_rows = _array(native.get("components"), "native components")
    if len(native_rows) > limits.max_footprints:
        raise ValidationError("live scene exceeds its native footprint limit")
    by_reference: dict[str, Mapping[str, Any]] = {}
    for raw in native_rows:
        row = _mapping(raw, "native component")
        reference = _bounded_text(row.get("reference"), "native reference", 64)
        if reference in by_reference:
            raise ValidationError(
                "native board contains duplicate footprint references"
            )
        by_reference[reference] = row

    components = [
        component
        for component in design.components
        if component.attributes.get("exclude_from_board") is not True
        and managed.graph.get(component.part_id).footprint is not None
    ]
    if len(components) > limits.max_footprints:
        raise ValidationError("live scene exceeds its footprint limit")
    if set(by_reference) != {component.reference for component in components}:
        raise ValidationError(
            "committed native and semantic footprint inventories differ"
        )
    poses = {pose.component: pose for pose in design.native_intent.footprint_poses}
    connectivity = _footprint_connectivity(design)
    result: list[dict[str, Any]] = []
    for component in sorted(components, key=lambda item: item.id):
        native_row = by_reference.get(component.reference)
        placement = component.placement
        pose = poses.get(component.id)
        x_mm: float | None = None
        y_mm: float | None = None
        rotation_deg: float | None = None
        side: str | None = None
        if native_row is not None:
            pose_fields = {"x_mm", "y_mm", "rotation_deg", "side"}
            present = pose_fields & set(native_row)
            if present and present != pose_fields:
                raise ValidationError("native footprint pose is incomplete")
            if present:
                x_mm = _number(native_row.get("x_mm"), "native footprint x")
                y_mm = _number(native_row.get("y_mm"), "native footprint y")
                rotation_deg = (
                    _number(native_row.get("rotation_deg"), "native footprint rotation")
                    % 360
                )
                side = _side(native_row.get("side"))
        fallback = pose or placement
        if x_mm is None and fallback is not None:
            x_mm, y_mm = fallback.x_mm, fallback.y_mm
            rotation_deg, side = fallback.rotation_deg, fallback.side
        footprint = None
        if native_row is not None and native_row.get("footprint") is not None:
            footprint = _bounded_text(
                native_row.get("footprint"), "native footprint", 256
            )
        if footprint is None:
            part = managed.graph.get(component.part_id)
            footprint = part.footprint
        result.append(
            {
                "id": component.id,
                "reference": component.reference,
                "label": component.reference,
                "value": component.value,
                "part_id": component.part_id,
                "footprint": footprint,
                "x_mm": x_mm,
                "y_mm": y_mm,
                "rotation_deg": rotation_deg,
                "side": side,
                "fixed": bool(fallback.fixed) if fallback is not None else False,
                "placed": x_mm is not None and y_mm is not None,
                "pads": connectivity.get(component.id, {}).get("pads", []),
                "nets": connectivity.get(component.id, {}).get("nets", []),
            }
        )
    return result


def _footprint_connectivity(design: Design) -> dict[str, dict[str, list[Any]]]:
    """Project semantic pad/net associations without exposing native paths."""

    result: dict[str, dict[str, list[Any]]] = {}
    for net in design.nets:
        for endpoint in net.endpoints:
            component = result.setdefault(endpoint.component, {"pads": [], "nets": []})
            pad = _bounded_text(endpoint.pin, "semantic pad", 64)
            if pad and pad not in component["pads"]:
                component["pads"].append(pad)
            if not any(item["id"] == net.id for item in component["nets"]):
                component["nets"].append(
                    {
                        "id": _bounded_text(net.id, "semantic net id", 128),
                        "name": _bounded_text(net.name, "semantic net name", 128),
                    }
                )
    for component in result.values():
        component["pads"].sort()
        component["nets"].sort(key=lambda item: (item["name"], item["id"]))
    return result


def _copper_projection(
    design: Design,
    native: Mapping[str, Any],
    layers: list[str],
    limits: SceneLimits,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracks = _array(native.get("tracks"), "native tracks")
    if len(tracks) > limits.max_routes + limits.max_vias:
        raise ValidationError("live scene exceeds its copper object limit")
    net_mappings = _native_copper_net_mappings(design)
    routes: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    for raw in tracks:
        row = _mapping(raw, "native track")
        kind = _bounded_text(row.get("kind"), "native track kind", 16)
        net_name = _bounded_text(
            row.get("net", ""), "native track net", 128, empty=True
        )
        net_id, semantic_net_name = _resolve_native_copper_net(net_name, net_mappings)
        if kind == "segment":
            layer_index = _counter(row.get("layer_index"), "route layer")
            if layer_index >= len(layers):
                raise ValidationError("native route references an unavailable layer")
            routes.append(
                {
                    "net": net_id,
                    "net_name": semantic_net_name,
                    "layer_index": layer_index,
                    "layer": layers[layer_index],
                    "x1_mm": _number(row.get("x1_mm"), "route x1"),
                    "y1_mm": _number(row.get("y1_mm"), "route y1"),
                    "x2_mm": _number(row.get("x2_mm"), "route x2"),
                    "y2_mm": _number(row.get("y2_mm"), "route y2"),
                    "width_mm": _positive_number(row.get("width_mm"), "route width"),
                }
            )
        elif kind == "via":
            start = _counter(row.get("from_layer", 0), "via first layer")
            stop = _counter(row.get("to_layer", len(layers) - 1), "via last layer")
            if stop <= start or stop >= len(layers):
                raise ValidationError("native via references an invalid layer pair")
            drill = _positive_number(row.get("drill_mm"), "via drill")
            diameter = _positive_number(row.get("width_mm"), "via diameter")
            if diameter <= drill:
                raise ValidationError("native via diameter does not exceed its drill")
            vias.append(
                {
                    "net": net_id,
                    "net_name": semantic_net_name,
                    "x_mm": _number(row.get("x_mm"), "via x"),
                    "y_mm": _number(row.get("y_mm"), "via y"),
                    "diameter_mm": diameter,
                    "drill_mm": drill,
                    "from_layer": start,
                    "to_layer": stop,
                }
            )
        else:
            raise ValidationError("native board contains unsupported copper")
    if len(routes) > limits.max_routes:
        raise ValidationError("live scene exceeds its route limit")
    if len(vias) > limits.max_vias:
        raise ValidationError("live scene exceeds its via limit")
    return _stable_rows("route", routes), _stable_rows("via", vias)


def _native_copper_net_mappings(
    design: Design,
) -> dict[str, tuple[str, str] | None]:
    """Map exact and KiCad root-hierarchy copper names to semantic nets.

    KiCad reports a root-sheet local net as ``/NAME``.  That one leading slash
    is accepted in addition to the exact semantic name; nested paths and other
    basename-like transformations are deliberately not considered.  ``None``
    marks a collision so it remains fail-closed at projection time.
    """

    mappings: dict[str, tuple[str, str] | None] = {}
    for net in design.nets:
        semantic = (net.id, net.name)
        for native_name in (
            net.name,
            *(() if net.name.startswith("/") else (f"/{net.name}",)),
        ):
            if native_name in mappings and mappings[native_name] != semantic:
                mappings[native_name] = None
            else:
                mappings[native_name] = semantic
    return mappings


def _resolve_native_copper_net(
    native_name: str,
    mappings: Mapping[str, tuple[str, str] | None],
) -> tuple[str | None, str]:
    """Resolve one native copper net without relaxing unknown-net handling."""

    if not native_name:
        return None, native_name
    if native_name not in mappings:
        raise ValidationError("native copper references an unknown semantic net")
    semantic = mappings[native_name]
    if semantic is None:
        raise ValidationError("native copper references an ambiguous semantic net")
    return semantic


def _unrouted_projection(
    design: Design,
    manifest: Mapping[str, Any],
    footprints: list[dict[str, Any]],
    limits: SceneLimits,
) -> list[dict[str, Any]]:
    net_by_id = {net.id: net for net in design.nets}
    net_by_name = {net.name: net for net in design.nets}
    unrouted_ids = set(design.native_intent.unrouted_nets)
    generation = manifest.get("generation")
    if isinstance(generation, Mapping):
        pcb = generation.get("pcb")
        routing = pcb.get("routing") if isinstance(pcb, Mapping) else None
        raw_unrouted = (
            routing.get("unrouted", []) if isinstance(routing, Mapping) else []
        )
        if not isinstance(raw_unrouted, list):
            raise ValidationError("managed routing summary is malformed")
        for value in raw_unrouted:
            name = _bounded_text(value, "unrouted net", 128)
            net = net_by_name.get(name)
            if net is None:
                raise ValidationError(
                    "managed routing summary references an unknown net"
                )
            unrouted_ids.add(net.id)
    if len(unrouted_ids) > limits.max_unrouted_nets:
        raise ValidationError("live scene exceeds its unrouted-net limit")

    poses = {
        row["id"]: row
        for row in footprints
        if row.get("placed") is True
        and isinstance(row.get("x_mm"), (int, float))
        and isinstance(row.get("y_mm"), (int, float))
    }
    result: list[dict[str, Any]] = []
    endpoint_total = 0
    for net_id in sorted(unrouted_ids):
        net = net_by_id.get(net_id)
        if net is None:
            raise ValidationError("native intent references an unknown unrouted net")
        endpoints: list[dict[str, Any]] = []
        seen: set[str] = set()
        for endpoint in sorted(net.endpoints):
            if endpoint.component in seen or endpoint.component not in poses:
                continue
            seen.add(endpoint.component)
            footprint = poses[endpoint.component]
            endpoints.append(
                {
                    "component": endpoint.component,
                    "reference": footprint["reference"],
                    "x_mm": footprint["x_mm"],
                    "y_mm": footprint["y_mm"],
                }
            )
        endpoint_total += len(endpoints)
        if endpoint_total > limits.max_unrouted_endpoints:
            raise ValidationError("live scene exceeds its unrouted endpoint limit")
        first = endpoints[0] if endpoints else None
        lines = (
            [
                {
                    "x1_mm": first["x_mm"],
                    "y1_mm": first["y_mm"],
                    "x2_mm": endpoint["x_mm"],
                    "y2_mm": endpoint["y_mm"],
                }
                for endpoint in endpoints[1:]
            ]
            if first is not None
            else []
        )
        result.append(
            {
                "id": net.id,
                "name": net.name,
                "approximation": "component_centers",
                "endpoints": endpoints,
                "lines": lines,
            }
        )
    return result


def _status_projection(
    view: Mapping[str, Any], *, design_available: bool
) -> dict[str, Any]:
    project = _mapping(view.get("project"), "project summary")
    artifacts = view.get("artifacts")
    validation = (
        _validation_projection(artifacts.get("validation"))
        if isinstance(artifacts, Mapping)
        else None
    )
    return {
        "project_status": _bounded_text(project.get("status"), "project status", 80),
        "updated_at": _bounded_text(project.get("updated_at"), "updated at", 80),
        "design_available": design_available,
        "validation": validation,
        "ipc": {"state": "disabled"},
    }


def _validation_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in (
        "candidate_ready",
        "production_evidence_complete",
        "production_ready",
        "production_claimed",
    ):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    for key in ("source_revision", "source_design_revision"):
        current = value.get(key)
        if isinstance(current, int) and not isinstance(current, bool) and current >= 0:
            result[key] = current
    for key in ("check", "state", "outcome", "assurance"):
        current = value.get(key)
        if isinstance(current, str) and current:
            result[key] = _bounded_text(current, f"validation {key}", 80)
    diagnostics = value.get("diagnostics")
    counts = diagnostics.get("counts") if isinstance(diagnostics, Mapping) else None
    if isinstance(counts, Mapping):
        projected_counts: dict[str, int] = {}
        for key in ("error", "warning", "total"):
            current = counts.get(key)
            if (
                isinstance(current, int)
                and not isinstance(current, bool)
                and current >= 0
            ):
                projected_counts[key] = current
        if projected_counts:
            result["counts"] = projected_counts
    levels = value.get("levels")
    if isinstance(levels, list):
        projected_levels: list[dict[str, str]] = []
        for raw in levels[:16]:
            if not isinstance(raw, Mapping):
                continue
            row: dict[str, str] = {}
            for key in ("id", "state", "outcome"):
                current = raw.get(key)
                if isinstance(current, str) and current:
                    row[key] = _bounded_text(current, f"validation level {key}", 80)
            if row:
                projected_levels.append(row)
        if projected_levels:
            result["levels"] = projected_levels
    return result or None


def _logical_layers(count: int) -> list[str]:
    if count == 1:
        return ["F.Cu"]
    return ["F.Cu", *(f"In{index}.Cu" for index in range(1, count - 1)), "B.Cu"]


def _stable_rows(prefix: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=_canonical_row)
    occurrences: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for row in ordered:
        digest = hashlib.sha256(canonical_json_bytes(row)).hexdigest()[:20]
        occurrences[digest] = occurrences.get(digest, 0) + 1
        suffix = "" if occurrences[digest] == 1 else f"_{occurrences[digest]}"
        result.append({"id": f"{prefix}_{digest}{suffix}", **row})
    return result


def _canonical_row(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _collection_diff(
    before: Mapping[str, Any], after: Mapping[str, Any], name: str
) -> dict[str, list[str]]:
    old = _rows_by_id(before.get(name), name)
    new = _rows_by_id(after.get(name), name)
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(
            identity
            for identity in old.keys() & new.keys()
            if old[identity] != new[identity]
        ),
    }


def _rows_by_id(value: Any, label: str) -> dict[str, Mapping[str, Any]]:
    rows = _array(value, label)
    result: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        row = _mapping(raw, f"{label} row")
        identity = _bounded_text(row.get("id"), f"{label} identity", 128)
        if identity in result:
            raise ValidationError(f"{label} contains duplicate identities")
        result[identity] = row
    return result


def _private_cache_root(value: Path) -> Path:
    if value.exists() and (value.is_symlink() or not value.is_dir()):
        raise ValidationError("exact preview cache root is unsafe")
    try:
        value.mkdir(mode=0o700, parents=True, exist_ok=True)
        value.chmod(0o700)
        root = value.resolve(strict=True)
    except OSError as exc:
        raise PCBDraftError("cannot prepare exact preview cache") from exc
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("exact preview cache root is unsafe")
    return root


def _private_child(parent: Path, name: str) -> Path:
    if not name or Path(name).name != name:
        raise ValidationError("exact preview cache member is invalid")
    path = parent / name
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValidationError("exact preview cache member is unsafe")
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise PCBDraftError("cannot prepare exact preview cache member") from exc
    return path


def _cache_hash_parent(cache_root: Path, project_id: str, content_hash: str) -> Path:
    _validate_content_hash(content_hash)
    project = _private_child(cache_root, project_id)
    return _private_child(project, content_hash)


def _copy_managed_snapshot(managed: ManagedProject, destination: Path) -> None:
    """Copy only manifest-declared committed files into private staging."""

    files = _mapping(managed.manifest.get("files"), "managed project file map")
    if "manifest" not in files:
        raise ValidationError("managed project file map is incomplete")
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise PCBDraftError("cannot prepare exact preview source snapshot") from exc
    root = managed.root.resolve(strict=True)
    copied: set[str] = set()
    for value in files.values():
        relative = _bounded_text(value, "managed project file", 256)
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 1
            or relative in {".", ".."}
            or relative in copied
        ):
            raise ValidationError("managed project file map is unsafe")
        copied.add(relative)
        source = root / relative
        try:
            info = source.lstat()
        except OSError as exc:
            raise ValidationError("managed project file is missing") from exc
        if source.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError("managed project file is unsafe")
        try:
            shutil.copy2(source, destination / relative, follow_symlinks=False)
        except OSError as exc:
            raise PCBDraftError("cannot copy exact preview source snapshot") from exc


def _verified_artifact(directory: Path, content_hash: str, kind: str) -> Path:
    _validate_content_hash(content_hash)
    _render_kind, file_key, filename = _PREVIEW_KINDS[kind]
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("exact preview cache entry is unsafe")
    receipt_path = directory / "receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValidationError("exact preview receipt is missing or unsafe")
    receipt = load_json_limited(receipt_path, MAX_PREVIEW_RECEIPT_BYTES)
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema") != "pcbdraft-preview-bundle"
        or receipt.get("version") != 1
        or receipt.get("design_content_hash") != content_hash
    ):
        raise ValidationError("exact preview receipt is invalid")
    files = receipt.get("files")
    inventory = files.get(file_key) if isinstance(files, Mapping) else None
    if not isinstance(inventory, Mapping) or inventory.get("path") != filename:
        raise ValidationError("exact preview receipt has an unexpected file")
    artifact = directory / filename
    try:
        info = artifact.lstat()
    except OSError as exc:
        raise ValidationError("exact preview artifact is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or artifact.is_symlink():
        raise ValidationError("exact preview artifact is unsafe")
    expected_size = inventory.get("bytes")
    expected_hash = inventory.get("sha256")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or expected_size > PREVIEW_MAX_BYTES
        or info.st_size != expected_size
        or not isinstance(expected_hash, str)
        or _CONTENT_HASH.fullmatch(expected_hash) is None
        or sha256_file(artifact, max_bytes=PREVIEW_MAX_BYTES) != expected_hash
    ):
        raise ValidationError("exact preview artifact does not match its receipt")
    return artifact


def _validate_content_hash(value: str) -> None:
    if not isinstance(value, str) or _CONTENT_HASH.fullmatch(value) is None:
        raise ValidationError("design content hash is invalid")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def _bounded_text(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValidationError(f"{label} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise ValidationError(f"{label} exceeds its size limit")
    return value


def _counter(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be a finite number")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise ValidationError(f"{label} must be positive")
    return result


def _side(value: Any) -> str:
    side = _bounded_text(value, "native footprint side", 16)
    if side not in {"front", "back"}:
        raise ValidationError("native footprint side is unsupported")
    return side
