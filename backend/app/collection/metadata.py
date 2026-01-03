# Generates the trip title immediately from the destination name using Python, with no LLM call required.
"""Temp trip-title generation triggered as soon as the destination is known.

The trip title is set by Python the moment the destination is extracted —
no LLM call, no background task, no async.  It is a simple human-readable
label derived directly from the destination name(s):

  Single destination  →  "Trip to Goa"
  Two destinations    →  "Goa & Manali"
  Three or more       →  "Goa, Manali & more"

The title is updated every time the destination changes (user correction).

The trip description (one sentence) and trip_scope (domestic / international)
are generated post-confirmation by the metadata agent inside
PostConfirmationService.dispatch(), alongside the profile updates.
"""

from __future__ import annotations

from app.models.memory import TripMemory


def should_update_temp_title(
    memory: TripMemory,
    changed_fields: list[str],
) -> bool:
    """Return True when the destination was just set or corrected this turn.

    A temp title should be written whenever destination enters or changes,
    so the trip card in the UI always shows a meaningful label as soon
    as the user mentions a destination — even before dates or budget are known.
    """
    return "destination" in changed_fields and bool(
        memory.needed.destination and memory.needed.destination.items
    )


def make_temp_trip_title(memory: TripMemory) -> str:
    """Build a simple Python temp title from the destination name(s).

    No LLM call.  Reads the destination items directly from memory and
    formats a short readable label.
    """
    dest = memory.needed.destination
    if not dest or not dest.items:
        return "New Trip"

    items = dest.items
    if len(items) == 1:
        return f"Trip to {items[0]}"
    if len(items) == 2:
        return f"{items[0]} & {items[1]}"
    # Three or more stops — keep it short.
    return f"{items[0]}, {items[1]} & more"
