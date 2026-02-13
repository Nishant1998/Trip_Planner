"""Deterministic route-matrix ordering for places within one planning base."""

from __future__ import annotations

from itertools import permutations
from math import inf

from app.models.evidence import PlaceEvidence
from app.domain.route_lookup import CachedRouteLookup, RoutePoint

_EXACT_SEARCH_LIMIT = 8


async def optimized_place_order(
    anchor: RoutePoint,
    places: list[PlaceEvidence],
    routes: CachedRouteLookup,
    mode: str,
) -> list[PlaceEvidence]:
    """Minimize anchor -> POIs -> anchor time using provider road durations.

    Exact search is inexpensive for normal daily POI counts. Larger groups use
    nearest-neighbour plus a bounded 2-opt improvement. If matrix evidence is
    unavailable, caller order is preserved instead of inventing route times.
    """

    routable = [place for place in places if place.location is not None]
    if len(routable) < 2:
        return list(places)
    points = [
        anchor,
        *(
            RoutePoint(
                place.name,
                place.location,
                query=(
                    f"{place.name}, {place.stop_name}"
                    if place.stop_name.casefold() not in place.name.casefold()
                    else place.name
                ),
            )
            for place in routable
        ),
    ]
    try:
        matrix = await routes.get_matrix(points, mode)
    except Exception:
        return list(places)

    place_indices = list(range(1, len(points)))
    if len(place_indices) <= _EXACT_SEARCH_LIMIT:
        best = min(
            permutations(place_indices),
            key=lambda order: _round_trip_minutes(order, matrix.durations_minutes),
        )
        if _round_trip_minutes(best, matrix.durations_minutes) == inf:
            return list(places)
        ordered_routable = [routable[index - 1] for index in best]
    else:
        order = _nearest_neighbour(place_indices, matrix.durations_minutes)
        order = _two_opt(order, matrix.durations_minutes)
        ordered_routable = [routable[index - 1] for index in order]

    unroutable = [place for place in places if place.location is None]
    return [*ordered_routable, *unroutable]


def _round_trip_minutes(
    order: tuple[int, ...] | list[int],
    durations: list[list[int | None]],
) -> float:
    sequence = [0, *order, 0]
    total = 0.0
    for origin, destination in zip(sequence, sequence[1:], strict=False):
        value = durations[origin][destination]
        if value is None:
            return inf
        total += value
    return total


def _nearest_neighbour(
    indices: list[int],
    durations: list[list[int | None]],
) -> list[int]:
    remaining = set(indices)
    current = 0
    result: list[int] = []
    while remaining:
        next_index = min(
            remaining,
            key=lambda index: (
                durations[current][index]
                if durations[current][index] is not None
                else inf
            ),
        )
        result.append(next_index)
        remaining.remove(next_index)
        current = next_index
    return result


def _two_opt(
    order: list[int],
    durations: list[list[int | None]],
) -> list[int]:
    improved = list(order)
    best_cost = _round_trip_minutes(improved, durations)
    changed = True
    while changed:
        changed = False
        for start in range(len(improved) - 1):
            for end in range(start + 2, len(improved) + 1):
                candidate = [
                    *improved[:start],
                    *reversed(improved[start:end]),
                    *improved[end:],
                ]
                cost = _round_trip_minutes(candidate, durations)
                if cost < best_cost:
                    improved = candidate
                    best_cost = cost
                    changed = True
    return improved
