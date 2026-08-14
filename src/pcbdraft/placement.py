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

from .errors import ValidationError

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
    referenced = (
        {item for net in net_groups for item in net}
        | {
            item
            for constraint in near_constraints
            for item in (constraint.first, constraint.second)
        }
        | {item for constraint in group_constraints for item in constraint.members}
    )
    unknown = referenced - known
    if unknown:
        raise ValidationError(
            "placement constraints reference unknown items: "
            + ", ".join(sorted(unknown))
        )
    for constraint in near_constraints:
        if constraint.max_distance_mm <= 0 or constraint.weight <= 0:
            raise ValidationError(
                "near constraints require positive distance and weight"
            )
    for constraint in group_constraints:
        if (
            len(set(constraint.members)) < 2
            or constraint.max_diameter_mm <= 0
            or constraint.weight <= 0
        ):
            raise ValidationError(
                "group constraints require two members and positive bounds"
            )

    fixed = tuple(item for item in sequence if item.fixed)
    hard_issues = _hard_geometry_issues(
        fixed,
        board_width_mm,
        board_height_mm,
        edge_clearance_mm,
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
    )
    soft_issues = _constraint_issues(current, near_constraints, group_constraints)
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
    for net in nets:
        # Half-perimeter wire length is stable and inexpensive for multi-terminal nets.
        xs = [items[item].x_mm for item in net]
        ys = [items[item].y_mm for item in net]
        result += (max(xs) - min(xs)) + (max(ys) - min(ys))
    for constraint in near:
        distance = _distance(items[constraint.first], items[constraint.second])
        result += (
            constraint.weight * max(0.0, distance - constraint.max_distance_mm) ** 2
        )
    for constraint in groups:
        diameter = max(
            _distance(items[first], items[second])
            for offset, first in enumerate(constraint.members)
            for second in constraint.members[offset + 1 :]
        )
        result += (
            constraint.weight * max(0.0, diameter - constraint.max_diameter_mm) ** 2
        )
    return result


def _distance(first: PlacementItem, second: PlacementItem) -> float:
    return math.hypot(first.x_mm - second.x_mm, first.y_mm - second.y_mm)


def _hard_geometry_issues(
    items: tuple[PlacementItem, ...],
    board_width: float,
    board_height: float,
    edge: float,
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
    return tuple(issues)


def _constraint_issues(
    items: Mapping[str, PlacementItem],
    near: tuple[NearConstraint, ...],
    groups: tuple[GroupConstraint, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    for constraint in near:
        distance = _distance(items[constraint.first], items[constraint.second])
        if distance > constraint.max_distance_mm + 1e-9:
            issues.append(
                f"{constraint.first}/{constraint.second} distance {distance:.3f} mm exceeds {constraint.max_distance_mm:.3f} mm"
            )
    for constraint in groups:
        diameter = max(
            _distance(items[first], items[second])
            for offset, first in enumerate(constraint.members)
            for second in constraint.members[offset + 1 :]
        )
        if diameter > constraint.max_diameter_mm + 1e-9:
            issues.append(
                f"group {'/'.join(constraint.members)} diameter {diameter:.3f} mm exceeds {constraint.max_diameter_mm:.3f} mm"
            )
    return tuple(issues)
