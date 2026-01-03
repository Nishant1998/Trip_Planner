"""Small, context-aware reply suggestions for the trip-collection chat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.enums import TripStatus
from app.models.memory import TripMemory

if TYPE_CHECKING:
    from app.collection.turn_io import TurnOutcome


def suggestions_for_turn(memory: TripMemory, outcome: "TurnOutcome") -> list[str]:
    """Return optional, immediately usable replies for the latest assistant turn."""

    if outcome.status in {
        TripStatus.PLANNING,
        TripStatus.ARCHIVED,
        TripStatus.CANCELLED,
    }:
        return []
    if outcome.status is TripStatus.AWAITING_CONFIRMATION:
        return ["Yes, start planning", "I want to change something"]

    question = outcome.response_text.casefold()
    if "how would you like to travel" in question or "travel by" in question:
        return ["By flight", "By train", "I am flexible"]
    if "travel style" in question or "pace" in question:
        return ["Relaxed and unhurried", "Balanced sightseeing and downtime"]
    if "dietary" in question or "food preference" in question:
        return ["No dietary restrictions", "I prefer vegetarian food"]
    if "how many adults" in question or "how many people" in question:
        return ["It will be a solo trip", "There will be two adults"]
    if "budget" in question:
        return ["My budget is ₹50,000", "Help me choose a realistic budget"]
    if "date" in question or "when would you like" in question:
        return ["I am flexible with dates", "Help me choose suitable dates"]

    # Destination and origin must be user-provided. Avoid placeholder chips
    # that would send unusable text into the collection flow.
    del memory
    return []
