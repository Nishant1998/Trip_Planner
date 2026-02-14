"""Build and estimate the required journey at the end of a planning day."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.models.evidence import RouteEvidence
from app.domain.travel_buffer import calculate_travel_window

RouteMap = Mapping[tuple[str, str], RouteEvidence]


@dataclass(frozen=True, slots=True)
class ClosingLeg:
    """One movement that must remain after optional activities are scheduled."""

    origin: str
    destination: str
    kind: Literal["local", "long"]


def build_closing_legs(
    *,
    current_stop: str,
    current_place_name: str | None,
    planned_stops: list[str],
    visited_stops: set[str],
    base_stop: str,
    return_target: str | None,
) -> list[ClosingLeg]:
    """Return a continuous place -> base -> final-target journey."""

    legs: list[ClosingLeg] = []
    location = current_place_name or current_stop

    if current_place_name and not _same(location, current_stop):
        legs.append(ClosingLeg(location, current_stop, "local"))
        location = current_stop

    for stop in planned_stops:
        if stop.casefold() in visited_stops:
            continue
        if not _same(location, stop):
            legs.append(ClosingLeg(location, stop, "long"))
        location = stop

    if not _same(location, base_stop):
        legs.append(ClosingLeg(location, base_stop, "long"))
        location = base_stop

    if return_target and not _same(location, return_target):
        legs.append(ClosingLeg(location, return_target, "long"))

    return legs


def projected_closing_time(
    start: datetime,
    legs: list[ClosingLeg],
    routes: RouteMap,
    *,
    local_mode: str,
    long_mode: str,
) -> datetime:
    """Estimate when all required closing legs finish, including buffers."""

    current = start
    for leg in legs:
        route = routes.get((leg.origin.casefold(), leg.destination.casefold()))
        fallback_minutes = 30 if leg.kind == "local" else 90
        duration = int(
            route.duration_minutes
            if route is not None and route.duration_minutes is not None
            else fallback_minutes
        )
        mode = (
            route.mode
            if route is not None
            else (local_mode if leg.kind == "local" else long_mode)
        )
        window = calculate_travel_window(duration, mode)
        current += timedelta(minutes=window.planned_minutes)
    return current


def _same(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()
