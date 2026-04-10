"""Convert a confirmed Phase 1 memory to immutable Phase 2 input."""

from __future__ import annotations

import json

from app.models.enums import TripStatus
from app.models.memory import OptionalFieldValues, TripMemory

from .contracts import TripRequest


def request_from_confirmed_memory(
    memory: TripMemory, *, trip_id: int, snapshot_id: str
) -> TripRequest:
    if memory.metadata.status is not TripStatus.CONFIRMED:
        raise ValueError("Phase 2 requires a confirmed Phase 1 memory")
    needed = memory.needed
    if not (
        needed.origin
        and needed.destination
        and needed.destination.items
        and needed.start_date
        and needed.end_date
        and needed.num_adults
    ):
        raise ValueError("Confirmed trip is missing required planning inputs")
    snapshot = json.dumps(
        memory.to_canonical_dict(), sort_keys=True, separators=(",", ":")
    )
    preferences = OptionalFieldValues.model_validate(memory.optional.to_value_dict())
    notes = list(dict.fromkeys([
        *(preferences.trip_notes or []),
        *memory.internal.trip_context_notes,
    ]))
    preferences.trip_notes = notes or None
    return TripRequest(
        preferences=preferences,
        preference_sources={
            name: (record.source.value if record.source else None)
            for name in memory.optional.variable_names()
            for record in [memory.optional.get_record(name)]
        },
        destination_type=needed.destination.type.value,
        destination_subtype=needed.destination.subtype,
        trip_id=trip_id,
        snapshot_id=snapshot_id,
        origin=needed.origin,
        destinations=list(needed.destination.items),
        start_date=needed.start_date,
        end_date=needed.end_date,
        adults=needed.num_adults,
        children=memory.optional.num_children or 0,
        budget=needed.trip_budget,
        currency=memory.metadata.currency_code,
        timezone=memory.metadata.timezone,
        must_see=list(memory.optional.must_see or []),
        avoid=list(memory.optional.avoid or []),
        accessibility_needs=list(memory.optional.accessibility_needs or []),
        interests=list(memory.optional.activity_interests or []),
        confirmed_memory_json=snapshot,
    )
