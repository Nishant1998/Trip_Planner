"""Schema-driven defaulting rules used after required collection is complete."""

from __future__ import annotations

from datetime import time

from app.models.enums import FieldSource
from app.models.memory import TripMemory
from app.models.schema import TRIP_FIELD_SCHEMA
from app.models.state import (
    apply_defaults,
    load_runtime_memory,
)


def apply_silent_defaults(memory: TripMemory) -> list[str]:
    """Apply schema defaults to empty optional fields and return user-facing notes."""

    updated_memory = load_runtime_memory(apply_defaults(memory.to_canonical_dict()))

    defaults_applied: list[str] = []
    for field_name in TRIP_FIELD_SCHEMA["optional"]:
        before = memory.get_field_value(field_name)
        after = updated_memory.get_field_value(field_name)
        meta = updated_memory.get_variable_record(field_name)
        if before is not None or after is None:
            continue
        if meta is None or meta.source != FieldSource.DEFAULT:
            continue
        memory.set_field(
            field_name,
            _context_aware_default(memory, field_name, after),
            FieldSource.DEFAULT,
        )
        # Defaults are deliberately silent in normal conversation. They are
        # stored for the planner but are not a user decision to display.
        defaults_applied.append(field_name)

    return defaults_applied


def _context_aware_default(
    memory: TripMemory, field_name: str, value: object
) -> object:
    """Choose cautious day hours for relaxed or mobility-aware trips."""

    relaxed_or_mobility_aware = (
        getattr(memory.optional.travel_style, "value", None) == "relaxed"
        or bool(memory.optional.accessibility_needs)
        or bool(memory.optional.num_seniors)
    )
    if relaxed_or_mobility_aware:
        if field_name == "day_start_time":
            return time(9, 30)
        if field_name == "day_end_time":
            return time(19, 0)
    return value
