"""Separate provider travel time from planning buffers."""

from __future__ import annotations

from math import ceil

from app.models.route import TravelWindow


def calculate_travel_window(nominal_minutes: int, mode: str) -> TravelWindow:
    normalized = mode.casefold()
    if normalized in {"flight", "plane", "air"}:
        uncertainty = max(30, ceil(nominal_minutes * 0.15))
        access = 120
    elif normalized in {"train", "rail"}:
        uncertainty = max(20, ceil(nominal_minutes * 0.12))
        access = 30
    elif normalized in {"walking", "walk"}:
        uncertainty = max(5, ceil(nominal_minutes * 0.15))
        access = 5
    else:
        uncertainty = max(10, ceil(nominal_minutes * 0.20))
        access = 10
    return TravelWindow(
        nominal_minutes=nominal_minutes,
        uncertainty_buffer_minutes=uncertainty,
        access_buffer_minutes=access,
        planned_minutes=nominal_minutes + uncertainty + access,
        basis="Provider duration plus deterministic uncertainty and access allowance.",
    )
