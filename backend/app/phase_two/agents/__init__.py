"""Six approved Phase 2 agent templates.

Role instances bind to one run-local SixAgentRuntime, which enforces prompts,
typed outputs, evidence selection, and tool permissions.
"""

from .accommodation import AccommodationAgent
from .destination_research import DestinationResearchAgent
from .itinerary_planning import ItineraryPlanningAgent
from .response_aggregator import ResponseAggregatorAgent
from .supervisor import SupervisorAgent
from .transport_booking import TransportBookingAgent

AGENT_TEMPLATES = {
    agent.name: agent
    for agent in (
        SupervisorAgent,
        DestinationResearchAgent,
        ItineraryPlanningAgent,
        AccommodationAgent,
        TransportBookingAgent,
        ResponseAggregatorAgent,
    )
}

__all__ = [
    "AGENT_TEMPLATES",
    "AccommodationAgent",
    "DestinationResearchAgent",
    "ItineraryPlanningAgent",
    "ResponseAggregatorAgent",
    "SupervisorAgent",
    "TransportBookingAgent",
]
