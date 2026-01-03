"""Cheap deterministic advisory context for collection responses."""

from __future__ import annotations

from datetime import date

from app.models.memory import TripMemory


def booking_urgency(memory: TripMemory) -> str | None:
    """Return a booking timing hint for near-future trips."""

    start = memory.needed.start_date
    if not start:
        return None
    days_away = (start - date.today()).days
    if 0 < days_away <= 45:
        return f"Trip is {days_away} days away; transport options may book up quickly."
    return None


def collect_advisories(memory: TripMemory) -> list[str]:
    """Return only hints that do not require destination knowledge.

    Worldwide destination and season advice already arrives in the single
    cached destination-intelligence record passed to the conversation agent.
    """

    urgency = booking_urgency(memory)
    return [urgency] if urgency is not None else []
