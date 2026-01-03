"""State transitions and derived-field inference helpers."""

from __future__ import annotations

# Imports timedelta so that bidirectional date/duration inference can compute end or start from the known pair.
from datetime import timedelta

from app.models.enums import (
    FieldSource,
    TravelStyle,
    TripPurpose,
    TripStatus,
)
from app.models.memory import TripMemory

ALLOWED_TRANSITIONS: dict[TripStatus, list[TripStatus]] = {
    TripStatus.DRAFT: [TripStatus.COLLECTING],
    TripStatus.COLLECTING: [TripStatus.AWAITING_CONFIRMATION],
    TripStatus.AWAITING_CONFIRMATION: [TripStatus.COLLECTING, TripStatus.CONFIRMED],
    TripStatus.PLANNING: [
        TripStatus.CHECKPOINT_REVIEW,
        TripStatus.DRAFT_REVIEW,
        TripStatus.FAILED,
    ],
    TripStatus.CHECKPOINT_REVIEW: [TripStatus.PLANNING, TripStatus.COLLECTING],
    TripStatus.DRAFT_REVIEW: [TripStatus.CONFIRMED, TripStatus.COLLECTING],
    TripStatus.CONFIRMED: [TripStatus.ARCHIVED, TripStatus.COLLECTING],
    TripStatus.FAILED: [TripStatus.PLANNING, TripStatus.COLLECTING],
    TripStatus.ARCHIVED: [],
    TripStatus.CANCELLED: [],
}


def transition(current: TripStatus, target: TripStatus) -> TripStatus:
    """Return the requested state transition when it is allowed."""

    if current == target:
        return target
    allowed = ALLOWED_TRANSITIONS.get(
        current, []
    )  # from the current status, which next statuses are allowed?
    if target not in allowed:
        raise ValueError(
            f"Invalid trip status transition: {current.value} -> {target.value}"
        )
    return target


def re_infer_derived_fields(memory: TripMemory) -> None:
    """Refresh derived trip fields after user-facing values change."""

    # Implements bidirectional inclusive-day date inference so that any two of start_date, end_date, trip_duration_days derive the third.
    # User-facing durations use inclusive elapsed-day counting.
    # "8 days starting Sep 15" means Sep 15 is day 1, Sep 22 is day 8 → end = start + 7.
    # Formulae: end = start + (days - 1),  days = (end - start).days + 1
    start = memory.needed.start_date
    end = memory.needed.end_date
    days = memory.needed.trip_duration_days
    if start and end:
        memory.set_field(
            "trip_duration_days",
            (end - start).days + 1,
            FieldSource.INFERRED,
        )
    elif start and days and not end:
        memory.set_field(
            "end_date",
            start + timedelta(days=days - 1),
            FieldSource.INFERRED,
        )
    elif end and days and not start:
        memory.set_field(
            "start_date",
            end - timedelta(days=days - 1),
            FieldSource.INFERRED,
        )
    else:
        duration_meta = memory.get_variable_record("trip_duration_days")
        if duration_meta and duration_meta.source == FieldSource.INFERRED:
            memory.clear_field("trip_duration_days")

    if memory.optional.travel_style is None and memory.needed.trip_duration_days:
        inferred_style = (
            TravelStyle.RELAXED
            if memory.needed.trip_duration_days >= 7
            else TravelStyle.MODERATE
        )
        memory.set_field(
            "travel_style",
            inferred_style,
            FieldSource.INFERRED,
        )

    # Infer cuisine_prefs from dietary_restrictions when not already set.
    # If the user told us their dietary needs (vegetarian, jain, vegan etc.)
    # we already know their food constraints — no need to ask separately.
    if memory.optional.cuisine_prefs is None and memory.optional.dietary_restrictions:
        memory.set_field(
            "cuisine_prefs",
            list(memory.optional.dietary_restrictions),
            FieldSource.INFERRED,
        )

    # Infers trip_purpose=PILGRIMAGE from the destination subtype when the user has not explicitly set the purpose.
    dest = memory.needed.destination
    purpose_record = memory.get_variable_record("trip_purpose")
    if (
        dest is not None
        and dest.subtype in {"pilgrimage_circuit", "pilgrimage_site"}
        and (purpose_record is None or purpose_record.source != FieldSource.USER)
    ):
        memory.set_field("trip_purpose", TripPurpose.PILGRIMAGE, FieldSource.INFERRED)
