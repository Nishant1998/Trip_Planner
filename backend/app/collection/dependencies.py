"""Dependency invalidation rules for fields derived from other inputs."""

from __future__ import annotations

from app.models.enums import FieldSource
from app.models.memory import TripMemory
from app.collection.geography import clear_location_derived_context

# Extends the dependency map so destination changes clear route context and date/duration edits recompute each other bidirectionally.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    # Changing destination clears inferred route and purpose context.
    "destination": ("must_see", "avoid", "long_distance_modes", "trip_purpose"),
    # Changing origin may make the inferred transport mode stale.
    "origin": ("long_distance_modes",),
    # Changing any date/duration field triggers recomputation of the other two.
    "start_date": ("end_date", "trip_duration_days"),
    "end_date": ("start_date", "trip_duration_days"),
    "trip_duration_days": ("start_date", "end_date"),
    "num_children": ("children_ages",),
}


def invalidate_dependents(memory: TripMemory, changed_fields: set[str]) -> list[str]:
    """Clear dependent fields that are no longer trustworthy after changes."""

    cleared: list[str] = []
    for field in changed_fields:
        for dependent in DEPENDENCIES.get(field, ()):
            if dependent in changed_fields:
                continue
            meta = memory.get_variable_record(dependent)
            if meta and meta.source == FieldSource.USER:
                continue
            if memory.get_field_value(dependent) is not None:
                memory.clear_field(dependent)
                cleared.append(dependent)
    if "origin" in changed_fields or "destination" in changed_fields:
        clear_location_derived_context(memory)
    if "destination" in changed_fields:
        memory.internal.sanity_state.resolved_city_names = []
        memory.internal.sanity_state.destination_verified = False
    return cleared
