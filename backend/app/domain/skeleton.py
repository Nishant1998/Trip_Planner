"""Coarse-to-fine day and stop allocation."""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.models.context import (
    DaySkeleton,
    PlanningContext,
    ResolvedDestination,
    TripSkeleton,
)

PACE_CAPACITY = {"relaxed": 2, "moderate": 3, "packed": 4}


def build_trip_skeleton(
    context: PlanningContext,
    destination: ResolvedDestination,
) -> TripSkeleton:
    """Allocate resolved stops across confirmed trip dates."""

    stop_names = [stop.name for stop in destination.stops]
    if not stop_names:
        stop_names = context.destination.items[:1]

    allocation = _allocate_stops(stop_names, context.duration_days, destination)
    base_capacity = PACE_CAPACITY.get(context.travel_style.casefold(), 3)
    light_first_day = any(
        phrase in note.casefold()
        for note in context.trip_context_notes
        for phrase in ("first day light", "light first day", "arrival day light")
    )

    days: list[DaySkeleton] = []
    previous_base: str | None = None
    for index, assigned_stops in enumerate(allocation):
        base_stop = assigned_stops[-1]
        is_transfer = previous_base is not None and base_stop != previous_base
        is_light = index == 0 and light_first_day
        is_arrival = index == 0 and context.trip_scope == "international"
        is_departure = (
            index == len(allocation) - 1
            and context.trip_scope == "international"
            and bool(context.end_location)
        )
        capacity = (
            0
            if is_arrival or is_departure
            else max(
                len(assigned_stops),
                base_capacity - int(is_transfer) - int(is_light),
            )
        )
        days.append(
            DaySkeleton(
                day_number=index + 1,
                date=context.start_date + timedelta(days=index),
                base_stop=base_stop,
                stop_names=assigned_stops,
                is_transfer_day=is_transfer,
                is_light_day=is_light,
                is_arrival_day=is_arrival,
                is_departure_day=is_departure,
                max_major_activities=capacity,
                day_start_time=context.day_start_time,
                day_end_time=context.day_end_time,
            )
        )
        previous_base = base_stop

    contingency = (context.trip_budget * Decimal("0.10")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assumptions = list(destination.assumptions)
    if light_first_day:
        assumptions.append(
            "The first day has reduced activity capacity because of trip context."
        )

    return TripSkeleton(
        days=days,
        contingency_amount=contingency,
        spendable_budget=context.trip_budget - contingency,
        assumptions=assumptions,
    )


def _allocate_stops(
    stops: list[str],
    day_count: int,
    destination: ResolvedDestination,
) -> list[list[str]]:
    if len(stops) >= day_count:
        allocation: list[list[str]] = []
        for day_index in range(day_count):
            start = round(day_index * len(stops) / day_count)
            end = round((day_index + 1) * len(stops) / day_count)
            allocation.append(stops[start:end] or [stops[min(start, len(stops) - 1)]])
        return allocation

    weights = {stop.name: stop.suggested_days for stop in destination.stops}
    allocation = [[stop] for stop in stops]
    while len(allocation) < day_count:
        best_stop = max(
            stops,
            key=lambda name: (
                weights.get(name, 1) / sum(name in assigned for assigned in allocation)
            ),
        )
        insert_after = max(
            index for index, assigned in enumerate(allocation) if best_stop in assigned
        )
        allocation.insert(insert_after + 1, [best_stop])
    return allocation
