"""Shared rules for interpreting provider-backed route evidence."""

from __future__ import annotations

from collections.abc import Iterable

from app.models.evidence import RouteEvidence

SAME_LOCATION_DISTANCE_KM = 0.05


def has_same_location_route(
    routes: Iterable[RouteEvidence],
    origin_label: str,
    destination_label: str,
) -> bool:
    """Return true only when provider evidence confirms two labels are co-located."""

    origin_key = origin_label.casefold()
    destination_key = destination_label.casefold()
    return any(
        route.distance_km is not None
        and route.distance_km <= SAME_LOCATION_DISTANCE_KM
        and (
            (
                route.origin_name.casefold() == origin_key
                and route.destination_name.casefold() == destination_key
            )
            or (
                route.origin_name.casefold() == destination_key
                and route.destination_name.casefold() == origin_key
            )
        )
        for route in routes
    )
