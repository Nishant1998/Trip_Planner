"""Transport research and route-evidence role; never books tickets."""

from .base import AgentRole


class TransportBookingAgent(AgentRole):
    """Collect travel options and directed route evidence, not reservations."""

    name = "transport_booking"
    allowed_tools = ("transport.search", "maps.lookup")
