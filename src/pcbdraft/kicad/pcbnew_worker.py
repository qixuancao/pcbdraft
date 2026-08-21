#!/usr/bin/python3
"""Isolated pcbnew worker for trusted footprint inspection and board writing.

This file intentionally imports only the standard library and KiCad's system
``pcbnew`` module.  The parent runtime invokes it with ``python3 -I`` and a
bounded, internally generated JSON job, keeping host project code off sys.path.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import pcbnew

JOB_LIMIT = 32 * 1024 * 1024
MAX_COMPONENTS = 500
MAX_NETS = 2000
MAX_ROUTES = 200_000
LIB_ID = re.compile(r"^[A-Za-z0-9_.+~-]+:[A-Za-z0-9_.+~(){}-]+$")
WORKER_NAMESPACE = uuid.UUID("33c385e7-95eb-5434-a09f-cf8c782dcc01")


def _footprint_root():
    candidates = []
    for variable in ("KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR"):
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    executable = Path(sys.executable).resolve()
    if executable.parent.name.casefold() == "bin":
        candidates.append(executable.parent.parent / "share" / "kicad" / "footprints")
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.name == "Contents":
                candidates.append(parent / "SharedSupport" / "footprints")
                break
        candidates.extend(
            (
                Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
                Path.home()
                / "Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
            )
        )
    elif sys.platform == "win32":
        install = os.environ.get("KICAD_INSTALL_PATH", "").strip()
        if install:
            candidates.append(Path(install) / "share" / "kicad" / "footprints")
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = os.environ.get(variable, "").strip()
            if root:
                candidates.append(
                    Path(root) / "KiCad" / "10.0" / "share" / "kicad" / "footprints"
                )
    else:
        candidates.extend(
            (
                Path("/usr/share/kicad/footprints"),
                Path("/usr/local/share/kicad/footprints"),
            )
        )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0] if candidates else Path("__missing_kicad_footprints__")


FOOTPRINT_ROOT = _footprint_root()


def _strict(value, required, optional=()):
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    keys = set(value)
    missing = set(required) - keys
    extra = keys - set(required) - set(optional)
    if missing or extra:
        raise ValueError(
            f"job object fields mismatch (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    return value


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(
            f"{name} must be a {'positive ' if positive else ''}finite number"
        )
    return result


def _text(value, name, limit=512):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _load_job(path):
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > JOB_LIMIT:
        raise ValueError("worker job is missing, linked, or oversized")
    with source.open("r", encoding="utf-8") as handle:
        job = json.load(handle)
    _strict(
        job,
        {"schema", "version", "mode", "design_id"},
        {"components", "board", "nets", "segments", "vias", "title", "board_path"},
    )
    if job["schema"] != "pcbdraft-pcbnew-job" or job["version"] != 1:
        raise ValueError("unsupported pcbnew worker job")
    if job["mode"] not in {"inspect", "inspect_board", "build"}:
        raise ValueError("unsupported pcbnew worker mode")
    _text(job["design_id"], "design_id", 128)
    return job


def _footprint_path(library_id):
    library_id = _text(library_id, "footprint", 256)
    if not LIB_ID.fullmatch(library_id):
        raise ValueError(f"invalid footprint library id: {library_id!r}")
    library, name = library_id.split(":", 1)
    library_dir = FOOTPRINT_ROOT / f"{library}.pretty"
    file_path = library_dir / f"{name}.kicad_mod"
    root = FOOTPRINT_ROOT.resolve()
    resolved = file_path.resolve(strict=True)
    if root not in resolved.parents or resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"footprint is unavailable: {library_id}")
    return library_dir, name, resolved


def _load_footprint(library_id):
    library_dir, name, _resolved = _footprint_path(library_id)
    footprint = pcbnew.FootprintLoad(str(library_dir), name)
    if footprint is None:
        raise ValueError(f"KiCad could not load footprint: {library_id}")
    return footprint


def _mm(value):
    return round(pcbnew.ToMM(value), 9)


def _available_copper_layers():
    """Return the copper layer IDs exposed by this installed pcbnew build."""

    layers = [pcbnew.F_Cu]
    index = 1
    while hasattr(pcbnew, f"In{index}_Cu"):
        layers.append(getattr(pcbnew, f"In{index}_Cu"))
        index += 1
    layers.append(pcbnew.B_Cu)
    return layers


def _actual_layers(count):
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("worker layer count must be a positive integer")
    available = _available_copper_layers()
    if count > len(available):
        raise ValueError(
            f"KiCad {pcbnew.GetBuildVersion()} exposes {len(available)} copper layers; "
            f"requested {count}"
        )
    if count == 1:
        return [pcbnew.F_Cu]
    return [pcbnew.F_Cu, *available[1:-1][: count - 2], pcbnew.B_Cu]


def _stable(design_id, kind, identifier):
    return str(uuid.uuid5(WORKER_NAMESPACE, f"{design_id}/{kind}/{identifier}"))


def _set_uuid(item, value, replacements):
    old = item.m_Uuid.AsStdString()
    prior = replacements.get(old)
    if prior is not None and prior != value:
        raise ValueError("pcbnew reused one UUID for distinct generated objects")
    replacements[old] = value
    if hasattr(item, "m_Uuid"):
        try:
            item.m_Uuid = pcbnew.KIID(value)
        except AttributeError:
            # KiCad 10's SWIG bindings expose UUIDs read-only.  The trusted output
            # is canonicalized after pcbnew writes it, preserving cross-references.
            pass


def _split_board_children(text):
    if not text.startswith("(kicad_pcb\n") or not text.endswith("\n)\n"):
        raise ValueError("pcbnew produced an unexpected board envelope")
    children = []
    depth = 1
    start = None
    in_string = False
    escaped = False
    index = len("(kicad_pcb\n")
    while index < len(text):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == "(":
            if depth == 1:
                start = index
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 1 and start is not None:
                children.append(text[start : index + 1])
                start = None
            elif depth == 0:
                break
            elif depth < 0:
                raise ValueError("pcbnew produced unbalanced board syntax")
        index += 1
    if depth != 0 or in_string or start is not None:
        raise ValueError("pcbnew produced malformed board syntax")
    return children


def _canonicalize_board_order(text):
    children = _split_board_children(text)
    priority = {
        "version": 0,
        "generator": 1,
        "generator_version": 2,
        "general": 3,
        "paper": 4,
        "title_block": 5,
        "layers": 6,
        "setup": 7,
        "net": 8,
        "footprint": 20,
        "gr_line": 30,
        "gr_arc": 31,
        "gr_rect": 32,
        "gr_circle": 33,
        "gr_poly": 34,
        "image": 35,
        "dimension": 36,
        "target": 37,
        "segment": 40,
        "via": 41,
        "zone": 42,
        "group": 43,
        "embedded_fonts": 1000,
    }

    def key(block):
        match = re.match(r"\(([A-Za-z0-9_]+)(?:\s|\))", block)
        if match is None:
            raise ValueError("pcbnew produced an unknown top-level board form")
        kind = match.group(1)
        return priority.get(kind, 900), kind, block

    ordered = sorted(children, key=key)
    return "(kicad_pcb\n" + "".join(f"\t{block}\n" for block in ordered) + ")\n"


def _canonicalize_board_uuids(path, replacements, design_id):
    source = Path(path)
    data = source.read_bytes()
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("generated board exceeds canonicalization bound")
    text = data.decode("utf-8")
    definition = re.compile(
        r'\((?:uuid|tstamp)\s+"?([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})"?\)'
    )
    point_definition = re.compile(
        r'(\(point\s+.*?)\(uuid\s+"?([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})"?\)',
        re.DOTALL,
    )
    for point_index, match in enumerate(point_definition.finditer(text)):
        old = match.group(2).lower()
        replacements.setdefault(
            old, _stable(design_id, "footprint_point", str(point_index))
        )
    for old, new in sorted(replacements.items()):
        text = re.sub(
            rf"(?<![0-9a-fA-F]){re.escape(old)}(?![0-9a-fA-F])",
            new,
            text,
            flags=re.IGNORECASE,
        )
    expected = set(replacements.values())
    unexpected = sorted(
        {
            match.group(1).lower()
            for match in definition.finditer(text)
            if match.group(1).lower() not in expected
        }
    )
    if unexpected:
        raise ValueError(
            f"generated board contains {len(unexpected)} untracked UUID definitions"
        )
    text = _canonicalize_board_order(text)
    source.write_text(text, encoding="utf-8", newline="\n")


def inspect_job(job):
    components = job.get("components")
    if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
        raise ValueError("inspect job has invalid component count")
    board = _strict(job.get("board"), {"layers"})
    layers = board["layers"]
    if isinstance(layers, bool) or not isinstance(layers, int):
        raise TypeError("board.layers must be an integer")
    actual_layers = _actual_layers(layers)
    output = []
    ids = set()
    for component in components:
        entry = _strict(component, {"id", "footprint", "rotation_deg", "side"})
        identifier = _text(entry["id"], "component.id", 128)
        if identifier in ids:
            raise ValueError("duplicate component id")
        ids.add(identifier)
        rotation = _number(entry["rotation_deg"], "rotation_deg") % 360
        footprint = _load_footprint(entry["footprint"])
        footprint.SetPosition(pcbnew.VECTOR2I(0, 0))
        footprint.SetOrientationDegrees(rotation)
        if entry["side"] not in {"front", "back"}:
            raise ValueError("component.side must be front or back")
        if entry["side"] == "back":
            footprint.Flip(footprint.GetPosition(), False)
        bbox = footprint.GetBoundingBox(False, False)
        pads = []
        for index, pad in enumerate(footprint.Pads()):
            pad_bbox = pad.GetBoundingBox()
            logical_layers = tuple(
                logical
                for logical, actual in enumerate(actual_layers)
                if pad.IsOnLayer(actual)
            )
            pads.append(
                {
                    "index": index,
                    "number": str(pad.GetNumber()),
                    "x_mm": _mm(pad.GetPosition().x),
                    "y_mm": _mm(pad.GetPosition().y),
                    "width_mm": _mm(pad_bbox.GetWidth()),
                    "height_mm": _mm(pad_bbox.GetHeight()),
                    "layers": list(logical_layers),
                }
            )
        output.append(
            {
                "id": identifier,
                "footprint": entry["footprint"],
                "bbox": {
                    "x_mm": _mm(bbox.GetX()),
                    "y_mm": _mm(bbox.GetY()),
                    "width_mm": _mm(bbox.GetWidth()),
                    "height_mm": _mm(bbox.GetHeight()),
                },
                "pads": pads,
            }
        )
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "inspect",
        "kicad_version": pcbnew.GetBuildVersion(),
        "components": sorted(output, key=lambda item: item["id"]),
    }


def inspect_board_job(job):
    raw_path = Path(_text(job.get("board_path"), "board_path", 4096))
    if raw_path.suffix != ".kicad_pcb" or raw_path.is_symlink():
        raise ValueError("inspect_board input must be a non-symlink .kicad_pcb")
    path = raw_path.resolve(strict=True)
    info = path.stat()
    if not path.is_file() or info.st_size > 128 * 1024 * 1024:
        raise ValueError("inspect_board input is missing or oversized")
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError("pcbnew could not load the board")
    components = []
    for footprint in board.GetFootprints():
        properties = {
            field.GetName(): field.GetText()
            for field in footprint.GetFields()
            if field.GetName()
            not in {"Reference", "Value", "Datasheet", "Description", "KiLib_Generator"}
        }
        pads = sorted(
            (
                {"number": str(pad.GetNumber()), "net": str(pad.GetNetname())}
                for pad in footprint.Pads()
            ),
            key=lambda item: item["number"],
        )
        components.append(
            {
                "reference": str(footprint.GetReference()),
                "value": str(footprint.GetValue()),
                "footprint": footprint.GetFPID().GetUniStringLibId(),
                "schematic_path": footprint.GetPath().AsString(),
                "x_mm": _mm(footprint.GetPosition().x),
                "y_mm": _mm(footprint.GetPosition().y),
                "rotation_deg": round(
                    float(footprint.GetOrientationDegrees()) % 360, 9
                ),
                "side": "back" if footprint.IsFlipped() else "front",
                "properties": dict(sorted(properties.items())),
                "pads": pads,
            }
        )
    tracks = []
    for item in board.Tracks():
        if isinstance(item, pcbnew.PCB_VIA):
            tracks.append(
                {
                    "kind": "via",
                    "net": str(item.GetNetname()),
                    "x_mm": _mm(item.GetPosition().x),
                    "y_mm": _mm(item.GetPosition().y),
                    "width_mm": _mm(item.GetWidth()),
                    "drill_mm": _mm(item.GetDrillValue()),
                }
            )
        elif isinstance(item, pcbnew.PCB_TRACK):
            tracks.append(
                {
                    "kind": "segment",
                    "net": str(item.GetNetname()),
                    "x1_mm": _mm(item.GetStart().x),
                    "y1_mm": _mm(item.GetStart().y),
                    "x2_mm": _mm(item.GetEnd().x),
                    "y2_mm": _mm(item.GetEnd().y),
                    "width_mm": _mm(item.GetWidth()),
                    "layer": str(board.GetLayerName(item.GetLayer())),
                }
            )
        else:
            raise TypeError("board contains an unsupported track object")
    zones = []
    for zone in board.Zones():
        layer = zone.GetLayer()
        zones.append(
            {
                "net": str(zone.GetNetname()),
                "layer": str(board.GetLayerName(layer)),
                "filled": bool(zone.HasFilledPolysForLayer(layer)),
                "area_mm2": round(float(zone.CalculateFilledArea()) / 1e12, 6),
                "pad_connection": (
                    "solid"
                    if zone.GetPadConnection() == pcbnew.ZONE_CONNECTION_FULL
                    else (
                        "thermal_relief"
                        if zone.GetPadConnection() == pcbnew.ZONE_CONNECTION_THERMAL
                        else "other"
                    )
                ),
            }
        )
    settings = board.GetDesignSettings()
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "inspect_board",
        "kicad_version": pcbnew.GetBuildVersion(),
        "components": sorted(components, key=lambda item: item["reference"]),
        "tracks": sorted(tracks, key=lambda item: json.dumps(item, sort_keys=True)),
        "zones": sorted(zones, key=lambda item: json.dumps(item, sort_keys=True)),
        "board": {
            "layers": board.GetCopperLayerCount(),
            "thickness_mm": _mm(settings.GetBoardThickness()),
            "min_clearance_mm": _mm(settings.m_MinClearance),
            "min_track_mm": _mm(settings.m_TrackMinWidth),
            "min_drill_mm": _mm(settings.m_MinThroughDrill),
            "edge_clearance_mm": _mm(settings.m_CopperEdgeClearance),
        },
    }


def _configure_board(board, rules, layer_count):
    board.SetCopperLayerCount(layer_count)
    settings = board.GetDesignSettings()
    settings.SetBoardThickness(
        pcbnew.FromMM(_number(rules["thickness_mm"], "thickness_mm", positive=True))
    )
    settings.m_MinClearance = pcbnew.FromMM(
        _number(rules["min_clearance_mm"], "min_clearance_mm", positive=True)
    )
    settings.m_TrackMinWidth = pcbnew.FromMM(
        _number(rules["min_track_mm"], "min_track_mm", positive=True)
    )
    settings.m_MinThroughDrill = pcbnew.FromMM(
        _number(rules["min_drill_mm"], "min_drill_mm", positive=True)
    )
    settings.m_HoleClearance = pcbnew.FromMM(
        _number(
            rules["min_hole_clearance_mm"],
            "min_hole_clearance_mm",
            positive=True,
        )
    )
    settings.m_HoleToHoleMin = pcbnew.FromMM(
        _number(
            rules["min_hole_to_hole_mm"],
            "min_hole_to_hole_mm",
            positive=True,
        )
    )
    settings.m_ViasMinSize = pcbnew.FromMM(
        _number(rules["via_diameter_mm"], "via_diameter_mm", positive=True)
    )
    settings.m_CopperEdgeClearance = pcbnew.FromMM(
        _number(rules["edge_clearance_mm"], "edge_clearance_mm", positive=True)
    )
    netclass = board.GetAllNetClasses()["Default"]
    netclass.SetClearance(settings.m_MinClearance)
    netclass.SetTrackWidth(settings.m_TrackMinWidth)
    netclass.SetViaDiameter(settings.m_ViasMinSize)
    netclass.SetViaDrill(settings.m_MinThroughDrill)


def _configure_project_rules(project_data):
    """Replace KiCad's broad default ignores with explicit project rules."""
    try:
        board_severities = project_data["board"]["design_settings"]["rule_severities"]
    except (KeyError, TypeError) as exc:
        raise ValueError("pcbnew project lacks board rule severities") from exc
    if not isinstance(board_severities, dict):
        raise TypeError("pcbnew board rule severities are malformed")
    board_severities.update(
        {
            # The trusted graph and parity gate own the exact vendor mapping;
            # generic symbol glob filters reject valid JST naming. Validation
            # records this exact disabled heuristic as not applicable.
            "footprint_filters_mismatch": "ignore",
            "footprint_type_mismatch": "error",
            "missing_courtyard": "warning",
            "track_not_centered_on_via": "error",
            # No impedance-tuned or differential-pair profile exists in the
            # accepted low-speed scope. Validation records this exact N/A.
            "tuning_profile_track_geometries": "ignore",
        }
    )
    erc = project_data.setdefault("erc", {})
    if not isinstance(erc, dict):
        raise TypeError("pcbnew ERC project settings are malformed")
    erc.setdefault("meta", {"version": 0})
    erc_severities = erc.setdefault("rule_severities", {})
    if not isinstance(erc_severities, dict):
        raise TypeError("pcbnew ERC rule severities are malformed")
    erc_severities.update(
        {
            "footprint_filter": "ignore",
            "four_way_junction": "warning",
            "single_global_label": "warning",
            # Bundled parts deliberately make no SPICE-model claim. L5 reports
            # optional simulation separately instead of treating absence as pass.
            "simulation_model_issue": "ignore",
        }
    )


