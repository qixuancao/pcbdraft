"""Bounded deterministic placement optimization for small low-risk boards.

The optimizer deliberately solves a constrained, auditable problem rather than
pretending to replace a placement engineer.  It snaps movable footprints to a
grid, preserves fixed mechanical items, rejects impossible fixed geometry, and
optimizes overlap, edge clearance, net length, proximity, and group contracts.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.spatial_contracts import (
    BOARD_REGIONS,
    board_region_bounds,
    rectangles_overlap,
)

MAX_ITEMS = 200
MAX_NETS = 1000
MAX_ITERATIONS = 100


@dataclass(frozen=True, order=True)
class PlacementItem:
    id: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    rotation_deg: float = 0.0
    fixed: bool = False

    def __post_init__(self) -> None:
        values = (
            self.x_mm,
            self.y_mm,
            self.width_mm,
            self.height_mm,
            self.rotation_deg,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValidationError(
                f"placement item {self.id!r} contains non-finite values"
            )
        if not self.id or self.width_mm <= 0 or self.height_mm <= 0:
            raise ValidationError(f"placement item {self.id!r} has invalid geometry")

    @property
    def half_width(self) -> float:
        turns = round(self.rotation_deg / 90) % 2
        return (self.height_mm if turns else self.width_mm) / 2

    @property
    def half_height(self) -> float:
        turns = round(self.rotation_deg / 90) % 2
        return (self.width_mm if turns else self.height_mm) / 2


@dataclass(frozen=True, order=True)
class NearConstraint:
    first: str
    second: str
    max_distance_mm: float
    weight: float = 20.0


@dataclass(frozen=True, order=True)
class GroupConstraint:
    members: tuple[str, ...]
    max_diameter_mm: float
    weight: float = 5.0


@dataclass(frozen=True, order=True)
class RegionConstraint:
    members: tuple[str, ...]
    region: str
    weight: float = 1_000_000.0


@dataclass(frozen=True, order=True)
class PlacementKeepout:
    id: str
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float


@dataclass(frozen=True)
class PlacementResult:
    items: tuple[PlacementItem, ...]
    objective: float
    iterations: int
    state: str
    diagnostics: tuple[str, ...]

    def by_id(self) -> dict[str, PlacementItem]:
        return {item.id: item for item in self.items}


def optimize_placement(
    items: Iterable[PlacementItem],
    *,
    board_width_mm: float,
    board_height_mm: float,
    edge_clearance_mm: float,
    nets: Iterable[Iterable[str]] = (),
    near: Iterable[NearConstraint] = (),
    groups: Iterable[GroupConstraint] = (),
    regions: Iterable[RegionConstraint] = (),
    keepouts: Iterable[PlacementKeepout] = (),
    grid_mm: float = 0.5,
    max_iterations: int = 40,
) -> PlacementResult:
    """Run deterministic coordinate descent with hard geometry penalties."""
    sequence = tuple(sorted(items, key=lambda item: item.id))
    if not sequence or len(sequence) > MAX_ITEMS:
        raise ValidationError(f"placement requires 1..{MAX_ITEMS} items")
    if len({item.id for item in sequence}) != len(sequence):
        raise ValidationError("placement item ids must be unique")
    for name, value in {
        "board_width_mm": board_width_mm,
        "board_height_mm": board_height_mm,
        "edge_clearance_mm": edge_clearance_mm,
        "grid_mm": grid_mm,
    }.items():
        if not math.isfinite(value) or value <= 0:
            raise ValidationError(f"{name} must be a positive finite number")
    if not 1 <= max_iterations <= MAX_ITERATIONS:
        raise ValidationError(f"max_iterations must be 1..{MAX_ITERATIONS}")

    known = {item.id for item in sequence}
    net_groups = tuple(tuple(sorted(set(net))) for net in nets if len(set(net)) > 1)
    if len(net_groups) > MAX_NETS:
        raise ValidationError(f"placement supports at most {MAX_NETS} nets")
    near_constraints = tuple(sorted(near))
    group_constraints = tuple(sorted(groups))
    region_constraints = tuple(sorted(regions))
    placement_keepouts = tuple(sorted(keepouts))
    referenced = (
        {item for net in net_groups for item in net}
        | {
            item
            for constraint in near_constraints
            for item in (constraint.first, constraint.second)
        }
        | {item for constraint in group_constraints for item in constraint.members}
        | {item for constraint in region_constraints for item in constraint.members}
    )
    unknown = referenced - known
    if unknown:
        raise ValidationError(
            "placement constraints reference unknown items: "
            + ", ".join(sorted(unknown))
        )
    for near_constraint in near_constraints:
        if near_constraint.max_distance_mm <= 0 or near_constraint.weight <= 0:
            raise ValidationError(
                "near constraints require positive distance and weight"
            )
    for group_constraint in group_constraints:
        if (
            len(set(group_constraint.members)) < 2
            or group_constraint.max_diameter_mm <= 0
            or group_constraint.weight <= 0
        ):
            raise ValidationError(
                "group constraints require two members and positive bounds"
            )
    for region_constraint in region_constraints:
        if (
            not region_constraint.members
            or region_constraint.region not in BOARD_REGIONS
            or region_constraint.weight <= 0
        ):
            raise ValidationError(
                "region constraints require members, a supported region, and positive weight"
            )
    for keepout in placement_keepouts:
        if (
            not keepout.id
            or not all(
                math.isfinite(value)
                for value in (
                    keepout.x1_mm,
                    keepout.y1_mm,
                    keepout.x2_mm,
                    keepout.y2_mm,
                )
            )
            or keepout.x1_mm >= keepout.x2_mm
            or keepout.y1_mm >= keepout.y2_mm
        ):
            raise ValidationError(
                "placement keepouts require valid rectangular geometry"
            )

    fixed = tuple(item for item in sequence if item.fixed)
    hard_issues = _hard_geometry_issues(
        fixed,
        board_width_mm,
        board_height_mm,
        edge_clearance_mm,
        placement_keepouts,
    )
    if hard_issues:
        raise ValidationError("invalid fixed placement: " + "; ".join(hard_issues))

    current = {
        item.id: replace(
            item,
            x_mm=_snap(item.x_mm, grid_mm),
            y_mm=_snap(item.y_mm, grid_mm),
        )
        if not item.fixed
        else item
        for item in sequence
    }
    objective = _objective(
        current,
        board_width_mm,
        board_height_mm,
        edge_clearance_mm,
        net_groups,
        near_constraints,
        group_constraints,
        region_constraints,
        placement_keepouts,
    )
    completed_iterations = 0
    for iteration in range(max_iterations):
        changed = False
        for item_id in sorted(current):
            original = current[item_id]
            if original.fixed:
                continue
            best = original
            best_score = objective
            for x_mm, y_mm in _candidates(
                original,
                board_width_mm,
                board_height_mm,
                edge_clearance_mm,
                grid_mm,
            ):
                candidate = replace(original, x_mm=x_mm, y_mm=y_mm)
                current[item_id] = candidate
                score = _objective(
                    current,
                    board_width_mm,
                    board_height_mm,
                    edge_clearance_mm,
                    net_groups,
                    near_constraints,
                    group_constraints,
                    region_constraints,
                    placement_keepouts,
                )
                # Stable coordinate tie-break makes repeated runs byte-for-byte equal.
                if score < best_score - 1e-9 or (
                    abs(score - best_score) <= 1e-9
                    and (candidate.x_mm, candidate.y_mm) < (best.x_mm, best.y_mm)
                ):
                    best = candidate
                    best_score = score
            current[item_id] = best
            if best != original:
                changed = True
                objective = best_score
        completed_iterations = iteration + 1
        if not changed:
            break

    issues = _hard_geometry_issues(
        tuple(current.values()),
        board_width_mm,
        board_height_mm,
        edge_clearance_mm,
        placement_keepouts,
    )
    soft_issues = _constraint_issues(
        current,
        near_constraints,
        group_constraints,
        region_constraints,
        board_width_mm,
        board_height_mm,
    )
    diagnostics = tuple(sorted((*issues, *soft_issues)))
    state = "completed" if not diagnostics else "heuristic"
    return PlacementResult(
        items=tuple(sorted(current.values(), key=lambda item: item.id)),
        objective=round(objective, 9),
        iterations=completed_iterations,
        state=state,
        diagnostics=diagnostics,
    )


def _snap(value: float, grid: float) -> float:
    return round(round(value / grid) * grid, 9)


def _candidates(
    item: PlacementItem,
    board_width: float,
    board_height: float,
    edge: float,
    grid: float,
) -> tuple[tuple[float, float], ...]:
    min_x = _snap(edge + item.half_width, grid)
    max_x = _snap(board_width - edge - item.half_width, grid)
    min_y = _snap(edge + item.half_height, grid)
    max_y = _snap(board_height - edge - item.half_height, grid)
    if min_x > max_x or min_y > max_y:
        return ((item.x_mm, item.y_mm),)
    points: set[tuple[float, float]] = {
        (min(max(item.x_mm, min_x), max_x), min(max(item.y_mm, min_y), max_y))
    }
    # Local moves plus all cardinal grid lines give escape paths from initial overlap.
    for step in range(-12, 13):
        points.add(
            (
                min(max(item.x_mm + step * grid, min_x), max_x),
                min(max(item.y_mm, min_y), max_y),
            )
        )
        points.add(
            (
                min(max(item.x_mm, min_x), max_x),
                min(max(item.y_mm + step * grid, min_y), max_y),
            )
        )
    for x_mm in _grid_values(min_x, max_x, grid, limit=120):
        points.add((x_mm, min(max(item.y_mm, min_y), max_y)))
    for y_mm in _grid_values(min_y, max_y, grid, limit=120):
        points.add((min(max(item.x_mm, min_x), max_x), y_mm))
    return tuple(sorted(points))


def _grid_values(
    start: float, stop: float, grid: float, *, limit: int
) -> tuple[float, ...]:
    count = math.floor((stop - start) / grid) + 1
    if count <= limit:
        return tuple(_snap(start + index * grid, grid) for index in range(count))
    stride = math.ceil(count / limit)
    values = {_snap(start + index * grid, grid) for index in range(0, count, stride)}
    values.add(stop)
    return tuple(sorted(values))


def _objective(
    items: Mapping[str, PlacementItem],
    board_width: float,
    board_height: float,
    edge: float,
    nets: tuple[tuple[str, ...], ...],
    near: tuple[NearConstraint, ...],
    groups: tuple[GroupConstraint, ...],
    regions: tuple[RegionConstraint, ...],
    keepouts: tuple[PlacementKeepout, ...],
) -> float:
    result = 0.0
    sequence = tuple(items[key] for key in sorted(items))
    for item in sequence:
        violation = max(0.0, edge + item.half_width - item.x_mm)
        violation += max(0.0, item.x_mm + item.half_width + edge - board_width)
        violation += max(0.0, edge + item.half_height - item.y_mm)
        violation += max(0.0, item.y_mm + item.half_height + edge - board_height)
        result += 1_000_000 * violation
    for index, first in enumerate(sequence):
        for second in sequence[index + 1 :]:
            overlap_x = max(
                0.0,
                first.half_width + second.half_width - abs(first.x_mm - second.x_mm),
            )
            overlap_y = max(
                0.0,
                first.half_height + second.half_height - abs(first.y_mm - second.y_mm),
            )
            result += 1_000_000 * overlap_x * overlap_y
    for item in sequence:
        item_bounds = _item_bounds(item)
        for keepout in keepouts:
            keepout_bounds = (
                keepout.x1_mm,
                keepout.y1_mm,
                keepout.x2_mm,
                keepout.y2_mm,
            )
            if rectangles_overlap(item_bounds, keepout_bounds):
                overlap_x = min(item_bounds[2], keepout_bounds[2]) - max(
                    item_bounds[0], keepout_bounds[0]
                )
                overlap_y = min(item_bounds[3], keepout_bounds[3]) - max(
                    item_bounds[1], keepout_bounds[1]
                )
                result += 1_000_000 * overlap_x * overlap_y
    for net in nets:
        # Half-perimeter wire length is stable and inexpensive for multi-terminal nets.
        xs = [items[item].x_mm for item in net]
        ys = [items[item].y_mm for item in net]
        result += (max(xs) - min(xs)) + (max(ys) - min(ys))
    for near_constraint in near:
        distance = _distance(
            items[near_constraint.first], items[near_constraint.second]
        )
        result += (
            near_constraint.weight
            * max(0.0, distance - near_constraint.max_distance_mm) ** 2
        )
    for group_constraint in groups:
        diameter = max(
            _distance(items[first], items[second])
            for offset, first in enumerate(group_constraint.members)
            for second in group_constraint.members[offset + 1 :]
        )
        result += (
            group_constraint.weight
            * max(0.0, diameter - group_constraint.max_diameter_mm) ** 2
        )
    for region_constraint in regions:
        bounds = board_region_bounds(
            region_constraint.region, board_width, board_height
        )
        for member in region_constraint.members:
            item = items[member]
            violation = _rectangle_containment_violation(_item_bounds(item), bounds)
            result += region_constraint.weight * violation
    return result


def _distance(first: PlacementItem, second: PlacementItem) -> float:
    return math.hypot(first.x_mm - second.x_mm, first.y_mm - second.y_mm)


def _hard_geometry_issues(
    items: tuple[PlacementItem, ...],
    board_width: float,
    board_height: float,
    edge: float,
    keepouts: tuple[PlacementKeepout, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    sequence = tuple(sorted(items, key=lambda item: item.id))
    for item in sequence:
        if (
            item.x_mm - item.half_width < edge - 1e-9
            or item.x_mm + item.half_width > board_width - edge + 1e-9
            or item.y_mm - item.half_height < edge - 1e-9
            or item.y_mm + item.half_height > board_height - edge + 1e-9
        ):
            issues.append(f"{item.id} violates board edge clearance")
    for index, first in enumerate(sequence):
        for second in sequence[index + 1 :]:
            if (
                abs(first.x_mm - second.x_mm)
                < first.half_width + second.half_width - 1e-9
                and abs(first.y_mm - second.y_mm)
                < first.half_height + second.half_height - 1e-9
            ):
                issues.append(f"{first.id} overlaps {second.id}")
    for item in sequence:
        bounds = _item_bounds(item)
        for keepout in keepouts:
            if rectangles_overlap(
                bounds,
                (keepout.x1_mm, keepout.y1_mm, keepout.x2_mm, keepout.y2_mm),
            ):
                issues.append(f"{item.id} overlaps placement keepout {keepout.id}")
    return tuple(issues)


def _constraint_issues(
    items: Mapping[str, PlacementItem],
    near: tuple[NearConstraint, ...],
    groups: tuple[GroupConstraint, ...],
    regions: tuple[RegionConstraint, ...],
    board_width: float,
    board_height: float,
) -> tuple[str, ...]:
    issues: list[str] = []
    for near_constraint in near:
        distance = _distance(
            items[near_constraint.first], items[near_constraint.second]
        )
        if distance > near_constraint.max_distance_mm + 1e-9:
            issues.append(
                f"{near_constraint.first}/{near_constraint.second} distance {distance:.3f} mm exceeds {near_constraint.max_distance_mm:.3f} mm"
            )
    for group_constraint in groups:
        diameter = max(
            _distance(items[first], items[second])
            for offset, first in enumerate(group_constraint.members)
            for second in group_constraint.members[offset + 1 :]
        )
        if diameter > group_constraint.max_diameter_mm + 1e-9:
            issues.append(
                f"group {'/'.join(group_constraint.members)} diameter {diameter:.3f} mm exceeds {group_constraint.max_diameter_mm:.3f} mm"
            )
    for region_constraint in regions:
        region = board_region_bounds(
            region_constraint.region,
            board_width,
            board_height,
        )
        for member in region_constraint.members:
            if (
                _rectangle_containment_violation(_item_bounds(items[member]), region)
                > 1e-9
            ):
                issues.append(
                    f"{member} lies outside placement region {region_constraint.region}"
                )
    return tuple(issues)


def _item_bounds(item: PlacementItem) -> tuple[float, float, float, float]:
    return (
        item.x_mm - item.half_width,
        item.y_mm - item.half_height,
        item.x_mm + item.half_width,
        item.y_mm + item.half_height,
    )


def _rectangle_containment_violation(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    return (
        max(0.0, outer[0] - inner[0])
        + max(0.0, outer[1] - inner[1])
        + max(0.0, inner[2] - outer[2])
        + max(0.0, inner[3] - outer[3])
    )
