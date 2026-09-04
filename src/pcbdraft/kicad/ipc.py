"""Optional, read-only KiCad 10 IPC companion for the local GUI.

``kipy`` is imported lazily.  The core GUI therefore remains usable when the
optional dependency is absent, IPC is disabled, or KiCad is not running.
There are intentionally no mutation methods in this module.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import canonical_json_bytes
from pcbdraft.services.live_view import LIVE_SCENE_SCHEMA, SceneLimits

IPC_MIN_POLL_INTERVAL_SECONDS = 0.5
IPC_MAX_POLL_INTERVAL_SECONDS = 60.0


class _IPCPackageUnavailable(RuntimeError):
    pass


class KiCadIPCCompanion:
    """Low-frequency, best-effort reader for an open KiCad PCB document."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        poll_interval: float = 1.0,
        connector: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        limits: SceneLimits | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValidationError("KiCad IPC enabled flag must be boolean")
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not math.isfinite(float(poll_interval))
            or not (
                IPC_MIN_POLL_INTERVAL_SECONDS
                <= float(poll_interval)
                <= IPC_MAX_POLL_INTERVAL_SECONDS
            )
        ):
            raise ValidationError(
                "KiCad IPC poll interval must be between 0.5 and 60 seconds"
            )
        self.enabled = enabled
        self.poll_interval = float(poll_interval)
        self._connector = connector or _connect_default_board
        self._clock = clock
        self.limits = limits or SceneLimits()
        self._next_poll = 0.0
        self._last_identity: tuple[Any, ...] | None = None
        self._last_result: dict[str, Any] | None = None

    def poll(self, base_scene: Mapping[str, Any]) -> dict[str, Any]:
        """Return IPC status and, when online, a read-only scene-shaped overlay."""

        _validate_base_scene(base_scene)
        identity = (
            base_scene.get("project_id"),
            base_scene.get("content_hash"),
            base_scene.get("state_revision"),
        )
        if not self.enabled:
            return {
                "status": self._status("disabled", "KiCad IPC companion is disabled")
            }

        now = float(self._clock())
        if (
            self._last_result is not None
            and identity == self._last_identity
            and now < self._next_poll
        ):
            return copy.deepcopy(self._last_result)
        self._next_poll = now + self.poll_interval
        self._last_identity = identity

        try:
            board = self._connector()
        except _IPCPackageUnavailable:
            result = {
                "status": self._status(
                    "unavailable", "Optional kicad-python support is not installed"
                )
            }
        except Exception:  # noqa: BLE001 - optional transport is an isolation boundary
            result = {
                "status": self._status(
                    "offline", "KiCad PCB Editor IPC is offline or unavailable"
                )
            }
        else:
            if board is None:
                result = {
                    "status": self._status(
                        "offline", "No open KiCad PCB Editor document is available"
                    )
                }
            else:
                try:
                    scene = map_ipc_board(base_scene, board, limits=self.limits)
                except Exception:  # noqa: BLE001 - optional transport boundary
                    result = {
                        "status": self._status(
                            "offline",
                            "KiCad IPC returned unsupported or oversized board data",
                        )
                    }
                else:
                    status = self._status("online", "KiCad PCB Editor IPC is online")
                    scene_status = scene.get("status")
                    if isinstance(scene_status, dict):
                        scene_status["ipc"] = status
                    result = {"status": status, "scene": scene}
        self._last_result = copy.deepcopy(result)
        return result

    def _status(self, state: str, message: str) -> dict[str, Any]:
        return {
            "state": state,
            "message": message,
            "read_only": True,
            "poll_interval_seconds": self.poll_interval,
        }


