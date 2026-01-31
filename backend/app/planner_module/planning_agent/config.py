"""Configuration for the bounded Planner V2 agent."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanningAgentConfig:
    model: str
    reasoning_effort: str
    # Compatibility field for legacy callers; the guarded graph uses max_llm_calls.
    max_agent_steps: int = 10
    max_llm_calls: int = 10
    max_repair_cycles: int = 2
    max_route_calls: int = 32
    max_search_calls: int = 3
    max_extract_calls: int = 5
    # Leave time to deterministically compile and validate the latest safe
    # notebook before the enclosing job timeout can cancel the request.
    runtime_seconds: int = 270
    finalization_reserve_seconds: int = 30
    model_timeout_seconds: float = 75
    max_output_tokens: int = 32768
    system_prompt: str = ""

    def __post_init__(self):
        if not self.model.strip():
            raise ValueError("PLANNER_AGENT_MODEL must not be empty")
        if self.max_llm_calls < 1 or self.max_repair_cycles < 0:
            raise ValueError("PLANNER_MAX_LLM_CALLS must be positive and PLANNER_MAX_REPAIR_CYCLES nonnegative")
        if min(self.max_route_calls, self.max_search_calls, self.max_extract_calls) < 0:
            raise ValueError("PLANNER_ROUTE/SEARCH/EXTRACT_CALLS limits must be nonnegative")
        if not 0 < self.finalization_reserve_seconds < self.runtime_seconds:
            raise ValueError("PLANNER_AGENT_RUNTIME_SECONDS must exceed a positive PLANNER_AGENT_FINALIZATION_RESERVE_SECONDS")
        if not math.isfinite(self.model_timeout_seconds) or self.model_timeout_seconds <= 0:
            raise ValueError("PLANNER_LLM_TIMEOUT_SECONDS must be a finite positive number")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("PLANNER_MAX_OUTPUT_TOKENS must be a positive integer")

    @classmethod
    def from_environment(cls) -> "PlanningAgentConfig":
        from .context import GUARDED_SYSTEM_PROMPT

        return cls(
            model=os.getenv("PLANNER_AGENT_MODEL", os.getenv("PLANNER_REASONING_MODEL", "gpt-5.6-terra")),
            reasoning_effort=os.getenv("PLANNER_AGENT_REASONING_EFFORT", os.getenv("PLANNER_REASONING_EFFORT", "medium")),
            max_agent_steps=int(os.getenv("PLANNER_MAX_AGENT_STEPS", "10")),
            max_llm_calls=int(os.getenv("PLANNER_MAX_LLM_CALLS", "10")),
            max_repair_cycles=int(os.getenv("PLANNER_MAX_REPAIR_CYCLES", "2")),
            max_route_calls=int(os.getenv("PLANNER_MAX_ROUTE_CALLS", "32")),
            max_search_calls=int(os.getenv("PLANNER_MAX_SEARCH_CALLS", "3")),
            max_extract_calls=int(os.getenv("PLANNER_MAX_EXTRACT_CALLS", "5")),
            runtime_seconds=int(os.getenv("PLANNER_AGENT_RUNTIME_SECONDS", "270")),
            finalization_reserve_seconds=int(
                os.getenv("PLANNER_AGENT_FINALIZATION_RESERVE_SECONDS", "30")
            ),
            model_timeout_seconds=float(os.getenv("PLANNER_LLM_TIMEOUT_SECONDS", "75")),
            max_output_tokens=int(os.getenv("PLANNER_MAX_OUTPUT_TOKENS", "32768")),
            system_prompt=GUARDED_SYSTEM_PROMPT,
        )
