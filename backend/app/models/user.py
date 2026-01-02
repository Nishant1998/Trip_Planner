"""User-profile models shared across storage and agent outputs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.usage import LLMUsageSummary


class UserProfile(BaseModel):
    """Stored profile text plus account-level LLM usage totals."""

    full_text: str = ""
    compressed_text: str = ""
    llm_usage: LLMUsageSummary = Field(default_factory=LLMUsageSummary)


class CompressedProfileOutput(BaseModel):
    """Compressed profile text returned by the compression agent."""

    compressed_text: str