def map_ipc_board(
    base_scene: Mapping[str, Any],
    board: Any,
    *,
    limits: SceneLimits | None = None,
) -> dict[str, Any]:
    """Map getter-only KiCad board data onto the common live-scene shape.

    The returned scene is explicitly marked uncommitted.  It is an optional
    companion view; the caller must continue treating ``base_scene`` as the
    authoritative committed geometry.
    """

    _validate_base_scene(base_scene)
    selected_limits = limits or SceneLimits()
    scene = copy.deepcopy(dict(base_scene))
    scene["source"] = "kicad_ipc"
    scene["committed"] = False

    board_projection = scene.get("board")
    if not isinstance(board_projection, dict):
        raise ValidationError("KiCad IPC mapping needs an available committed board")
    layers = _ipc_layers(board, board_projection, selected_limits)
    board_projection["layers"] = layers
    board_projection["layer_count"] = len(layers)

    footprints = _ipc_footprints(
        board,
        _scene_rows(base_scene.get("footprints"), "base footprints"),
        selected_limits,
    )
    routes, vias = _ipc_copper(
        board,
        layers,
        _scene_rows(base_scene.get("routes"), "base routes"),
        _scene_rows(base_scene.get("vias"), "base vias"),
        _scene_rows(base_scene.get("unrouted_nets"), "base unrouted nets"),
        selected_limits,
    )
    scene["footprints"] = footprints
    scene["routes"] = routes
    scene["vias"] = vias
    status = scene.get("status")
    if isinstance(status, dict):
        status["counts"] = {
            "footprints": len(footprints),
            "routes": len(routes),
            "vias": len(vias),
            "unrouted_nets": len(scene.get("unrouted_nets", [])),
        }
    if len(canonical_json_bytes(scene)) > selected_limits.max_response_bytes:
        raise ValidationError("KiCad IPC scene exceeds its response size limit")
    return scene


def _connect_default_board() -> Any:
    try:
        module = importlib.import_module("kipy")
    except ModuleNotFoundError as exc:
        if exc.name == "kipy":
            raise _IPCPackageUnavailable from exc
        raise
    client_type = getattr(module, "KiCad", None)
    if not callable(client_type):
        raise _IPCPackageUnavailable
    client = client_type()
    getter = _member(client, ("get_board", "GetBoard", "board"))
    return getter


def _ipc_layers(
    board: Any, base_board: Mapping[str, Any], limits: SceneLimits
) -> list[str]:
    copper_count = _field(
        board,
        ("get_copper_layer_count", "GetCopperLayerCount", "copper_layer_count"),
        default=None,
    )
    if copper_count is not None:
        count = _counter(copper_count, "IPC copper layer count")
        if count < 1 or count > limits.max_layers:
            raise ValidationError("KiCad IPC copper layer count is invalid")
        return _logical_layers(count)
    raw = _optional_collection(
        board,
        (
            "get_layers",
            "GetLayers",
            "get_enabled_layers",
            "GetEnabledLayers",
            "layers",
            "layer_names",
        ),
    )
    if raw is None:
        base = base_board.get("layers")
        if not isinstance(base, list):
            raise ValidationError("base scene layers are malformed")
        result = [_text(item, "base layer", 32) for item in base]
    else:
        result = []
        for item in raw:
            value = _field(item, ("name", "layer_name"), default=item)
            if isinstance(value, int) and not isinstance(value, bool):
                if value in {0, 3}:
                    value = "F.Cu"
                elif value in {31, 34}:
                    value = "B.Cu"
                elif 4 <= value <= 33:
                    value = f"In{value - 3}.Cu"
                elif 0 < value < 31:
                    value = f"In{value}.Cu"
                else:
                    continue
            name = _text(value, "IPC layer", 32)
            if name.endswith(".Cu"):
                result.append(name)
    if not result or len(result) > limits.max_layers or len(result) != len(set(result)):
        raise ValidationError("KiCad IPC layer list is invalid or oversized")
    return result


