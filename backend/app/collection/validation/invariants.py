# Post-extraction invariant checks that detect date inconsistencies without blocking writes.
"""Post-extraction, pre-write deterministic invariant checks."""

from __future__ import annotations

from datetime import date

from app.models.api import ExtractionOutput
from app.models.memory import TripMemory


def check_date_consistency(
    extraction: ExtractionOutput,
    memory: TripMemory,
) -> list[str]:
    """Return independent date-order and elapsed-duration warnings."""

    warnings: list[str] = []
    fields = {field.field_name: field.value for field in extraction.extracted_fields}
    start = fields.get("start_date") or memory.needed.start_date
    end = fields.get("end_date") or memory.needed.end_date
    days = fields.get("trip_duration_days") or memory.needed.trip_duration_days

    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    if start and end and end < start:
        warnings.append(f"end_date {end} is before start_date {start}.")

    if start and end and days:
        # Inclusive-day convention: Sep 15 → Sep 22 = 8 days (Sep 15 is day 1).
        # Formula: (end - start).days + 1, matching re_infer_derived_fields.
        computed = (end - start).days + 1
        if computed != days:
            warnings.append(
                f"Date inconsistency: {start} to {end} = {computed} days, "
                f"but trip_duration_days={days}. "
                "re_infer_derived_fields will recompute."
            )

    return warnings


def run_invariant_checks(
    extraction: ExtractionOutput,
    memory: TripMemory,
) -> list[str]:
    """Run checks that can be proven from explicit structured values."""

    # Traveler decomposition stays with extraction; there is no explicit total.
    return check_date_consistency(extraction, memory)
