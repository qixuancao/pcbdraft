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

from .errors import ValidationError

MAX_CELLS = 2_000_000
MAX_PADS = 2_000
MAX_NETS = 1_000
MAX_EXPANSIONS_PER_BRANCH = 750_000


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


@dataclass(frozen=True)
class RoutingResult:
    segments: tuple[RouteSegment, ...]
    vias: tuple[RouteVia, ...]
    unrouted: tuple[str, ...]
    state: str
    expanded_nodes: int
    diagnostics: tuple[str, ...]


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
        if layers not in {2, 3, 4}:
            raise ValidationError("router supports only 2-4 copper layers")
        if not 1 <= max_expansions <= MAX_EXPANSIONS_PER_BRANCH:
            raise ValidationError(
                f"max_expansions must be 1..{MAX_EXPANSIONS_PER_BRANCH}"
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
        self._pad_specs: list[list[tuple[int, int, float, float, str]]] = []
        self._pad_cell_cache: dict[float, list[dict[tuple[int, int], set[str]]]] = {}
        self._raw_pad_cells: list[dict[tuple[int, int], set[str]]] = []
        self._keepout_cells: list[set[tuple[int, int]]] = []
        self._occupied: list[dict[tuple[int, int], list[tuple[str, float]]]] = []
        self._seed_terminals: dict[tuple[str, int, int, int], GridState] = {}
        self._max_track_width = min_track_mm
        self._max_occupied_radius = max(min_track_mm / 2, self.via_diameter_mm / 2)
        self.expanded_nodes = 0

    def route(
        self,
        pads: Iterable[RoutingPad],
        *,
        widths: Mapping[str, float] | None = None,
        keepouts: Iterable[RoutingKeepout] = (),
        power_nets: Iterable[str] = (),
        seed_segments: Iterable[RouteSegment] = (),
    ) -> RoutingResult:
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
        self._initialize_obstacles(pads_tuple, tuple(sorted(keepouts)))
        self._seed_terminals = {}
        self.expanded_nodes = 0

        seeds = tuple(sorted(seed_segments))
        for segment in seeds:
            if segment.net not in net_names:
                raise ValidationError(
                    f"seed segment references unknown net: {segment.net}"
                )
            if (
                segment.layer < 0
                or segment.layer >= self.layer_count
                or segment.width_mm < self.min_track_mm
            ):
                raise ValidationError("seed segment violates layer or width bounds")
            path = self._segment_path(segment)
            pad_anchor = (segment.net, path[0][0], path[0][1], path[0][2])
            if not any(
                pad.net == segment.net
                and path[0][2] in pad.layers
                and self._point(pad.x_mm, pad.y_mm) == path[0][:2]
                for pad in pads_tuple
            ):
                raise ValidationError(
                    f"seed segment is not anchored to a routing pad: {segment.net}"
                )
            if pad_anchor in self._seed_terminals:
                raise ValidationError(
                    f"routing pad has multiple seed segments: {segment.net}"
                )
            if any(
                self._blocked(state, segment.net, segment.width_mm, via=False)
                for state in path
            ):
                raise ValidationError(
                    f"seed segment collides with constrained geometry: {segment.net}"
                )
            self._reserve(segment.net, path, segment.width_mm)
            self._seed_terminals[pad_anchor] = path[-1]

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
        all_segments: list[RouteSegment] = list(seeds)
        all_vias: list[RouteVia] = []
        diagnostics: list[str] = []
        unrouted: list[str] = []
        for net in order:
            width = widths_dict[net]
            segments, vias, errors = self._route_net(
                net, tuple(sorted(by_net[net])), width
            )
            all_segments.extend(segments)
            all_vias.extend(vias)
            if errors:
                unrouted.append(net)
                diagnostics.extend(errors)

        segments = tuple(sorted(set(all_segments)))
        vias = tuple(sorted(set(all_vias)))
        return RoutingResult(
            segments=segments,
            vias=vias,
            unrouted=tuple(sorted(unrouted)),
            state="heuristic" if unrouted else "completed",
            expanded_nodes=self.expanded_nodes,
            diagnostics=tuple(sorted(diagnostics)),
        )

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
        self, net: str, pads: tuple[RoutingPad, ...], width: float
    ) -> tuple[list[RouteSegment], list[RouteVia], list[str]]:
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
        diagnostics: list[str] = []
        while pending:
            pending.sort(key=lambda pad: (self._pad_tree_distance(pad, tree), pad.id))
            pad = pending.pop(0)
            starts = self._pad_terminal_states(pad)
            path = self._a_star(net, starts, tree, width)
            if path is None:
                diagnostics.append(
                    f"{net}: could not connect pad {pad.id} within bounded A* search"
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
        return segments, vias, diagnostics

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
    ) -> list[GridState] | None:
        if any(start in goals for start in starts):
            return [next(start for start in starts if start in goals)]
        goal_xy = tuple(sorted({(x, y) for x, y, _layer in goals}))

        def heuristic(state: GridState) -> int:
            x_cell, y_cell, _layer = state
            return min(abs(x_cell - x) + abs(y_cell - y) for x, y in goal_xy)

        heap: list[tuple[int, int, int, int, int, int]] = []
        costs: dict[GridState, int] = {}
        came_from: dict[GridState, GridState] = {}
        serial = itertools.count()
        for state in sorted(starts, key=lambda item: (item[2], item[1], item[0])):
            if self._blocked(
                state, net, self._effective_width(net, state, width), via=False
            ):
                continue
            costs[state] = 0
            x_cell, y_cell, layer = state
            heapq.heappush(
                heap, (heuristic(state), 0, layer, y_cell, x_cell, next(serial))
            )
        expanded = 0
        while heap and expanded < self.max_expansions:
            _priority, cost, layer, y_cell, x_cell, _serial = heapq.heappop(heap)
            state = (x_cell, y_cell, layer)
            if cost != costs.get(state):
                continue
            expanded += 1
            if state in goals:
                self.expanded_nodes += expanded
                return self._reconstruct(came_from, state)
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
                if self._blocked(neighbor, net, candidate_width, via=is_via):
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
        self.expanded_nodes += expanded
        return None

    def _blocked(self, state: GridState, net: str, width: float, *, via: bool) -> bool:
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
            return True
        layers = range(self.layer_count) if via else (layer,)
        pad_cells = self._pad_cells_for_width(self.via_diameter_mm if via else width)
        for check_layer in layers:
            cell = (x_cell, y_cell)
            if cell in self._keepout_cells[check_layer]:
                return True
            pad_nets = pad_cells[check_layer].get(cell, set())
            if pad_nets - {net}:
                return True
            if self._occupied_conflict(
                check_layer,
                x_cell,
                y_cell,
                net,
                self.via_diameter_mm / 2 if via else width / 2,
            ):
                return True
            if via and self._raw_pad_cells[check_layer].get(cell):
                return True
        return False

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
                assert run_width is not None
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
        return round(x_mm / self.grid_mm), round(y_mm / self.grid_mm)

    def _mm(self, cell: int) -> float:
        return round(cell * self.grid_mm, 9)
