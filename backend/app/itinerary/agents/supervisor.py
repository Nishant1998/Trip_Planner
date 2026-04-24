"""Supervisor routing role bound to the run-local LLM runtime."""

from .base import AgentRole


class SupervisorAgent(AgentRole):
    """Orchestrate the four specialists and hand accepted state to aggregation."""

    name = "supervisor"
    allowed_tools: tuple[str, ...] = ()
