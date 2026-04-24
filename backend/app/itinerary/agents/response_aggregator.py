"""Presentation role; authoritative plan fields remain Python-owned."""

from .base import AgentRole


class ResponseAggregatorAgent(AgentRole):
    """Present accepted state as a final plan without changing plan logic."""

    name = "response_aggregator"
    allowed_tools: tuple[str, ...] = ()
