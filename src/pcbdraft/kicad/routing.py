"""Deterministic bounded multilayer grid router.

This router targets small, low-speed controller boards.  It owns geometry and
search; an LLM never emits track coordinates.  Results remain explicitly
``heuristic`` until KiCad DRC and the runtime's connectivity checks validate the
materialized board.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pcbdraft.core.errors import ValidationError

MAX_CELLS = 2_000_000
MAX_PADS = 2_000
MAX_NETS = 1_000
MAX_EXPANSIONS_PER_BRANCH = 750_000
MAX_TOTAL_EXPANSIONS = 2_000_000
ROUTING_FAILURE_CODES = frozenset(
    {
        "invalid_seed",
        "zero_length_seed",
        "pad_escape_blocked",
        "no_legal_channel",
        "congestion_exhausted",
        "search_budget_exhausted",
        "native_commit_failed",
        "native_connectivity_failed",
        "unintended_net_merge",
    }
)


@dataclass(frozen=True, order=True)
class RoutingPad:
    id: str
    net: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    layers: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.net:
            raise ValidationError("routing pads require non-empty id and net")
        if not all(
            math.isfinite(value)
            for value in (self.x_mm, self.y_mm, self.width_mm, self.height_mm)
        ):
            raise ValidationError(f"routing pad {self.id!r} contains non-finite values")
        if self.width_mm <= 0 or self.height_mm <= 0 or not self.layers:
            raise ValidationError(
                f"routing pad {self.id!r} has invalid geometry/layers"
            )
        if tuple(sorted(set(self.layers))) != self.layers:
            raise ValidationError(
                f"routing pad {self.id!r} layers must be sorted and unique"
            )


@dataclass(frozen=True, order=True)
class RoutingKeepout:
    id: str
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    layers: tuple[int, ...]


@dataclass(frozen=True, order=True)
class RouteSegment:
    net: str
    layer: int
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    width_mm: float


@dataclass(frozen=True, order=True)
class RouteVia:
    net: str
    x_mm: float
    y_mm: float
    diameter_mm: float
    drill_mm: float
    from_layer: int
    to_layer: int


@dataclass(frozen=True, order=True)
class RoutingFailure:
    """Bounded, machine-readable reason why one route could not be completed."""

    code: str
    net: str
    endpoints: tuple[str, ...] = ()
    expanded_nodes: int = 0
    blocking_summary: str = ""
    recommendations: tuple[str, ...] = ()
    blocking_region: tuple[float, float, float, float] | None = None
    nearest_obstacle_class: str | None = None
    state_revision: int = 0
    state_context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.code not in ROUTING_FAILURE_CODES:
            raise ValidationError(f"unsupported routing failure code: {self.code}")
        if not self.net:
            raise ValidationError("routing failure requires a non-empty net")
        if self.expanded_nodes < 0:
            raise ValidationError("routing failure expanded_nodes must be non-negative")
        if isinstance(self.state_revision, bool) or self.state_revision < 0:
            raise ValidationError("routing failure state_revision must be non-negative")
        if self.blocking_region is not None and (
            len(self.blocking_region) != 4
            or not all(math.isfinite(item) for item in self.blocking_region)
            or self.blocking_region[0] > self.blocking_region[2]
            or self.blocking_region[1] > self.blocking_region[3]
        ):
            raise ValidationError("routing failure blocking_region is invalid")
        if self.nearest_obstacle_class is not None and (
            not self.nearest_obstacle_class
            or len(self.nearest_obstacle_class.encode("utf-8")) > 64
        ):
            raise ValidationError(
                "routing failure nearest_obstacle_class must be bounded"
            )
        if any(
            not item or len(item.encode("utf-8")) > 512 for item in self.state_context
        ):
            raise ValidationError("routing failure state_context must be bounded")

    @property
    def retry_key(self) -> str:
        endpoints = ",".join(self.endpoints) or "unknown"
        values = (
            self.code,
            self.net,
            endpoints,
            f"revision={self.state_revision}",
            *self.state_context,
        )
        return "|".join(item.replace("|", "%7C") for item in values)

    @property
    def diagnostic(self) -> str:
        endpoint = self.endpoints[-1] if self.endpoints else "unknown endpoint"
        detail = f"; {self.blocking_summary}" if self.blocking_summary else ""
        return f"{self.net}: could not connect pad {endpoint}; {self.code}{detail}"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "net": self.net,
            "endpoints": list(self.endpoints),
            "expanded_nodes": self.expanded_nodes,
            "blocking_summary": self.blocking_summary,
            "blocking_region": (
                list(self.blocking_region) if self.blocking_region is not None else None
            ),
            "nearest_obstacle_class": self.nearest_obstacle_class,
            "state_revision": self.state_revision,
            "state_context": list(self.state_context),
            "retry_key": self.retry_key,
            "recommendations": list(self.recommendations),
        }


class RoutingFailureError(ValidationError):
    """Expected route rejection carrying a stable structured failure."""

    def __init__(self, failure: RoutingFailure) -> None:
        super().__init__(failure.diagnostic)
        self.failure = failure


@dataclass(frozen=True)
class RoutingResult:
    segments: tuple[RouteSegment, ...]
    vias: tuple[RouteVia, ...]
    unrouted: tuple[str, ...]
    state: str
    expanded_nodes: int
    diagnostics: tuple[str, ...]
    failures: tuple[RoutingFailure, ...] = ()


GridState = tuple[int, int, int]  # x, y, logical copper layer


class GridRouter:
    """A bounded A* router with deterministic net ordering and through vias."""

    def __init__(
        self,
        *,
        board_width_mm: float,
        board_height_mm: float,
        layers: int,
        clearance_mm: float,
        min_track_mm: float,
        min_drill_mm: float,
        edge_clearance_mm: float,
        grid_mm: float = 0.1,
        via_diameter_mm: float | None = None,
        via_drill_mm: float | None = None,
        max_expansions: int = MAX_EXPANSIONS_PER_BRANCH,
        max_total_expansions: int | None = None,
    ) -> None:
        values = {
            "board_width_mm": board_width_mm,
            "board_height_mm": board_height_mm,
            "clearance_mm": clearance_mm,
            "min_track_mm": min_track_mm,
            "min_drill_mm": min_drill_mm,
            "edge_clearance_mm": edge_clearance_mm,
            "grid_mm": grid_mm,
        }
        if any(not math.isfinite(value) or value <= 0 for value in values.values()):
            raise ValidationError(
                "router dimensions and rules must be positive finite numbers"
            )
        if isinstance(layers, bool) or not isinstance(layers, int) or layers < 1:
            raise ValidationError("router layer count must be a positive integer")
        if not 1 <= max_expansions <= MAX_EXPANSIONS_PER_BRANCH:
            raise ValidationError(
                f"max_expansions must be 1..{MAX_EXPANSIONS_PER_BRANCH}"
            )
        if max_total_expansions is not None and (
            isinstance(max_total_expansions, bool)
            or not isinstance(max_total_expansions, int)
            or not 1 <= max_total_expansions <= MAX_TOTAL_EXPANSIONS
        ):
            raise ValidationError(
                f"max_total_expansions must be 1..{MAX_TOTAL_EXPANSIONS}"
            )
        self.board_width_mm = board_width_mm
        self.board_height_mm = board_height_mm
        self.layer_count = layers
        self.clearance_mm = clearance_mm
        self.min_track_mm = min_track_mm
        self.min_drill_mm = min_drill_mm
        self.edge_clearance_mm = edge_clearance_mm
        self.grid_mm = grid_mm
        self.width_cells = round(board_width_mm / grid_mm) + 1
        self.height_cells = round(board_height_mm / grid_mm) + 1
        if self.width_cells * self.height_cells * layers > MAX_CELLS:
            raise ValidationError(
                f"routing grid exceeds bounded limit of {MAX_CELLS} cells"
            )
        self.via_drill_mm = via_drill_mm or max(min_drill_mm, 0.3)
        self.via_diameter_mm = via_diameter_mm or max(
            self.via_drill_mm + 0.3, min_track_mm * 2
        )
        if self.via_diameter_mm <= self.via_drill_mm:
            raise ValidationError("via diameter must exceed drill diameter")
        self.max_expansions = max_expansions
        self.max_total_expansions = max_total_expansions
        self._pad_specs: list[list[tuple[int, int, float, float, str]]] = []
        self._pad_cell_cache: dict[float, list[dict[tuple[int, int], set[str]]]] = {}
        self._raw_pad_cells: list[dict[tuple[int, int], set[str]]] = []
        self._keepout_cells: list[set[tuple[int, int]]] = []
        self._occupied: list[dict[tuple[int, int], list[tuple[str, float]]]] = []
        self._seed_terminals: dict[tuple[str, int, int, int], GridState] = {}
        self._max_track_width = min_track_mm
        self._max_occupied_radius = max(min_track_mm / 2, self.via_diameter_mm / 2)
        self.expanded_nodes = 0
        self._remaining_expansions: int | None = None
        self._seed_obstructions: dict[str, list[str]] = {}

    def route(
        self,
        pads: Iterable[RoutingPad],
        *,
        widths: Mapping[str, float] | None = None,
        keepouts: Iterable[RoutingKeepout] = (),
        power_nets: Iterable[str] = (),
        seed_segments: Iterable[RouteSegment] = (),
        obstacle_segments: Iterable[RouteSegment] = (),
        obstacle_vias: Iterable[RouteVia] = (),
        state_revision: int = 0,
    ) -> RoutingResult:
        if (
            isinstance(state_revision, bool)
            or not isinstance(state_revision, int)
            or state_revision < 0
        ):
            raise ValidationError(
                "routing state_revision must be a non-negative integer"
            )
        self._state_revision = state_revision
        pads_tuple = tuple(sorted(pads))
        if len(pads_tuple) > MAX_PADS:
            raise ValidationError(f"router supports at most {MAX_PADS} pads")
        if len({pad.id for pad in pads_tuple}) != len(pads_tuple):
            raise ValidationError("routing pad ids must be unique")
        if any(
            layer < 0 or layer >= self.layer_count
            for pad in pads_tuple
            for layer in pad.layers
        ):
            raise ValidationError("routing pad references an unavailable copper layer")
        widths_dict = dict(widths or {})
        net_names = {pad.net for pad in pads_tuple}
        if len(net_names) > MAX_NETS:
            raise ValidationError(f"router supports at most {MAX_NETS} nets")
        unknown_widths = set(widths_dict) - net_names
        if unknown_widths:
            raise ValidationError(
                "track widths reference unknown nets: "
                + ", ".join(sorted(unknown_widths))
            )
        for net in net_names:
            widths_dict.setdefault(net, self.min_track_mm)
        if any(
            not math.isfinite(width) or width < self.min_track_mm
            for width in widths_dict.values()
        ):
            raise ValidationError(
                "track widths must be finite and at least min_track_mm"
            )
        self._max_track_width = max(widths_dict.values(), default=self.min_track_mm)
        self._max_occupied_radius = max(
            self._max_track_width / 2, self.via_diameter_mm / 2
        )
        keepouts_tuple = tuple(sorted(keepouts))
        self._initialize_obstacles(pads_tuple, keepouts_tuple)
        self._seed_terminals = {}
        self.expanded_nodes = 0
        self._remaining_expansions = self.max_total_expansions
        self._seed_obstructions = {}
        diagnostics: list[str] = []

        obstacle_segments_tuple = tuple(sorted(obstacle_segments))
        obstacle_vias_tuple = tuple(sorted(obstacle_vias))
        for segment in obstacle_segments_tuple:
            if (
                segment.layer < 0
                or segment.layer >= self.layer_count
                or segment.width_mm < self.min_track_mm
            ):
                raise ValidationError(
                    "retained obstacle segment violates layer or width bounds"
                )
            self._reserve(
                f"__retained__{segment.net}",
                self._segment_path(segment),
                segment.width_mm,
            )
        for via in obstacle_vias_tuple:
            if (
                via.from_layer < 0
                or via.to_layer >= self.layer_count
                or via.from_layer >= via.to_layer
                or via.drill_mm <= 0
                or via.diameter_mm <= via.drill_mm
            ):
                raise ValidationError("retained obstacle via violates board bounds")
            x_cell, y_cell = self._point(via.x_mm, via.y_mm)
            owner = f"__retained__{via.net}"
            for layer in range(via.from_layer, via.to_layer + 1):
                self._reserve(
                    owner,
                    [(x_cell, y_cell, layer)],
                    via.diameter_mm,
                )

        seeds = tuple(sorted(seed_segments))
        accepted_seeds: list[RouteSegment] = []
        for raw_segment in seeds:
            if raw_segment.net not in net_names:
                raise RoutingFailureError(
                    self._seed_failure(
                        "invalid_seed",
                        raw_segment,
                        "seed references an unknown net",
                    )
                )
            if (
                raw_segment.layer < 0
                or raw_segment.layer >= self.layer_count
                or not all(
                    math.isfinite(value)
                    for value in (
                        raw_segment.x1_mm,
                        raw_segment.y1_mm,
                        raw_segment.x2_mm,
                        raw_segment.y2_mm,
                        raw_segment.width_mm,
                    )
                )
                or raw_segment.width_mm < self.min_track_mm
            ):
                raise RoutingFailureError(
                    self._seed_failure(
                        "invalid_seed",
                        raw_segment,
                        "seed violates layer, width, or finite-coordinate bounds",
                    )
                )
            segment = self._normalize_seed_segment(raw_segment)
            path = self._segment_path(segment)
            pad_anchor = (segment.net, path[0][0], path[0][1], path[0][2])
            if not any(
                pad.net == segment.net
                and path[0][2] in pad.layers
                and self._point(pad.x_mm, pad.y_mm) == path[0][:2]
                for pad in pads_tuple
            ):
                raise RoutingFailureError(
                    self._seed_failure(
                        "invalid_seed",
                        segment,
                        "seed is not anchored to a routing pad",
                    )
                )
            if pad_anchor in self._seed_terminals:
                raise RoutingFailureError(
                    self._seed_failure(
                        "invalid_seed",
                        segment,
                        "routing pad has multiple seed segments",
                    )
                )
            terminal = (self._mm(path[-1][0]), self._mm(path[-1][1]))
            # Keep the supplied exact escape geometry.  For a non-zero escape
            # that collapses to one grid cell, normalization only chooses the
            # adjacent routing terminal; it must not replace the physical
            # segment with a different full-grid direction.
            materialized_seed = [raw_segment]
            if (raw_segment.x2_mm, raw_segment.y2_mm) != terminal:
                materialized_seed.append(
                    RouteSegment(
                        net=raw_segment.net,
                        layer=raw_segment.layer,
                        x1_mm=raw_segment.x2_mm,
                        y1_mm=raw_segment.y2_mm,
                        x2_mm=terminal[0],
                        y2_mm=terminal[1],
                        width_mm=raw_segment.width_mm,
                    )
                )
            obstruction = next(
                (
                    reason
                    for piece in materialized_seed
                    if (
                        reason := self._seed_obstruction(
                            piece,
                            pads_tuple,
                            keepouts_tuple,
                            (*obstacle_segments_tuple, *accepted_seeds),
                            obstacle_vias_tuple,
                        )
                    )
                    is not None
                ),
                None,
            )
            if obstruction is not None:
                self._seed_obstructions.setdefault(segment.net, []).append(obstruction)
                diagnostics.append(
                    f"{segment.net}: omitted obstructed optional fine-pitch escape "
                    f"({obstruction}) and routed from the pad"
                )
                continue
            self._reserve(segment.net, path, segment.width_mm)
            self._seed_terminals[pad_anchor] = path[-1]
            accepted_seeds.extend(materialized_seed)

        by_net: dict[str, list[RoutingPad]] = defaultdict(list)
        for pad in pads_tuple:
            by_net[pad.net].append(pad)
        power = set(power_nets)
        order = sorted(
            (net for net, members in by_net.items() if len(members) > 1),
            key=lambda net: (
                min(min(member.width_mm, member.height_mm) for member in by_net[net]),
                (
                    0
                    if net.upper() in {"GND", "GROUND", "VSS"}
                    else 1
                    if net in power
                    else 2
                ),
                -len(by_net[net]),
                net,
            ),
        )
        all_segments: list[RouteSegment] = accepted_seeds
        all_vias: list[RouteVia] = []
        unrouted: list[str] = []
        failures: list[RoutingFailure] = []
        for route_index, net in enumerate(order):
            width = widths_dict[net]
            segments, vias, errors = self._route_net(
                net,
                tuple(sorted(by_net[net])),
                width,
                route_order=tuple(order),
                route_index=route_index,
            )
            all_segments.extend(segments)
            all_vias.extend(vias)
            if errors:
                unrouted.append(net)
                failures.extend(errors)
                diagnostics.extend(error.diagnostic for error in errors)

        final_segments = tuple(sorted(set(all_segments)))
        final_vias = tuple(sorted(set(all_vias)))
        return RoutingResult(
            segments=final_segments,
            vias=final_vias,
            unrouted=tuple(sorted(unrouted)),
            state="heuristic" if unrouted else "completed",
            expanded_nodes=self.expanded_nodes,
            diagnostics=tuple(sorted(diagnostics)),
            failures=tuple(sorted(failures)),
        )

    def _normalize_seed_segment(self, segment: RouteSegment) -> RouteSegment:
        """Move a sub-grid non-zero seed to one adjacent cell when that is safe.

        Exact geometry checks still decide whether the normalized escape may be
        retained. An actually zero-length input has no direction to recover and
        therefore fails with a stable typed code.
        """

        start = self._point(segment.x1_mm, segment.y1_mm)
        end = self._point(segment.x2_mm, segment.y2_mm)
        if start != end:
            return segment
        delta_x = segment.x2_mm - segment.x1_mm
        delta_y = segment.y2_mm - segment.y1_mm
        if math.hypot(delta_x, delta_y) <= 1e-12:
            raise RoutingFailureError(
                self._seed_failure(
                    "zero_length_seed",
                    segment,
                    "seed has no recoverable direction",
                )
            )
        if abs(delta_x) >= abs(delta_y):
            end = (start[0] + (1 if delta_x > 0 else -1), start[1])
        else:
            end = (start[0], start[1] + (1 if delta_y > 0 else -1))
        if not (0 <= end[0] < self.width_cells and 0 <= end[1] < self.height_cells):
            raise RoutingFailureError(
                self._seed_failure(
                    "zero_length_seed",
                    segment,
                    "sub-grid seed cannot be normalized inside the board",
                )
            )
        return RouteSegment(
            net=segment.net,
            layer=segment.layer,
            x1_mm=segment.x1_mm,
            y1_mm=segment.y1_mm,
            x2_mm=self._mm(end[0]),
            y2_mm=self._mm(end[1]),
            width_mm=segment.width_mm,
        )

    def _seed_failure(
        self, code: str, segment: RouteSegment, summary: str
    ) -> RoutingFailure:
        coordinates = (
            segment.x1_mm,
            segment.y1_mm,
            segment.x2_mm,
            segment.y2_mm,
        )
        blocking_region = (
            (
                min(segment.x1_mm, segment.x2_mm),
                min(segment.y1_mm, segment.y2_mm),
                max(segment.x1_mm, segment.x2_mm),
                max(segment.y1_mm, segment.y2_mm),
            )
            if all(math.isfinite(item) for item in coordinates)
            else None
        )
        return RoutingFailure(
            code=code,
            net=segment.net or "unknown",
            endpoints=(
                f"{segment.x1_mm:.6g},{segment.y1_mm:.6g}",
                f"{segment.x2_mm:.6g},{segment.y2_mm:.6g}",
            ),
            blocking_summary=summary,
            recommendations=("inspect_pad_escape",),
            blocking_region=blocking_region,
            nearest_obstacle_class="unknown",
            state_revision=self._state_revision,
            state_context=(f"layer={segment.layer}",),
        )

    def _seed_obstruction(
        self,
        segment: RouteSegment,
        pads: tuple[RoutingPad, ...],
        keepouts: tuple[RoutingKeepout, ...],
        copper: tuple[RouteSegment, ...],
        vias: tuple[RouteVia, ...],
    ) -> str | None:
        """Check a short escape against exact geometry, before raster reservation.

        Fine-pitch pads can be legally separated by only a fraction more than the
        declared clearance.  Expanding rounded grid cells makes that legal gap
        disappear, so using ``_blocked`` here rejects the very escape that lets A*
        leave the pad.  Exact checks retain the physical rule; the grid remains the
        bounded search and reservation representation after acceptance.
        """
        radius = segment.width_mm / 2
        edge_limit = self.edge_clearance_mm + radius
        if not all(
            edge_limit <= x_mm <= self.board_width_mm - edge_limit
            and edge_limit <= y_mm <= self.board_height_mm - edge_limit
            for x_mm, y_mm in (
                (segment.x1_mm, segment.y1_mm),
                (segment.x2_mm, segment.y2_mm),
            )
        ):
            return "board-edge clearance"

        required = radius + self.clearance_mm
        line = (
            (segment.x1_mm, segment.y1_mm),
            (segment.x2_mm, segment.y2_mm),
        )
        for pad in pads:
            if segment.layer not in pad.layers or pad.net == segment.net:
                continue
            gap = _segment_rectangle_distance(
                line[0],
                line[1],
                pad.x_mm - pad.width_mm / 2,
                pad.y_mm - pad.height_mm / 2,
                pad.x_mm + pad.width_mm / 2,
                pad.y_mm + pad.height_mm / 2,
            )
            if gap + 1e-9 < required:
                return f"foreign pad {pad.id}"
        for keepout in keepouts:
            if segment.layer not in keepout.layers:
                continue
            gap = _segment_rectangle_distance(
                line[0],
                line[1],
                min(keepout.x1_mm, keepout.x2_mm),
                min(keepout.y1_mm, keepout.y2_mm),
                max(keepout.x1_mm, keepout.x2_mm),
                max(keepout.y1_mm, keepout.y2_mm),
            )
            if gap + 1e-9 < required:
                return f"keepout {keepout.id}"
        for other in copper:
            if other.layer != segment.layer or other.net == segment.net:
                continue
            gap = _segment_segment_distance(
                line[0],
                line[1],
                (other.x1_mm, other.y1_mm),
                (other.x2_mm, other.y2_mm),
            )
            required_between = radius + other.width_mm / 2 + self.clearance_mm
            if gap + 1e-9 < required_between:
                return f"escape for net {other.net}"
        for via in vias:
            if (
                segment.layer < via.from_layer
                or segment.layer > via.to_layer
                or via.net == segment.net
            ):
                continue
            gap = _point_segment_distance((via.x_mm, via.y_mm), line[0], line[1])
            required_between = radius + via.diameter_mm / 2 + self.clearance_mm
            if gap + 1e-9 < required_between:
                return f"via for net {via.net}"
        return None

    def _initialize_obstacles(
        self, pads: tuple[RoutingPad, ...], keepouts: tuple[RoutingKeepout, ...]
    ) -> None:
        self._pad_specs = [[] for _ in range(self.layer_count)]
        self._pad_cell_cache = {}
        self._raw_pad_cells = [defaultdict(set) for _ in range(self.layer_count)]
        self._keepout_cells = [set() for _ in range(self.layer_count)]
        self._occupied = [defaultdict(list) for _ in range(self.layer_count)]
        margin = self.clearance_mm + self._max_track_width / 2
        for pad in pads:
            center_x, center_y = self._point(pad.x_mm, pad.y_mm)
            raw_x = math.ceil(pad.width_mm / 2 / self.grid_mm)
            raw_y = math.ceil(pad.height_mm / 2 / self.grid_mm)
            for layer in pad.layers:
                self._pad_specs[layer].append(
                    (
                        center_x,
                        center_y,
                        pad.width_mm / 2,
                        pad.height_mm / 2,
                        pad.net,
                    )
                )
                for x_cell in range(center_x - raw_x, center_x + raw_x + 1):
                    for y_cell in range(center_y - raw_y, center_y + raw_y + 1):
                        if (
                            0 <= x_cell < self.width_cells
                            and 0 <= y_cell < self.height_cells
                        ):
                            self._raw_pad_cells[layer][(x_cell, y_cell)].add(pad.net)
        for keepout in keepouts:
            if not keepout.id or not keepout.layers:
                raise ValidationError("routing keepouts require id and layers")
            if any(layer < 0 or layer >= self.layer_count for layer in keepout.layers):
                raise ValidationError(
                    f"keepout {keepout.id!r} references an unavailable layer"
                )
            x1, x2 = sorted((keepout.x1_mm, keepout.x2_mm))
            y1, y2 = sorted((keepout.y1_mm, keepout.y2_mm))
            if (
                not all(math.isfinite(value) for value in (x1, x2, y1, y2))
                or x1 == x2
                or y1 == y2
            ):
                raise ValidationError(f"keepout {keepout.id!r} has invalid geometry")
            start_x, start_y = self._point(x1 - margin, y1 - margin)
            stop_x, stop_y = self._point(x2 + margin, y2 + margin)
            for layer in keepout.layers:
                for x_cell in range(
                    max(0, start_x), min(self.width_cells - 1, stop_x) + 1
                ):
                    for y_cell in range(
                        max(0, start_y), min(self.height_cells - 1, stop_y) + 1
                    ):
                        self._keepout_cells[layer].add((x_cell, y_cell))

    def _route_net(
        self,
        net: str,
        pads: tuple[RoutingPad, ...],
        width: float,
        *,
        route_order: tuple[str, ...],
        route_index: int,
    ) -> tuple[list[RouteSegment], list[RouteVia], list[RoutingFailure]]:
        ordered_pads = tuple(
            sorted(
                pads,
                key=lambda pad: (
                    min(pad.width_mm, pad.height_mm),
                    pad.width_mm * pad.height_mm,
                    pad.id,
                ),
            )
        )
        # Escape the most constrained fine-pitch pad before large easy terminals
        # build a tree that walls it in.
        first = ordered_pads[0]
        tree: set[GridState] = set(self._pad_terminal_states(first))
        pending = list(ordered_pads[1:])
        segments: list[RouteSegment] = []
        vias: list[RouteVia] = []
        failures: list[RoutingFailure] = []
        while pending:
            pending.sort(key=lambda pad: (self._pad_tree_distance(pad, tree), pad.id))
            pad = pending.pop(0)
            starts = self._pad_terminal_states(pad)
            before_expansions = self.expanded_nodes
            path, failure_kind = self._a_star(net, starts, tree, width)
            if path is None:
                code = (
                    "search_budget_exhausted"
                    if failure_kind
                    in {
                        "branch_search_budget_exhausted",
                        "total_search_budget_exhausted",
                    }
                    else failure_kind or "no_legal_channel"
                )
                known_seed_obstructions = self._seed_obstructions.get(net, [])
                if code == "no_legal_channel" and known_seed_obstructions:
                    code = "pad_escape_blocked"
                summary_kind = (
                    failure_kind
                    if failure_kind
                    in {
                        "branch_search_budget_exhausted",
                        "total_search_budget_exhausted",
                    }
                    else code
                )
                summary = self._failure_summary(summary_kind, known_seed_obstructions)
                failures.append(
                    RoutingFailure(
                        code=code,
                        net=net,
                        endpoints=(first.id, pad.id),
                        expanded_nodes=self.expanded_nodes - before_expansions,
                        blocking_summary=summary,
                        recommendations=self._failure_recommendations(code),
                        blocking_region=(
                            round(pad.x_mm - pad.width_mm / 2, 9),
                            round(pad.y_mm - pad.height_mm / 2, 9),
                            round(pad.x_mm + pad.width_mm / 2, 9),
                            round(pad.y_mm + pad.height_mm / 2, 9),
                        ),
                        nearest_obstacle_class=self._nearest_obstacle_class(
                            code, known_seed_obstructions
                        ),
                        state_revision=self._state_revision,
                        state_context=self._failure_state_context(
                            first,
                            pad,
                            route_order=route_order,
                            route_index=route_index,
                        ),
                    )
                )
                continue
            new_segments, new_vias = self._materialize(net, path, width)
            segments.extend(new_segments)
            vias.extend(new_vias)
            self._reserve(net, path, width)
            tree.update(path)
            for x_cell, y_cell, _layer in path:
                # A through via joins every copper layer at its location.
                if any(
                    via.x_mm == self._mm(x_cell) and via.y_mm == self._mm(y_cell)
                    for via in new_vias
                ):
                    tree.update(
                        (x_cell, y_cell, layer) for layer in range(self.layer_count)
                    )
        return segments, vias, failures

    def _failure_state_context(
        self,
        first: RoutingPad,
        target: RoutingPad,
        *,
        route_order: tuple[str, ...],
        route_index: int,
    ) -> tuple[str, ...]:
        previous = route_order[route_index - 1] if route_index else "none"
        following = (
            route_order[route_index + 1]
            if route_index + 1 < len(route_order)
            else "none"
        )
        return (
            (
                "placement="
                f"{first.id}@{first.x_mm:.9g},{first.y_mm:.9g};"
                f"{target.id}@{target.x_mm:.9g},{target.y_mm:.9g}"
            ),
            (
                "layers="
                f"board:{self.layer_count};{first.id}:{','.join(map(str, first.layers))};"
                f"{target.id}:{','.join(map(str, target.layers))}"
            ),
            f"order={route_index}/{len(route_order)};prev:{previous};next:{following}",
        )

    @staticmethod
    def _nearest_obstacle_class(code: str, seed_obstructions: list[str]) -> str:
        if code == "congestion_exhausted":
            return "copper"
        if code != "pad_escape_blocked" or not seed_obstructions:
            return "unknown"
        first = seed_obstructions[0]
        if first.startswith("foreign pad"):
            return "pad"
        if first.startswith("keepout"):
            return "keepout"
        if first.startswith("board-edge"):
            return "board_edge"
        if first.startswith(("escape for net", "via for net")):
            return "copper"
        return "unknown"

    @staticmethod
    def _failure_summary(code: str, seed_obstructions: list[str]) -> str:
        if code == "pad_escape_blocked":
            detail = ", ".join(sorted(set(seed_obstructions))[:3])
            return f"pad escape is blocked{': ' + detail if detail else ''}"
        if code == "total_search_budget_exhausted":
            return (
                "total routing expansion budget was exhausted within bounded A* search"
            )
        if code in {"branch_search_budget_exhausted", "search_budget_exhausted"}:
            return "per-branch routing expansion budget was exhausted"
        if code == "congestion_exhausted":
            return "foreign retained or routed copper occupies the search grid"
        return "bounded search found no legal channel"

    @staticmethod
    def _failure_recommendations(code: str) -> tuple[str, ...]:
        if code == "pad_escape_blocked":
            return ("inspect_pad_escape", "reposition_component")
        if code == "search_budget_exhausted":
            return ("change_net_order", "reposition_component")
        if code == "congestion_exhausted":
            return ("change_net_order", "change_layer", "reposition_component")
        return ("change_layer", "reposition_component")

    def _pad_tree_distance(self, pad: RoutingPad, tree: set[GridState]) -> int:
        return min(
            abs(state[0] - x) + abs(state[1] - y)
            for state in self._pad_terminal_states(pad)
            for x, y, _layer in tree
        )

    def _pad_terminal_states(self, pad: RoutingPad) -> tuple[GridState, ...]:
        x_cell, y_cell = self._point(pad.x_mm, pad.y_mm)
        return tuple(
            self._seed_terminals.get(
                (pad.net, x_cell, y_cell, layer), (x_cell, y_cell, layer)
            )
            for layer in pad.layers
        )

    def _a_star(
        self,
        net: str,
        starts: tuple[GridState, ...],
        goals: set[GridState],
        width: float,
    ) -> tuple[list[GridState] | None, str | None]:
        if any(start in goals for start in starts):
            return [next(start for start in starts if start in goals)], None
        goal_xy = tuple(sorted({(x, y) for x, y, _layer in goals}))

        def heuristic(state: GridState) -> int:
            x_cell, y_cell, _layer = state
            return min(abs(x_cell - x) + abs(y_cell - y) for x, y in goal_xy)

        heap: list[tuple[int, int, int, int, int, int]] = []
        costs: dict[GridState, int] = {}
        came_from: dict[GridState, GridState] = {}
        serial = itertools.count()
        for state in sorted(starts, key=lambda item: (item[2], item[1], item[0])):
            if (
                self._block_reason(
                    state, net, self._effective_width(net, state, width), via=False
                )
                is not None
            ):
                continue
            costs[state] = 0
            x_cell, y_cell, layer = state
            heapq.heappush(
                heap, (heuristic(state), 0, layer, y_cell, x_cell, next(serial))
            )
        if not heap:
            return None, "pad_escape_blocked"
        expanded = 0
        branch_budget = self.max_expansions
        total_limited = False
        if self._remaining_expansions is not None:
            total_limited = self._remaining_expansions <= branch_budget
            branch_budget = min(branch_budget, self._remaining_expansions)
        blocked_reasons: set[str] = set()
        while heap and expanded < branch_budget:
            _priority, cost, layer, y_cell, x_cell, _serial = heapq.heappop(heap)
            state = (x_cell, y_cell, layer)
            if cost != costs.get(state):
                continue
            expanded += 1
            if state in goals:
                self._record_expansions(expanded)
                return self._reconstruct(came_from, state), None
            neighbors: list[tuple[GridState, int, bool]] = [
                ((x_cell - 1, y_cell, layer), 1, False),
                ((x_cell, y_cell - 1, layer), 1, False),
                ((x_cell, y_cell + 1, layer), 1, False),
                ((x_cell + 1, y_cell, layer), 1, False),
            ]
            for other_layer in range(self.layer_count):
                if other_layer != layer:
                    # Cost vias well above a short planar detour, but keep them available
                    # for crossings and obstructed fan-out.
                    neighbors.append(((x_cell, y_cell, other_layer), 16, True))
            for neighbor, step_cost, is_via in neighbors:
                candidate_width = (
                    width if is_via else self._effective_width(net, neighbor, width)
                )
                blocked_reason = self._block_reason(
                    neighbor, net, candidate_width, via=is_via
                )
                if blocked_reason is not None:
                    blocked_reasons.add(blocked_reason)
                    continue
                if not is_via and any(
                    owner == net
                    for owner, _radius in self._occupied[neighbor[2]].get(
                        (neighbor[0], neighbor[1]), ()
                    )
                ):
                    step_cost = 0
                new_cost = cost + step_cost
                if new_cost >= costs.get(neighbor, 1 << 60):
                    continue
                costs[neighbor] = new_cost
                came_from[neighbor] = state
                nx, ny, next_layer = neighbor
                heapq.heappush(
                    heap,
                    (
                        new_cost + heuristic(neighbor),
                        new_cost,
                        next_layer,
                        ny,
                        nx,
                        next(serial),
                    ),
                )
        self._record_expansions(expanded)
        if expanded >= branch_budget and heap:
            return (
                None,
                "total_search_budget_exhausted"
                if total_limited
                else "branch_search_budget_exhausted",
            )
        if "foreign_copper" in blocked_reasons and not blocked_reasons & {
            "foreign_pad",
            "keepout",
            "via_on_pad",
        }:
            return None, "congestion_exhausted"
        return None, "no_legal_channel"

    def _record_expansions(self, count: int) -> None:
        self.expanded_nodes += count
        if self._remaining_expansions is not None:
            self._remaining_expansions -= count

    def _block_reason(
        self, state: GridState, net: str, width: float, *, via: bool
    ) -> str | None:
        x_cell, y_cell, layer = state
        radius = (
            self.via_diameter_mm / 2 if via else width / 2
        ) + self.edge_clearance_mm
        margin_cells = math.ceil(radius / self.grid_mm)
        if (
            x_cell < margin_cells
            or y_cell < margin_cells
            or x_cell >= self.width_cells - margin_cells
            or y_cell >= self.height_cells - margin_cells
        ):
            return "edge"
        layers = range(self.layer_count) if via else (layer,)
        pad_cells = self._pad_cells_for_width(self.via_diameter_mm if via else width)
        for check_layer in layers:
            cell = (x_cell, y_cell)
            if cell in self._keepout_cells[check_layer]:
                return "keepout"
            pad_nets = pad_cells[check_layer].get(cell, set())
            if pad_nets - {net}:
                return "foreign_pad"
            if self._occupied_conflict(
                check_layer,
                x_cell,
                y_cell,
                net,
                self.via_diameter_mm / 2 if via else width / 2,
            ):
                return "foreign_copper"
            if via and self._raw_pad_cells[check_layer].get(cell):
                return "via_on_pad"
        return None

    def _effective_width(
        self, net: str, state: GridState, nominal_width: float
    ) -> float:
        x_cell, y_cell, layer = state
        seeded = [
            radius * 2
            for owner, radius in self._occupied[layer].get((x_cell, y_cell), ())
            if owner == net
        ]
        return min((nominal_width, *seeded)) if seeded else nominal_width

    def _occupied_conflict(
        self,
        layer: int,
        x_cell: int,
        y_cell: int,
        net: str,
        radius_mm: float,
    ) -> bool:
        search = math.ceil(
            (radius_mm + self._max_occupied_radius + self.clearance_mm) / self.grid_mm
        )
        for delta_x in range(-search, search + 1):
            for delta_y in range(-search, search + 1):
                entries = self._occupied[layer].get(
                    (x_cell + delta_x, y_cell + delta_y), ()
                )
                if not entries:
                    continue
                distance = math.hypot(delta_x, delta_y) * self.grid_mm
                for owner, occupied_radius in entries:
                    if owner != net and distance < (
                        radius_mm + occupied_radius + self.clearance_mm - 1e-9
                    ):
                        return True
        return False

    def _segment_path(self, segment: RouteSegment) -> list[GridState]:
        start_x, start_y = self._point(segment.x1_mm, segment.y1_mm)
        end_x, end_y = self._point(segment.x2_mm, segment.y2_mm)
        if start_x == end_x and start_y == end_y:
            raise ValidationError("seed segments must have non-zero length")
        # Rasterize arbitrary seed angles with deterministic Bresenham cells.
        # Fine-pitch pad centers are commonly off-grid, so their short escape
        # tracks may need a shallow angle to terminate on an exact grid point.
        x_cell, y_cell = start_x, start_y
        delta_x = abs(end_x - start_x)
        step_x = 1 if start_x < end_x else -1
        delta_y = -abs(end_y - start_y)
        step_y = 1 if start_y < end_y else -1
        error = delta_x + delta_y
        path: list[GridState] = []
        while True:
            path.append((x_cell, y_cell, segment.layer))
            if x_cell == end_x and y_cell == end_y:
                return path
            doubled = 2 * error
            if doubled >= delta_y:
                error += delta_y
                x_cell += step_x
            if doubled <= delta_x:
                error += delta_x
                y_cell += step_y

    def _pad_cells_for_width(
        self, width: float
    ) -> list[dict[tuple[int, int], set[str]]]:
        """Expand pad obstacles for the active width instead of a global maximum."""
        key = round(width, 9)
        cached = self._pad_cell_cache.get(key)
        if cached is not None:
            return cached
        cells: list[dict[tuple[int, int], set[str]]] = [
            defaultdict(set) for _ in range(self.layer_count)
        ]
        margin = self.clearance_mm + width / 2
        for layer, specs in enumerate(self._pad_specs):
            for center_x, center_y, half_width, half_height, net in specs:
                # Equality at the specified clearance is legal; block grid cells
                # strictly inside the Minkowski-expanded pad boundary.
                radius_x = math.floor((half_width + margin - 1e-9) / self.grid_mm)
                radius_y = math.floor((half_height + margin - 1e-9) / self.grid_mm)
                for x_cell in range(center_x - radius_x, center_x + radius_x + 1):
                    for y_cell in range(center_y - radius_y, center_y + radius_y + 1):
                        if (
                            0 <= x_cell < self.width_cells
                            and 0 <= y_cell < self.height_cells
                        ):
                            cells[layer][(x_cell, y_cell)].add(net)
        self._pad_cell_cache[key] = cells
        return cells

    def _reserve(self, net: str, path: list[GridState], width: float) -> None:
        via_points: set[tuple[int, int]] = set()
        for first, second in itertools.pairwise(path):
            if first[2] != second[2]:
                via_points.add((second[0], second[1]))
        for x_cell, y_cell, layer in path:
            state = (x_cell, y_cell, layer)
            entry = (net, self._effective_width(net, state, width) / 2)
            if entry not in self._occupied[layer][(x_cell, y_cell)]:
                self._occupied[layer][(x_cell, y_cell)].append(entry)
        for x_cell, y_cell in via_points:
            for layer in range(self.layer_count):
                entry = (net, self.via_diameter_mm / 2)
                if entry not in self._occupied[layer][(x_cell, y_cell)]:
                    self._occupied[layer][(x_cell, y_cell)].append(entry)

    def _materialize(
        self, net: str, path: list[GridState], width: float
    ) -> tuple[list[RouteSegment], list[RouteVia]]:
        if len(path) < 2:
            return [], []
        segments: list[RouteSegment] = []
        vias: list[RouteVia] = []
        run_start: GridState | None = None
        run_end: GridState | None = None
        run_direction: tuple[int, int] | None = None
        run_width: float | None = None

        def flush() -> None:
            nonlocal run_start, run_end, run_direction, run_width
            if run_start is not None and run_end is not None and run_start != run_end:
                if run_width is None:
                    raise ValidationError("router segment width invariant failed")
                segments.append(self._segment(net, run_start, run_end, run_width))
            run_start = None
            run_end = None
            run_direction = None
            run_width = None

        for previous, current in itertools.pairwise(path):
            if current[2] != previous[2]:
                flush()
                vias.append(
                    RouteVia(
                        net=net,
                        x_mm=self._mm(current[0]),
                        y_mm=self._mm(current[1]),
                        diameter_mm=self.via_diameter_mm,
                        drill_mm=self.via_drill_mm,
                        from_layer=0,
                        to_layer=self.layer_count - 1,
                    )
                )
                continue
            direction = (current[0] - previous[0], current[1] - previous[1])
            edge_width = min(
                self._effective_width(net, previous, width),
                self._effective_width(net, current, width),
            )
            if (
                run_start is None
                or run_direction != direction
                or run_width != edge_width
                or run_end != previous
            ):
                flush()
                run_start = previous
                run_direction = direction
                run_width = edge_width
            run_end = current
        flush()
        return segments, vias

    def _segment(
        self, net: str, first: GridState, second: GridState, width: float
    ) -> RouteSegment:
        return RouteSegment(
            net=net,
            layer=first[2],
            x1_mm=self._mm(first[0]),
            y1_mm=self._mm(first[1]),
            x2_mm=self._mm(second[0]),
            y2_mm=self._mm(second[1]),
            width_mm=width,
        )

    @staticmethod
    def _reconstruct(
        came_from: dict[GridState, GridState], state: GridState
    ) -> list[GridState]:
        result = [state]
        while state in came_from:
            state = came_from[state]
            result.append(state)
        result.reverse()
        return result

    def _point(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        # Decimal half-pitch pad arrays must not alternately collapse from 0.5 mm
        # to 0.4 mm because Python's banker rounding chooses the even grid cell.
        # A stable half-up rule preserves their pitch on a 0.1 mm routing grid.
        return self._grid_index(x_mm), self._grid_index(y_mm)

    def _grid_index(self, value_mm: float) -> int:
        return math.floor(value_mm / self.grid_mm + 0.5 + 1e-9)

    def _mm(self, cell: int) -> float:
        return round(cell * self.grid_mm, 9)


def _segment_rectangle_distance(
    first: tuple[float, float],
    second: tuple[float, float],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Return the exact Euclidean gap from a segment to a closed rectangle."""
    if (
        x1 <= first[0] <= x2
        and y1 <= first[1] <= y2
        or x1 <= second[0] <= x2
        and y1 <= second[1] <= y2
    ):
        return 0.0
    corners = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    return min(
        _segment_segment_distance(first, second, edge_first, edge_second)
        for edge_first, edge_second in zip(
            corners, (*corners[1:], corners[0]), strict=True
        )
    )


