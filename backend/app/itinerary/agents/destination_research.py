"""Destination research role with Places and Weather permissions."""

from .base import AgentRole


class DestinationResearchAgent(AgentRole):
    """Gather destination, place, and weather evidence without scheduling."""

    name = "destination_research"
    allowed_tools = ("places.lookup", "weather.forecast")
