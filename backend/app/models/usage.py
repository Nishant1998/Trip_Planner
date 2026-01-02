"""LLM token-usage contracts shared by collection, storage, and future limits."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LLMUsageDelta(BaseModel):
    """Token usage produced by one model-backed agent run."""

    agent_name: str
    model: str
    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class LLMUsageSummary(BaseModel):
    """Aggregated token counts for one user or trip."""

    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        """Return provider-reported input plus output tokens.

        Cached input tokens are already included in input_tokens and must not
        be added again when calculating totals or future cost.
        """

        return self.input_tokens + self.output_tokens


class ToolUsageDelta(BaseModel):
    """Measured use of one external tool during a trip operation."""

    agent_name: str
    provider: str
    tool_name: str
    calls: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class ToolUsageSummary(BaseModel):
    """Aggregated external-tool use for one user or trip."""

    calls: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class TripUsageSummary(BaseModel):
    """Complete measured usage for a trip, ready for future limit decisions."""

    llm: LLMUsageSummary = Field(default_factory=LLMUsageSummary)
    tools: ToolUsageSummary = Field(default_factory=ToolUsageSummary)