def _segment_segment_distance(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> float:
    if _segments_intersect(first_start, first_end, second_start, second_end):
        return 0.0
    return min(
        _point_segment_distance(first_start, second_start, second_end),
        _point_segment_distance(first_end, second_start, second_end),
        _point_segment_distance(second_start, first_start, first_end),
        _point_segment_distance(second_end, first_start, first_end),
    )


def _point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 1e-18:
        return math.dist(point, start)
    fraction = (
        (point[0] - start[0]) * delta_x + (point[1] - start[1]) * delta_y
    ) / length_squared
    fraction = min(1.0, max(0.0, fraction))
    closest = (start[0] + fraction * delta_x, start[1] + fraction * delta_y)
    return math.dist(point, closest)


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    def orientation(
        first: tuple[float, float],
        second: tuple[float, float],
        third: tuple[float, float],
    ) -> float:
        return (second[0] - first[0]) * (third[1] - first[1]) - (
            second[1] - first[1]
        ) * (third[0] - first[0])

    def on_segment(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> bool:
        return (
            min(start[0], end[0]) - 1e-9 <= point[0] <= max(start[0], end[0]) + 1e-9
            and min(start[1], end[1]) - 1e-9 <= point[1] <= max(start[1], end[1]) + 1e-9
            and abs(orientation(start, end, point)) <= 1e-9
        )

    first_side = orientation(first_start, first_end, second_start)
    second_side = orientation(first_start, first_end, second_end)
    third_side = orientation(second_start, second_end, first_start)
    fourth_side = orientation(second_start, second_end, first_end)
    if first_side * second_side < -1e-18 and third_side * fourth_side < -1e-18:
        return True
    return any(
        (abs(side) <= 1e-9 and on_segment(point, segment_start, segment_end))
        for side, point, segment_start, segment_end in (
            (first_side, second_start, first_start, first_end),
            (second_side, second_end, first_start, first_end),
            (third_side, first_start, second_start, second_end),
            (fourth_side, first_end, second_start, second_end),
        )
    )
