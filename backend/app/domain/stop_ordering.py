"""Global stop-order optimization before detailed day scheduling."""

from __future__ import annotations

from itertools import permutations
from math import inf

from app.models.context import PlanningContext, ResolvedStop
from app.domain.route_lookup import CachedRouteLookup, RoutePoint


async def optimized_stop_order_variants(
    context: PlanningContext,
    stops: list[ResolvedStop],
    routes: CachedRouteLookup,
    mode: str,
    *,
    maximum_variants: int = 12,
) -> list[list[ResolvedStop]]:
    """Rank stop orders by provider road time, including the origin and end."""

    if len(stops) < 2:
        return [list(stops)]
    fixed = _explicit_fixed_order(context, stops)
    if fixed is not None:
        return [fixed]

    points = [
        RoutePoint(context.origin),
        *(_stop_point(stop) for stop in stops),
        RoutePoint(context.end_location or context.origin),
    ]
    matrix = await routes.get_matrix(points, mode)
    size = len(stops)
    if size <= 8:
        ranked = sorted(
            permutations(range(size)),
            key=lambda order: _matrix_score(order, matrix.durations_minutes),
        )
    else:
        seeds = [
            _nearest_neighbour_order(start, size, matrix.durations_minutes)
            for start in range(size)
        ]
        ranked = sorted(
            (_two_opt(order, matrix.durations_minutes) for order in seeds),
            key=lambda order: _matrix_score(order, matrix.durations_minutes),
        )

    result: list[list[ResolvedStop]] = []
    seen: set[tuple[int, ...]] = set()
    for order in ranked:
        key = tuple(order)
        if key in seen or _matrix_score(key, matrix.durations_minutes) == inf:
            continue
        seen.add(key)
        result.append([stops[index] for index in key])
        if len(result) >= maximum_variants:
            break
    return result or [list(stops)]


def _explicit_fixed_order(
    context: PlanningContext,
    stops: list[ResolvedStop],
) -> list[ResolvedStop] | None:
    requested = context.requirements.get("fixed_stop_order")
    if requested is True:
        return list(stops)
    if not isinstance(requested, list):
        return None
    by_name = {stop.name.casefold(): stop for stop in stops}
    keys = [str(name).casefold() for name in requested]
    if (
        len(keys) == len(stops)
        and len(set(keys)) == len(keys)
        and set(keys) == set(by_name)
    ):
        return [by_name[key] for key in keys]
    return None


def _matrix_score(
    order: tuple[int, ...] | list[int], matrix: list[list[int | None]]
) -> float:
    indices = [0, *(index + 1 for index in order), len(matrix) - 1]
    total = 0.0
    for origin, destination in zip(indices, indices[1:], strict=False):
        value = matrix[origin][destination]
        if value is None:
            return inf
        total += value
    return total


def _nearest_neighbour_order(
    first: int,
    size: int,
    matrix: list[list[int | None]],
) -> list[int]:
    remaining = set(range(size))
    remaining.remove(first)
    order = [first]
    while remaining:
        current = order[-1] + 1
        next_index = min(
            remaining,
            key=lambda index: (
                matrix[current][index + 1]
                if matrix[current][index + 1] is not None
                else inf
            ),
        )
        order.append(next_index)
        remaining.remove(next_index)
    return order


def _two_opt(order: list[int], matrix: list[list[int | None]]) -> list[int]:
    best = list(order)
    best_score = _matrix_score(best, matrix)
    improved = True
    while improved:
        improved = False
        for start in range(len(best) - 1):
            for end in range(start + 2, len(best) + 1):
                candidate = [*best[:start], *reversed(best[start:end]), *best[end:]]
                score = _matrix_score(candidate, matrix)
                if score < best_score:
                    best, best_score, improved = candidate, score
                    improved = True
    return best


def _stop_point(stop: ResolvedStop) -> RoutePoint:
    from app.models.evidence import GeoPoint

    location = (
        GeoPoint(latitude=stop.latitude, longitude=stop.longitude)
        if stop.latitude is not None and stop.longitude is not None
        else None
    )
    return RoutePoint(stop.name, location)
