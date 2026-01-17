"""Configuration for the bounded V2 research agent."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchAgentConfig:
    model: str
    reasoning_effort: str
    max_agent_steps: int
    max_llm_calls: int
    max_repair_cycles: int
    system_prompt: str = ""

    @classmethod
    def from_environment(cls) -> ResearchAgentConfig:
        from .prompt import SYSTEM_PROMPT

        return cls(
            model=os.getenv("RESEARCH_AGENT_MODEL", os.getenv("RESEARCH_REASONING_MODEL", "gpt-5.6-terra")),
            reasoning_effort=os.getenv("RESEARCH_AGENT_REASONING_EFFORT", os.getenv("RESEARCH_REASONING_EFFORT", "medium")),
            max_agent_steps=int(os.getenv("RESEARCH_MAX_AGENT_STEPS", "16")),
            max_llm_calls=int(os.getenv("RESEARCH_MAX_LLM_CALLS", "8")),
            max_repair_cycles=int(os.getenv("RESEARCH_MAX_REPAIR_CYCLES", "1")),
            system_prompt=SYSTEM_PROMPT,
        )
