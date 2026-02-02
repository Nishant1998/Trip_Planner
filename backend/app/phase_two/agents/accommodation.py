"""Accommodation search role bound to the shared catalog."""

from .base import AgentRole


class AccommodationAgent(AgentRole):
    """Find dated stays without changing the itinerary allocation."""

    name = "accommodation"
    allowed_tools = ("accommodation.search",)
