"""Itinerary allocation/scheduling role with no external tools."""

from .base import AgentRole


class ItineraryPlanningAgent(AgentRole):
    """Allocate destinations and schedule visits from collected state only."""

    name = "itinerary_planning"
    allowed_tools: tuple[str, ...] = ()
