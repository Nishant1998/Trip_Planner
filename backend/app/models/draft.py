"""Structured intermediate itinerary produced by an itinerary provider."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DraftCost(BaseModel):
    """LLM-safe numeric cost before conversion into precise output money."""

    minimum: float = Field(default=0, ge=0)
    maximum: float = Field(default=0, ge=0)
    currency_code: str
    basis: str
    source_ids: list[str] = Field(default_factory=list)


class DraftActivity(BaseModel):
    kind: Literal["activity", "meal", "rest", "free_time", "check_in", "check_out"]
    title: str
    description: str
    evidence_place_id: str | None = None
    duration_minutes: int = Field(default=90, ge=15, le=720)
    cost: DraftCost | None = None


class DraftDay(BaseModel):
    day_number: int
    theme: str
    activities: list[DraftActivity] = Field(default_factory=list)


class DraftStay(BaseModel):
    stop_name: str
    area: str
    nights: int = Field(ge=1)
    description: str
    cost: DraftCost


class ItineraryDraft(BaseModel):
    title: str
    summary: str
    days: list[DraftDay]
    stays: list[DraftStay] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    context_notes_applied: list[str] = Field(default_factory=list)


class DraftResult(BaseModel):
    """One provider response with request-local usage metadata."""

    draft: ItineraryDraft
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