def _ipc_footprints(
    board: Any, base_rows: list[Mapping[str, Any]], limits: SceneLimits
) -> list[dict[str, Any]]:
    raw = _optional_collection(board, ("get_footprints", "GetFootprints", "footprints"))
    if raw is None:
        return [dict(row) for row in base_rows]
    if len(raw) > limits.max_footprints:
        raise ValidationError("KiCad IPC footprint list exceeds its limit")
    base_by_reference = {
        _text(row.get("reference"), "base footprint reference", 64): row
        for row in base_rows
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        reference = _ipc_reference(item)
        if reference in seen:
            raise ValidationError("KiCad IPC returned duplicate footprint references")
        seen.add(reference)
        base = base_by_reference.get(reference)
        if base is None:
            raise ValidationError(
                "open KiCad board does not match the selected project"
            )
        position = _field(
            item, ("position", "get_position", "GetPosition"), default=None
        )
        x_value = _field(item, ("x_mm",), default=None)
        y_value = _field(item, ("y_mm",), default=None)
        x_is_nm = False
        y_is_nm = False
        if position is not None:
            explicit_x = _field(position, ("x_mm",), default=None)
            explicit_y = _field(position, ("y_mm",), default=None)
            x_value = (
                explicit_x
                if explicit_x is not None
                else _field(position, ("x",), default=x_value)
            )
            y_value = (
                explicit_y
                if explicit_y is not None
                else _field(position, ("y",), default=y_value)
            )
            x_is_nm = explicit_x is None
            y_is_nm = explicit_y is None
        row = dict(base)
        if x_value is not None and y_value is not None:
            row["x_mm"] = _distance_mm(
                x_value, "IPC footprint x", raw_numeric_nanometers=x_is_nm
            )
            row["y_mm"] = _distance_mm(
                y_value, "IPC footprint y", raw_numeric_nanometers=y_is_nm
            )
            row["placed"] = True
        rotation = _ipc_orientation(item)
        if rotation is not None:
            row["rotation_deg"] = rotation
        side = _field(item, ("side",), default=None)
        if side is None:
            flipped = _field(item, ("flipped", "is_flipped", "IsFlipped"), default=None)
            if isinstance(flipped, bool):
                side = "back" if flipped else "front"
        if side is None:
            layer = _field(item, ("layer",), default=None)
            if layer is not None:
                side = _footprint_side_from_layer(layer)
        if side is not None:
            row["side"] = _side(side)
        footprint = _field(
            item,
            ("footprint", "library_id", "lib_id", "get_library_id"),
            default=None,
        )
        if footprint is None:
            definition = _field(item, ("definition",), default=None)
            if definition is not None:
                footprint = _field(definition, ("id",), default=None)
        if footprint is not None:
            if not isinstance(footprint, str):
                library = _field(
                    footprint, ("library", "library_nickname"), default=None
                )
                name = _field(footprint, ("name", "entry_name", "value"), default=None)
                footprint = (
                    f"{library}:{name}"
                    if isinstance(library, str)
                    and library
                    and isinstance(name, str)
                    and name
                    else name
                )
            row["footprint"] = _text(footprint, "IPC footprint library id", 256)
        result.append(row)
    if seen != set(base_by_reference):
        raise ValidationError("open KiCad board does not match the selected project")
    return sorted(result, key=lambda row: str(row["id"]))


def _ipc_copper(
    board: Any,
    layers: list[str],
    base_routes: list[Mapping[str, Any]],
    base_vias: list[Mapping[str, Any]],
    base_unrouted: list[Mapping[str, Any]],
    limits: SceneLimits,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_tracks = _optional_collection(board, ("get_tracks", "GetTracks", "tracks"))
    raw_vias = _optional_collection(board, ("get_vias", "GetVias", "vias"))
    if raw_tracks is None and raw_vias is None:
        return [dict(row) for row in base_routes], [dict(row) for row in base_vias]
    tracks = raw_tracks or []
    via_items = list(raw_vias or [])
    if len(tracks) > limits.max_routes + limits.max_vias:
        raise ValidationError("KiCad IPC track list exceeds its limit")
    net_ids = _base_net_ids(base_routes, base_vias, base_unrouted)
    routes: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    for item in tracks:
        kind = _field(item, ("kind", "type"), default="segment")
        kind_text = str(kind).casefold()
        if "via" in kind_text:
            via_items.append(item)
            continue
        routes.append(_ipc_route(item, layers, net_ids))
    if len(routes) > limits.max_routes or len(via_items) > limits.max_vias:
        raise ValidationError("KiCad IPC copper exceeds its scene limit")
    vias.extend(_ipc_via(item, layers, net_ids) for item in via_items)
    return _stable_rows("ipc_route", routes), _stable_rows("ipc_via", vias)


def _ipc_route(
    item: Any, layers: list[str], net_ids: Mapping[str, str]
) -> dict[str, Any]:
    start = _field(item, ("start", "get_start", "GetStart"), default=None)
    end = _field(item, ("end", "get_end", "GetEnd"), default=None)
    x1 = _field(item, ("x1_mm",), default=None)
    y1 = _field(item, ("y1_mm",), default=None)
    x2 = _field(item, ("x2_mm",), default=None)
    y2 = _field(item, ("y2_mm",), default=None)
    x1_is_nm = False
    y1_is_nm = False
    x2_is_nm = False
    y2_is_nm = False
    if start is not None:
        explicit_x = _field(start, ("x_mm",), default=None)
        explicit_y = _field(start, ("y_mm",), default=None)
        x1 = explicit_x if explicit_x is not None else _field(start, ("x",), default=x1)
        y1 = explicit_y if explicit_y is not None else _field(start, ("y",), default=y1)
        x1_is_nm = explicit_x is None
        y1_is_nm = explicit_y is None
    if end is not None:
        explicit_x = _field(end, ("x_mm",), default=None)
        explicit_y = _field(end, ("y_mm",), default=None)
        x2 = explicit_x if explicit_x is not None else _field(end, ("x",), default=x2)
        y2 = explicit_y if explicit_y is not None else _field(end, ("y",), default=y2)
        x2_is_nm = explicit_x is None
        y2_is_nm = explicit_y is None
    layer_index, layer = _ipc_layer(item, layers)
    net_name = _ipc_net_name(item)
    width = _field(item, ("width_mm",), default=None)
    width_is_nm = width is None
    if width is None:
        width = _field(item, ("width", "get_width", "GetWidth"))
    return {
        "net": net_ids.get(net_name, net_name or None),
        "net_name": net_name,
        "layer_index": layer_index,
        "layer": layer,
        "x1_mm": _distance_mm(x1, "IPC route x1", raw_numeric_nanometers=x1_is_nm),
        "y1_mm": _distance_mm(y1, "IPC route y1", raw_numeric_nanometers=y1_is_nm),
        "x2_mm": _distance_mm(x2, "IPC route x2", raw_numeric_nanometers=x2_is_nm),
        "y2_mm": _distance_mm(y2, "IPC route y2", raw_numeric_nanometers=y2_is_nm),
        "width_mm": _positive_distance(
            width,
            "IPC route width",
            raw_numeric_nanometers=width_is_nm,
        ),
    }


def _ipc_via(
    item: Any, layers: list[str], net_ids: Mapping[str, str]
) -> dict[str, Any]:
    position = _field(item, ("position", "get_position", "GetPosition"), default=None)
    x_value = _field(item, ("x_mm",), default=None)
    y_value = _field(item, ("y_mm",), default=None)
    x_is_nm = False
    y_is_nm = False
    if position is not None:
        explicit_x = _field(position, ("x_mm",), default=None)
        explicit_y = _field(position, ("y_mm",), default=None)
        x_value = (
            explicit_x
            if explicit_x is not None
            else _field(position, ("x",), default=x_value)
        )
        y_value = (
            explicit_y
            if explicit_y is not None
            else _field(position, ("y",), default=y_value)
        )
        x_is_nm = explicit_x is None
        y_is_nm = explicit_y is None
    padstack = _field(item, ("padstack",), default=None)
    drill_definition = (
        _field(padstack, ("drill",), default=None) if padstack is not None else None
    )
    start = _layer_bound(
        drill_definition if drill_definition is not None else item,
        ("from_layer", "start_layer", "top_layer"),
        layers,
        0,
    )
    stop = _layer_bound(
        drill_definition if drill_definition is not None else item,
        ("to_layer", "end_layer", "bottom_layer"),
        layers,
        len(layers) - 1,
    )
    if stop <= start:
        raise ValidationError("KiCad IPC via has an invalid layer pair")
    net_name = _ipc_net_name(item)
    raw_drill = _field(item, ("drill_mm",), default=None)
    drill_is_nm = raw_drill is None
    if raw_drill is None:
        raw_drill = _field(
            item,
            ("drill_diameter", "drill", "get_drill", "GetDrill"),
        )
    drill = _positive_distance(
        raw_drill,
        "IPC via drill",
        raw_numeric_nanometers=drill_is_nm,
    )
    raw_diameter = _field(item, ("diameter_mm", "width_mm"), default=None)
    diameter_is_nm = raw_diameter is None
    if raw_diameter is None:
        raw_diameter = _field(
            item,
            ("diameter", "width", "get_width", "GetWidth"),
            default=None,
        )
    if raw_diameter is None and padstack is not None:
        raw_diameter = _field(
            padstack, ("diameter_mm", "diameter", "size", "width"), default=None
        )
    diameter = _positive_distance(
        raw_diameter,
        "IPC via diameter",
        raw_numeric_nanometers=diameter_is_nm,
    )
    if diameter <= drill:
        raise ValidationError("KiCad IPC via diameter does not exceed its drill")
    return {
        "net": net_ids.get(net_name, net_name or None),
        "net_name": net_name,
        "x_mm": _distance_mm(x_value, "IPC via x", raw_numeric_nanometers=x_is_nm),
        "y_mm": _distance_mm(y_value, "IPC via y", raw_numeric_nanometers=y_is_nm),
        "diameter_mm": diameter,
        "drill_mm": drill,
        "from_layer": start,
        "to_layer": stop,
    }


def _ipc_layer(item: Any, layers: list[str]) -> tuple[int, str]:
    index = _field(item, ("layer_index",), default=None)
    raw = _field(item, ("layer", "layer_name", "get_layer", "GetLayer"), default=None)
    if index is not None:
        resolved = _counter(index, "IPC route layer")
        if resolved >= len(layers):
            raise ValidationError("KiCad IPC route layer is unavailable")
        return resolved, layers[resolved]
    if isinstance(raw, int) and not isinstance(raw, bool):
        resolved = _physical_layer_index(raw, len(layers))
        return resolved, layers[resolved]
    if raw is not None and not isinstance(raw, str):
        raw = _field(raw, ("value", "name"), default=raw)
        if isinstance(raw, int) and not isinstance(raw, bool):
            resolved = _physical_layer_index(raw, len(layers))
            return resolved, layers[resolved]
    name = _text(raw, "IPC route layer", 32)
    if name not in layers:
        raise ValidationError("KiCad IPC route layer is unavailable")
    return layers.index(name), name


def _layer_bound(
    item: Any, names: tuple[str, ...], layers: list[str], default: int
) -> int:
    value = _field(item, names, default=default)
    if isinstance(value, str):
        if value not in layers:
            raise ValidationError("KiCad IPC via layer is unavailable")
        return layers.index(value)
    if not isinstance(value, int) or isinstance(value, bool):
        value = _field(value, ("value",), default=value)
    result = _counter(value, "IPC via layer")
    return _physical_layer_index(result, len(layers))


def _ipc_net_name(item: Any) -> str:
    value = _field(
        item,
        ("net_name", "net", "get_net_name", "GetNetname", "GetNetName"),
        default="",
    )
    if not isinstance(value, str):
        value = _field(value, ("name", "get_name", "GetName"), default="")
    return _text(value, "IPC net name", 128, empty=True)


def _ipc_reference(item: Any) -> str:
    value = _field(
        item,
        ("reference", "ref", "get_reference", "GetReference"),
        default=None,
    )
    if value is None:
        reference_field = _field(item, ("reference_field",), default=None)
        text = (
            _field(reference_field, ("text",), default=None)
            if reference_field is not None
            else None
        )
        value = _field(text, ("value",), default=None) if text is not None else None
    return _text(value, "IPC footprint reference", 64)


def _ipc_orientation(item: Any) -> float | None:
    value = _field(
        item,
        (
            "rotation_deg",
            "orientation_degrees",
            "get_orientation_degrees",
            "GetOrientationDegrees",
            "orientation",
        ),
        default=None,
    )
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = _field(
            value,
            ("degrees", "value_degrees", "value"),
            default=None,
        )
    return _number(value, "IPC footprint rotation") % 360


def _footprint_side_from_layer(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        value = _field(value, ("name", "value"), default=None)
    if isinstance(value, str):
        lowered = value.casefold()
        return (
            "back"
            if lowered in {"b.cu", "b_cu", "back", "31", "34", "bl_b_cu"}
            else "front"
        )
    if isinstance(value, int) and not isinstance(value, bool):
        return "back" if value in {31, 34} else "front"
    raise ValidationError("KiCad IPC footprint layer is malformed")


def _physical_layer_index(value: int, layer_count: int) -> int:
    """Map kipy/proto or legacy KiCad layer IDs to compact scene indices."""

    # kicad-python 0.7.1 BoardLayer protobuf values use 3/34 for F.Cu/B.Cu
    # and 4..33 for In1..In30.  Generic/legacy adapters often expose the
    # pcbnew-style 0/31 and 1..30 values, which remain accepted too.
    if value in {0, 3}:
        return 0
    if value in {31, 34} and layer_count > 1:
        return layer_count - 1
    if 4 <= value <= 33:
        resolved = value - 3
        if resolved < layer_count - 1:
            return resolved
    if 0 < value < layer_count - 1:
        return value
    if 0 <= value < layer_count:
        return value
    raise ValidationError("KiCad IPC copper layer is unavailable")


def _logical_layers(count: int) -> list[str]:
    if count == 1:
        return ["F.Cu"]
    return ["F.Cu", *(f"In{index}.Cu" for index in range(1, count - 1)), "B.Cu"]


def _base_net_ids(
    routes: list[Mapping[str, Any]],
    vias: list[Mapping[str, Any]],
    unrouted: list[Mapping[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in (*routes, *vias):
        name, identity = row.get("net_name"), row.get("net")
        if isinstance(name, str) and name and isinstance(identity, str) and identity:
            result[name] = identity
    for row in unrouted:
        name, identity = row.get("name"), row.get("id")
        if isinstance(name, str) and name and isinstance(identity, str) and identity:
            result[name] = identity
    return result


def _optional_collection(value: Any, names: tuple[str, ...]) -> list[Any] | None:
    member = _field(value, names, default=None)
    if member is None:
        return None
    if isinstance(member, Mapping):
        return list(member.values())
    if isinstance(member, (str, bytes)) or not isinstance(member, Sequence):
        try:
            return list(member)
        except TypeError as exc:
            raise ValidationError("KiCad IPC collection is malformed") from exc
    return list(member)


_MISSING = object()


def _field(value: Any, names: tuple[str, ...], *, default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            current = value[name]
        elif hasattr(value, name):
            current = getattr(value, name)
        else:
            continue
        return current() if callable(current) else current
    if default is not _MISSING:
        return default
    raise AttributeError(f"KiCad IPC object lacks required field {names[0]}")


def _member(value: Any, names: tuple[str, ...]) -> Any:
    return _field(value, names)


def _distance_mm(
    value: Any, label: str, *, raw_numeric_nanometers: bool = False
) -> float:
    if value is None:
        raise ValidationError(f"{label} is unavailable")
    converted = _field(value, ("to_mm",), default=None)
    if converted is not None:
        return _number(converted, label)
    millimeters = _field(value, ("mm", "value_mm"), default=None)
    if millimeters is not None:
        return _number(millimeters, label)
    nanometers = _field(value, ("nm", "nanometers", "value_nm"), default=None)
    if nanometers is not None:
        return _number(nanometers, label) / 1_000_000.0
    result = _number(value, label)
    return result / 1_000_000.0 if raw_numeric_nanometers else result


def _positive_distance(
    value: Any, label: str, *, raw_numeric_nanometers: bool = False
) -> float:
    result = _distance_mm(value, label, raw_numeric_nanometers=raw_numeric_nanometers)
    if result <= 0:
        raise ValidationError(f"{label} must be positive")
    return result


def _stable_rows(prefix: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )
    counts: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for row in ordered:
        digest = hashlib.sha256(canonical_json_bytes(row)).hexdigest()[:20]
        counts[digest] = counts.get(digest, 0) + 1
        suffix = "" if counts[digest] == 1 else f"_{counts[digest]}"
        result.append({"id": f"{prefix}_{digest}{suffix}", **row})
    return result


def _scene_rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise ValidationError(f"{label} must be an array of objects")
    return list(value)


def _validate_base_scene(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or value.get("schema") != LIVE_SCENE_SCHEMA:
        raise ValidationError("KiCad IPC companion requires a live-board scene")


def _text(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValidationError(f"{label} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise ValidationError(f"{label} exceeds its size limit")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be a finite number")
    return result


def _counter(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _side(value: Any) -> str:
    result = _text(value, "IPC footprint side", 16)
    if result not in {"front", "back"}:
        raise ValidationError("KiCad IPC footprint side is unsupported")
    return result