def _set_footprint_uuids(footprint, design_id, component_id, replacements):
    _set_uuid(footprint, _stable(design_id, "footprint", component_id), replacements)
    for field_index, field in enumerate(footprint.GetFields()):
        _set_uuid(
            field,
            _stable(design_id, "footprint_field", f"{component_id}/{field_index}"),
            replacements,
        )
    for pad_index, pad in enumerate(footprint.Pads()):
        _set_uuid(
            pad,
            _stable(design_id, "pad", f"{component_id}/{pad_index}/{pad.GetNumber()}"),
            replacements,
        )
    for item_index, item in enumerate(footprint.GraphicalItems()):
        _set_uuid(
            item,
            _stable(design_id, "footprint_graphic", f"{component_id}/{item_index}"),
            replacements,
        )
    for zone_index, zone in enumerate(footprint.Zones()):
        _set_uuid(
            zone,
            _stable(design_id, "footprint_zone", f"{component_id}/{zone_index}"),
            replacements,
        )
    for group_index, group in enumerate(footprint.Groups()):
        _set_uuid(
            group,
            _stable(design_id, "footprint_group", f"{component_id}/{group_index}"),
            replacements,
        )


def _add_outline(board, design_id, width_mm, height_mm, replacements):
    points = ((0, 0), (width_mm, 0), (width_mm, height_mm), (0, height_mm), (0, 0))
    for index, (first, second) in enumerate(itertools.pairwise(points)):
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(pcbnew.FromMM(0.05))
        shape.SetStart(pcbnew.VECTOR2I_MM(first[0], first[1]))
        shape.SetEnd(pcbnew.VECTOR2I_MM(second[0], second[1]))
        _set_uuid(shape, _stable(design_id, "board_outline", str(index)), replacements)
        board.Add(shape)


