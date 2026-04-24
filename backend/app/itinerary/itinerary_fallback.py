"""Deterministic Itinerary-agent fallback for missing model/provider evidence."""

from __future__ import annotations

from datetime import timedelta

from .contracts import CostRecord, PlannedDay, PlannedEvent, TripRequest


def fallback_days(request: TripRequest) -> list[PlannedDay]:
    """Preserve every requested date without inventing activities or travel.

    This is an Itinerary-agent helper, not a Supervisor or Aggregator planner.
    Requested destination labels remain in the request and final summary; a
    particular overnight base is not asserted without allocation evidence.
    """

    count = (request.end_date - request.start_date).days + 1
    return [
        PlannedDay(
            day=index + 1,
            date=request.start_date + timedelta(days=index),
            base_location=None,
            events=[PlannedEvent(
                id=f"unallocated-day-{index + 1}", kind="unallocated",
                title="Travel details not yet available",
                timing_status="unscheduled",
            )],
            daily_cost=CostRecord(
                currency=request.currency,
                known_subtotal=0,
                unknown_components=["accommodation", "transport", "activities"],
                budget_status="unknown",
            ),
        )
        for index in range(count)
    ]
