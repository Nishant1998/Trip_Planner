"""Helpers for copying profile defaults into trip memory."""

from __future__ import annotations

from app.models.api import PrefillOutput
from app.models.enums import FieldSource
from app.models.memory import TripMemory


def apply_prefill_to_memory(
    memory: TripMemory,
    prefill_output: PrefillOutput,
) -> TripMemory:
    """Write prefill results into trip memory. Source is always 'profile'."""

    for field_name, prefill_field in prefill_output.prefilled_fields.items():
        existing_meta = memory.get_variable_record(field_name)
        if existing_meta and existing_meta.source == FieldSource.USER:
            continue
        try:
            memory.set_field(
                variable_name=field_name,
                value=prefill_field.value,
                source=FieldSource.PROFILE,
            )
        except (AttributeError, ValueError):
            # Field not in memory or value failed validation, so skip it.
            continue
    return memory