def _add_reference_plane(
    board,
    design_id,
    net_item,
    layer,
    width_mm,
    height_mm,
    edge_clearance_mm,
    local_clearance_mm,
    replacements,
):
    inset = edge_clearance_mm + 0.05
    if width_mm <= 2 * inset or height_mm <= 2 * inset:
        raise ValueError("board is too small for the declared reference plane")
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer)
    zone.SetNet(net_item)
    zone.SetLocalClearance(pcbnew.FromMM(local_clearance_mm))
    # Thermal relief is deterministic and keeps the through-hole programming
    # header hand-solderable while retaining a continuous low-current reference.
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    zone.SetThermalReliefGap(pcbnew.FromMM(0.2))
    zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.3))
    outline = zone.Outline()
    polygon = outline.NewOutline()
    for x_mm, y_mm in (
        (inset, inset),
        (width_mm - inset, inset),
        (width_mm - inset, height_mm - inset),
        (inset, height_mm - inset),
    ):
        outline.Append(pcbnew.VECTOR2I_MM(x_mm, y_mm), polygon)
    _set_uuid(
        zone,
        _stable(design_id, "reference_plane", str(board.GetLayerName(layer))),
        replacements,
    )
    board.Add(zone)
    return zone


def build_job(job, output_path):
    design_id = job["design_id"]
    rules = _strict(
        job.get("board"),
        {
            "width_mm",
            "height_mm",
            "layers",
            "thickness_mm",
            "min_clearance_mm",
            "min_track_mm",
            "min_drill_mm",
            "min_hole_clearance_mm",
            "min_hole_to_hole_mm",
            "edge_clearance_mm",
            "via_diameter_mm",
        },
    )
    width_mm = _number(rules["width_mm"], "width_mm", positive=True)
    height_mm = _number(rules["height_mm"], "height_mm", positive=True)
    layers = rules["layers"]
    if isinstance(layers, bool) or not isinstance(layers, int):
        raise TypeError("board.layers must be an integer")
    actual_layers = _actual_layers(layers)
    components = job.get("components")
    nets = job.get("nets")
    segments = job.get("segments")
    vias = job.get("vias")
    if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
        raise ValueError("build job has invalid component count")
    if not isinstance(nets, list) or len(nets) > MAX_NETS:
        raise ValueError("build job has invalid net count")
    if (
        not isinstance(segments, list)
        or not isinstance(vias, list)
        or len(segments) + len(vias) > MAX_ROUTES
    ):
        raise ValueError("build job has invalid route count")

    board = pcbnew.BOARD()
    uuid_replacements: dict[str, str] = {}
    _set_uuid(board, _stable(design_id, "board", "root"), uuid_replacements)
    _configure_board(board, rules, layers)
    title = job.get("title", {})
    if title:
        _strict(title, {"name", "revision", "ir_hash"})
        block = board.GetTitleBlock()
        block.SetTitle(_text(title["name"], "title.name", 256))
        block.SetRevision(_text(title["revision"], "title.revision", 64))
        block.SetCompany("Generated by PCBDraft")
        block.SetComment(
            0, f"Semantic IR: {_text(title['ir_hash'], 'title.ir_hash', 64)}"
        )
        block.SetComment(1, "Candidate; engineering and L7 physical evidence pending")

    net_items = {}
    for entry in nets:
        net = _strict(entry, {"name", "endpoints"})
        name = _text(net["name"], "net.name", 128)
        if name in net_items:
            raise ValueError(f"duplicate net: {name}")
        if not isinstance(net["endpoints"], list):
            raise TypeError("net endpoints must be a list")
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        net_items[name] = item

    footprints = {}
    pads_by_key = {}
    component_ids = set()
    for entry in components:
        component = _strict(
            entry,
            {
                "id",
                "reference",
                "value",
                "footprint",
                "x_mm",
                "y_mm",
                "rotation_deg",
                "side",
                "schematic_uuid",
                "datasheet",
                "description",
                "properties",
            },
        )
        component_id = _text(component["id"], "component.id", 128)
        if component_id in component_ids:
            raise ValueError("duplicate component id")
        component_ids.add(component_id)
        footprint = _load_footprint(component["footprint"])
        library, footprint_name = component["footprint"].split(":", 1)
        footprint.SetFPID(pcbnew.LIB_ID(library, footprint_name))
        footprint.SetReference(_text(component["reference"], "component.reference", 64))
        footprint.SetValue(_text(component["value"], "component.value", 256))
        footprint.Reference().SetVisible(False)
        footprint.Value().SetVisible(False)
        footprint.SetPosition(
            pcbnew.VECTOR2I_MM(
                _number(component["x_mm"], "component.x_mm"),
                _number(component["y_mm"], "component.y_mm"),
            )
        )
        footprint.SetOrientationDegrees(
            _number(component["rotation_deg"], "component.rotation_deg") % 360
        )
        if component["side"] not in {"front", "back"}:
            raise ValueError("component.side must be front or back")
        if component["side"] == "back":
            footprint.Flip(footprint.GetPosition(), False)
        footprint.SetPath(
            pcbnew.KIID_PATH(_text(component["schematic_uuid"], "schematic_uuid", 36))
        )
        footprint.SetBoardOnly(False)
        if footprint.HasField("Datasheet"):
            footprint.GetField("Datasheet").SetText(str(component["datasheet"])[:2048])
        if footprint.HasField("Description"):
            footprint.GetField("Description").SetText(
                str(component["description"])[:2048]
            )
        properties = component["properties"]
        if not isinstance(properties, dict) or len(properties) > 32:
            raise ValueError("component.properties must be a bounded object")
        for name, value in sorted(properties.items()):
            field_name = _text(name, "component property name", 64)
            if not isinstance(value, str) or len(value.encode("utf-8")) > 2048:
                raise ValueError("component property values must be bounded strings")
            footprint.SetField(field_name, value)
            field = footprint.GetField(field_name)
            field.SetVisible(False)
            field.SetLayer(pcbnew.F_Fab)
        board.Add(footprint)
        # KiCad assigns identifiers to some footprint-owned text fields only
        # when the footprint joins a board. Canonicalize after that ownership
        # transition so every serialized UUID is tracked deterministically.
        _set_footprint_uuids(footprint, design_id, component_id, uuid_replacements)
        footprints[component_id] = footprint
        for pad_index, pad in enumerate(footprint.Pads()):
            pads_by_key[(component_id, str(pad.GetNumber()), pad_index)] = pad

    assigned = set()
    for net in nets:
        net_item = net_items[net["name"]]
        for endpoint in net["endpoints"]:
            value = _strict(endpoint, {"component", "pad"})
            component_id = _text(value["component"], "endpoint.component", 128)
            pad_number = _text(value["pad"], "endpoint.pad", 64)
            matches = [
                pad
                for (candidate, number, _index), pad in pads_by_key.items()
                if candidate == component_id and number == pad_number
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"endpoint does not resolve to one pad: {component_id}/{pad_number}"
                )
            key = (component_id, pad_number)
            if key in assigned:
                raise ValueError(
                    f"pad assigned to multiple nets: {component_id}/{pad_number}"
                )
            assigned.add(key)
            matches[0].SetNet(net_item)

    _add_outline(board, design_id, width_mm, height_mm, uuid_replacements)
    ground_name = next((name for name in net_items if name.lstrip("/") == "GND"), None)
    if ground_name is None:
        raise ValueError("build job lacks the required GND reference net")
    reference_layer = actual_layers[1] if layers >= 3 else actual_layers[-1]
    reference_plane = _add_reference_plane(
        board,
        design_id,
        net_items[ground_name],
        reference_layer,
        width_mm,
        height_mm,
        _number(rules["edge_clearance_mm"], "edge_clearance_mm", positive=True),
        _number(rules["min_clearance_mm"], "min_clearance_mm", positive=True),
        uuid_replacements,
    )
    for index, entry in enumerate(segments):
        segment = _strict(
            entry, {"net", "layer", "x1_mm", "y1_mm", "x2_mm", "y2_mm", "width_mm"}
        )
        name = _text(segment["net"], "segment.net", 128)
        layer = segment["layer"]
        if (
            isinstance(layer, bool)
            or not isinstance(layer, int)
            or not 0 <= layer < layers
        ):
            raise ValueError("segment references invalid logical layer")
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(
            pcbnew.VECTOR2I_MM(
                _number(segment["x1_mm"], "x1_mm"), _number(segment["y1_mm"], "y1_mm")
            )
        )
        track.SetEnd(
            pcbnew.VECTOR2I_MM(
                _number(segment["x2_mm"], "x2_mm"), _number(segment["y2_mm"], "y2_mm")
            )
        )
        track.SetWidth(
            pcbnew.FromMM(
                _number(segment["width_mm"], "segment.width_mm", positive=True)
            )
        )
        track.SetLayer(actual_layers[layer])
        track.SetNet(net_items[name])
        _set_uuid(track, _stable(design_id, "track", str(index)), uuid_replacements)
        board.Add(track)
    for index, entry in enumerate(vias):
        via_data = _strict(entry, {"net", "x_mm", "y_mm", "diameter_mm", "drill_mm"})
        name = _text(via_data["net"], "via.net", 128)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(
            pcbnew.VECTOR2I_MM(
                _number(via_data["x_mm"], "via.x_mm"),
                _number(via_data["y_mm"], "via.y_mm"),
            )
        )
        via.SetWidth(
            pcbnew.FromMM(
                _number(via_data["diameter_mm"], "via.diameter_mm", positive=True)
            )
        )
        via.SetDrill(
            pcbnew.FromMM(_number(via_data["drill_mm"], "via.drill_mm", positive=True))
        )
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNet(net_items[name])
        _set_uuid(via, _stable(design_id, "via", str(index)), uuid_replacements)
        board.Add(via)

    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
        raise ValueError("pcbnew failed to fill the GND reference plane")
    if not reference_plane.HasFilledPolysForLayer(reference_layer):
        raise ValueError("GND reference plane has no filled polygon")
    reference_area_mm2 = round(float(reference_plane.CalculateFilledArea()) / 1e12, 6)
    if reference_area_mm2 <= 0:
        raise ValueError("GND reference plane has non-positive filled area")

    target = Path(output_path).resolve(strict=False)
    if target.suffix != ".kicad_pcb" or target.is_symlink():
        raise ValueError("board output must be a non-symlink .kicad_pcb path")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".kicad_pcb", dir=target.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        board.SetFileName(str(temporary))
        saved = pcbnew.SaveBoard(str(temporary), board)
        if saved is False:
            raise ValueError("pcbnew.SaveBoard returned failure")
        _canonicalize_board_uuids(temporary, uuid_replacements, design_id)
        auxiliary_project = temporary.with_suffix(".kicad_pro")
        project_target = target.with_suffix(".kicad_pro")
        if project_target.is_symlink():
            raise ValueError("refusing to replace a project symlink")
        if not auxiliary_project.is_file():
            raise ValueError("pcbnew did not create a project settings file")
        project_data = json.loads(auxiliary_project.read_text(encoding="utf-8"))
        project_data["meta"]["filename"] = project_target.name
        _configure_project_rules(project_data)
        auxiliary_project.write_text(
            json.dumps(
                project_data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(auxiliary_project, 0o644)
        os.replace(auxiliary_project, project_target)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_suffix(".kicad_pro").unlink(missing_ok=True)
        temporary.with_suffix(".kicad_prl").unlink(missing_ok=True)
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "build",
        "kicad_version": pcbnew.GetBuildVersion(),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "project_sha256": hashlib.sha256(
            target.with_suffix(".kicad_pro").read_bytes()
        ).hexdigest(),
        "counts": {
            "footprints": len(components),
            "nets": len(nets),
            "segments": len(segments),
            "vias": len(vias),
            "zones": 1,
        },
        "reference_planes": [
            {
                "net": ground_name,
                "layer": str(board.GetLayerName(reference_layer)),
                "filled": True,
                "area_mm2": reference_area_mm2,
                "pad_connection": "thermal_relief",
            }
        ],
    }


def _atomic_json(path, value):
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in {"inspect", "inspect_board", "build"}:
        raise ValueError(
            "usage: pcbnew_worker.py inspect|inspect_board|build JOB_JSON OUTPUT"
        )
    mode, job_path, output_path = sys.argv[1:]
    job = _load_job(job_path)
    if job["mode"] != mode:
        raise ValueError("worker argv mode disagrees with job mode")
    if mode == "inspect":
        _atomic_json(output_path, inspect_job(job))
    elif mode == "inspect_board":
        _atomic_json(output_path, inspect_board_job(job))
    else:
        result_path = str(Path(output_path).with_suffix(".worker-result.json"))
        _atomic_json(result_path, build_job(job, output_path))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - isolated worker failure boundary
        print(f"pcbnew worker failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from None
